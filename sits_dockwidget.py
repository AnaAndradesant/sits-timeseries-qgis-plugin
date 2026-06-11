# -*- coding: utf-8 -*-
import os
import subprocess
import tempfile

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QDateEdit, QCheckBox,
    QGroupBox, QTextEdit, QScrollArea, QFrame,
    QMessageBox, QDialog, QFileDialog, QProgressBar,
    QRadioButton, QButtonGroup, QStackedWidget, QApplication,
    QSplitter, QSizePolicy, QSpinBox, QGridLayout
)
from qgis.PyQt.QtCore import Qt, QDate, pyqtSignal, QThread, pyqtSlot, QTimer, QObject
from qgis.PyQt.QtGui import QFont, QPixmap, QCursor, QColor, QPainter

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsProject, QgsGeometry, QgsPointXY, QgsWkbTypes,
    QgsVectorLayer
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsMapTool


# ==============================================================================
# Mapeamento de linhas do R → etapas da barra de progresso
# ==============================================================================
STEPS = [
    ("Modo: Ponto",              1, "1/4 — Configurando…"),
    ("Modo: Polígono",           1, "1/4 — Configurando…"),
    ("Criando cubo",             1, "1/4 — Conectando ao BDC…"),
    ("cache",                    1, "1/4 — Cubo carregado do cache ⚡"),
    ("query STAC",               1, "1/4 — Criando cubo BDC…"),
    ("Série temporal carregada", 4, "4/4 — Série carregada do cache ⚡"),
    ("Extraindo série",          2, "2/4 — Extraindo série temporal…"),
    ("Amostrado",                2, "2/4 — Amostras de polígono extraídas…"),
    ("Calculando índices",       3, "3/4 — Calculando índices…"),
    ("Despiking",                3, "3/4 — Removendo picos de nuvem…"),
    ("Filtro Savitzky",          3, "3/4 — Suavizando série…"),
    ("Série salva no cache",     3, "3/4 — Série salva no cache…"),
    ("Gerando gráfico",          4, "4/4 — Gerando gráfico…"),
    ("OK - Plot salvo",          4, "✅  Concluído!"),
]

SEP = "\x1f"  # separador de campos (ASCII Unit Separator)


# ==============================================================================
# Thread do servidor R persistente
# ==============================================================================
class RServerThread(QThread):
    """
    Servidor R persistente com IPC via arquivos (sem stdin).
    Python escreve sits_req.txt  →  R processa  →  R escreve sits_result.txt.
    Cancelamento: Python cria sits_cancel.txt  →  R checa entre etapas.
    """
    server_ready = pyqtSignal()
    server_died  = pyqtSignal(str)
    log_line     = pyqtSignal(str)

    def __init__(self, rscript_exe, server_script, cache_dir):
        super().__init__()
        self.rscript_exe   = rscript_exe
        self.server_script = server_script
        self.cache_dir     = cache_dir
        self._proc         = None
        self._stop_flag    = False

    # files
    def _f(self, name): return os.path.join(self.cache_dir, name)

    def run(self):
        self._stop_flag = False
        os.makedirs(self.cache_dir, exist_ok=True)
        cmd = [self.rscript_exe, "--vanilla", self.server_script, self.cache_dir]
        kw  = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, text=True, bufsize=1)
        if os.name == "nt":
            kw["creationflags"] = 0x08000000
        try:
            self._proc = subprocess.Popen(cmd, **kw)
        except FileNotFoundError as e:
            self.server_died.emit(str(e)); return

        for raw in self._proc.stdout:
            if self._stop_flag: break
            line = raw.rstrip()
            if not line: continue
            if line == "SERVER_READY":
                self.server_ready.emit(); continue
            self.log_line.emit(line)

        self._proc.wait()
        if not self._stop_flag:
            self.server_died.emit("Processo R encerrado inesperadamente.")

    def send_request(self, args_dict):
        """Escreve o pedido no arquivo de request (atômico)."""
        SEP = "\x1f"
        cache_dir = args_dict.get("cache_dir", self.cache_dir)
        content = SEP.join([
            str(args_dict.get("lat",      "")),
            str(args_dict.get("lon",      "")),
            str(args_dict["start"]),
            str(args_dict["end"]),
            str(args_dict["bands"]),
            str(args_dict["output"]),
            str(args_dict.get("wkt",      "")),
            cache_dir,
            str(args_dict.get("max_pts", 9)),
        ])
        req = self._f("sits_req.txt")
        tmp = req + ".tmp"
        try:
            with open(tmp, "w") as f: f.write(content)
            os.replace(tmp, req)
        except OSError: pass

    def cancel_request(self):
        try:
            with open(self._f("sits_cancel.txt"), "w") as f: f.write("cancel")
        except OSError: pass

    def stop(self):
        self._stop_flag = True
        # Send QUIT via request file
        try:
            req = self._f("sits_req.txt")
            with open(req, "w") as f: f.write("QUIT")
        except OSError: pass
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass



# ==============================================================================
# Ferramenta de captura de PONTO no mapa
# ==============================================================================
class PointCaptureTool(QgsMapToolEmitPoint):
    pointCaptured = pyqtSignal(float, float)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.setCursor(QCursor(Qt.CrossCursor))

    def canvasReleaseEvent(self, event):
        point  = self.toMapCoordinates(event.pos())
        wgs84  = QgsCoordinateReferenceSystem("EPSG:4326")
        src    = QgsProject.instance().crs()
        if src != wgs84:
            t     = QgsCoordinateTransform(src, wgs84, QgsProject.instance())
            point = t.transform(point)
        self.pointCaptured.emit(point.x(), point.y())


# ==============================================================================
# Ferramenta de captura de POLÍGONO no mapa
# ==============================================================================
class PolygonCaptureTool(QgsMapTool):
    polygonCaptured = pyqtSignal(str)
    vertexAdded     = pyqtSignal(int)

    _FILL   = QColor(255, 140, 0, 60)
    _STROKE = QColor(255, 140, 0, 220)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.setCursor(QCursor(Qt.CrossCursor))
        self._points = []

        self._band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._band.setColor(self._FILL)
        self._band.setStrokeColor(self._STROKE)
        self._band.setWidth(2)

    def canvasPressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pt = self.toMapCoordinates(event.pos())
            self._points.append(pt)
            self._update_band()
            self.vertexAdded.emit(len(self._points))

    def canvasDoubleClickEvent(self, event):
        if self._points:
            self._points.pop()
        self._finish()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._reset()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finish()
        elif event.key() == Qt.Key_Backspace and self._points:
            self._points.pop()
            self._update_band()
            self.vertexAdded.emit(len(self._points))

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self._finish()

    def _update_band(self):
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        for pt in self._points:
            self._band.addPoint(pt, False)
        self._band.addPoint(self._points[0] if self._points else QgsPointXY())
        self._band.show()

    def _reset(self):
        self._points = []
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        self.vertexAdded.emit(0)

    def _finish(self):
        if len(self._points) < 3:
            QMessageBox.warning(
                None, "Polígono inválido",
                "Desenhe pelo menos 3 vértices antes de fechar o polígono."
            )
            return

        wgs84   = QgsCoordinateReferenceSystem("EPSG:4326")
        src_crs = QgsProject.instance().crs()
        if src_crs != wgs84:
            t      = QgsCoordinateTransform(src_crs, wgs84, QgsProject.instance())
            pts_wgs = [t.transform(p) for p in self._points]
        else:
            pts_wgs = list(self._points)

        geom = QgsGeometry.fromPolygonXY([pts_wgs])
        wkt  = geom.asWkt(6)
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        self._points = []
        self.polygonCaptured.emit(wkt)

    def deactivate(self):
        self._band.reset(QgsWkbTypes.PolygonGeometry)
        self._points = []
        super().deactivate()


# ==============================================================================
# Painel de imagem reutilizável (scroll + zoom)
# ==============================================================================
class ImagePanel(QWidget):
    def __init__(self, image_path, label_text="", parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self._pixmap    = QPixmap(image_path)
        self._zoom      = 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        if label_text:
            lbl = QLabel(label_text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(
                "background:#f0f4ff;border:1px solid #c8d0e8;"
                "border-radius:4px;padding:4px 6px;"
                "font-size:9px;color:#334;"
            )
            layout.addWidget(lbl)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.img_label)
        layout.addWidget(self.scroll)
        self._apply_zoom()

    def _apply_zoom(self):
        w      = int(self._pixmap.width()  * self._zoom)
        h      = int(self._pixmap.height() * self._zoom)
        scaled = self._pixmap.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.img_label.setPixmap(scaled)
        self.img_label.resize(scaled.width(), scaled.height())

    def set_zoom(self, z):
        self._zoom = max(0.1, min(z, 4.0))
        self._apply_zoom()

    def get_zoom(self):
        return self._zoom

    def fit_to_viewport(self):
        vp  = self.scroll.viewport()
        z_w = (vp.width()  - 4) / max(self._pixmap.width(),  1)
        z_h = (vp.height() - 4) / max(self._pixmap.height(), 1)
        self.set_zoom(min(z_w, z_h))

    def pixmap(self):
        return self._pixmap


# ==============================================================================
# Visualizador único
# ==============================================================================
class PlotDialog(QDialog):
    def __init__(self, image_path, label_text="", parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Time Series — SITS/BDC")
        self.setWindowFlags(
            Qt.Window | Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint
        )
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(int(screen.width() * 0.85), int(screen.height() * 0.70))
        self.image_path = image_path
        self.label_text = label_text
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)
        tb = QHBoxLayout()
        self._add_zoom_controls(tb)
        tb.addStretch()
        self._add_save_controls(tb)
        layout.addLayout(tb)
        self.panel = ImagePanel(self.image_path, self.label_text)
        layout.addWidget(self.panel)

    def _add_zoom_controls(self, tb):
        btn_fit = QPushButton("⊡  Ajustar"); btn_fit.setFixedHeight(26)
        btn_fit.clicked.connect(self._fit); tb.addWidget(btn_fit)
        btn_100 = QPushButton("100%"); btn_100.setFixedSize(46, 26)
        btn_100.clicked.connect(lambda: self._set_zoom(1.0)); tb.addWidget(btn_100)
        btn_m = QPushButton("−"); btn_m.setFixedSize(26, 26)
        btn_m.clicked.connect(lambda: self._set_zoom(self.panel.get_zoom() - 0.15))
        tb.addWidget(btn_m)
        self.zoom_label = QLabel("100%"); self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setFixedWidth(42); tb.addWidget(self.zoom_label)
        btn_p = QPushButton("+"); btn_p.setFixedSize(26, 26)
        btn_p.clicked.connect(lambda: self._set_zoom(self.panel.get_zoom() + 0.15))
        tb.addWidget(btn_p)

    def _add_save_controls(self, tb):
        btn_save = QPushButton("💾  Salvar PNG…"); btn_save.setFixedHeight(26)
        btn_save.clicked.connect(self._save_as); tb.addWidget(btn_save)
        btn_close = QPushButton("✕  Fechar"); btn_close.setFixedHeight(26)
        btn_close.setStyleSheet("color:#c0392b;font-weight:bold;")
        btn_close.clicked.connect(self.close); tb.addWidget(btn_close)

    def _set_zoom(self, z):
        self.panel.set_zoom(z)
        self.zoom_label.setText(f"{int(self.panel.get_zoom() * 100)}%")

    def _fit(self):
        self.panel.fit_to_viewport()
        self.zoom_label.setText(f"{int(self.panel.get_zoom() * 100)}%")

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(120, self._fit)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self._set_zoom(self.panel.get_zoom() + (0.1 if event.angleDelta().y() > 0 else -0.1))
            event.accept()
        else:
            super().wheelEvent(event)

    def _save_as(self):
        import shutil
        dest, _ = QFileDialog.getSaveFileName(
            self, "Salvar gráfico", "time_series.png",
            "PNG (*.png);;Todos os arquivos (*)"
        )
        if dest:
            shutil.copy2(self.image_path, dest)
            QMessageBox.information(self, "Salvo", f"Salvo em:\n{dest}")


# ==============================================================================
# Visualizador de COMPARAÇÃO
# ==============================================================================
class PlotCompareDialog(QDialog):
    """Compara N séries em grade configurável (lado a lado, depois para baixo)."""
    COLORS = ["🔵","🟠","🟢","🔴","🟣","🟡","⚪","🟤"]
    PANEL_H = 300   # altura fixa de cada painel (px)

    def __init__(self, entries, parent=None):
        super().__init__(parent, Qt.Window)
        self.entries  = list(entries)
        self._panels  = []
        self._cols    = min(2, len(entries))
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(int(screen.width() * 0.93), int(screen.height() * 0.88))
        self.setWindowTitle(f"Comparação de {len(self.entries)} Séries — SITS/BDC")
        self.setWindowFlags(Qt.Window | Qt.WindowMinimizeButtonHint |
                            Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6); root.setSpacing(4)

        # ── Toolbar ──
        tb = QHBoxLayout(); tb.setSpacing(5)
        tb.addWidget(QLabel("📊  Comparação:")); 

        # Colunas
        tb.addWidget(QLabel("Colunas:"))
        self._spin_cols = QSpinBox()
        self._spin_cols.setRange(1, 4); self._spin_cols.setValue(self._cols)
        self._spin_cols.setFixedSize(48, 26)
        self._spin_cols.setToolTip("Número de colunas na grade")
        self._spin_cols.valueChanged.connect(self._on_cols_changed)
        tb.addWidget(self._spin_cols)

        sep = QFrame(); sep.setFrameShape(QFrame.VLine); sep.setFixedWidth(12)
        tb.addWidget(sep)

        # Zoom
        tb.addWidget(QLabel("Zoom:"))
        btn_fit = QPushButton("⊡ Ajustar"); btn_fit.setFixedHeight(26)
        btn_fit.clicked.connect(self._fit_all); tb.addWidget(btn_fit)
        btn_100 = QPushButton("100%"); btn_100.setFixedSize(46, 26)
        btn_100.clicked.connect(lambda: self._set_zoom_all(1.0)); tb.addWidget(btn_100)
        btn_m = QPushButton("−"); btn_m.setFixedSize(26, 26)
        btn_m.clicked.connect(lambda: self._step_zoom(-0.15)); tb.addWidget(btn_m)
        self.zoom_label = QLabel("100%"); self.zoom_label.setFixedWidth(40)
        self.zoom_label.setAlignment(Qt.AlignCenter); tb.addWidget(self.zoom_label)
        btn_p = QPushButton("+"); btn_p.setFixedSize(26, 26)
        btn_p.clicked.connect(lambda: self._step_zoom(+0.15)); tb.addWidget(btn_p)

        tb.addStretch()
        btn_save = QPushButton("💾  Salvar grade…"); btn_save.setFixedHeight(26)
        btn_save.clicked.connect(self._save_combined); tb.addWidget(btn_save)
        btn_close = QPushButton("✕  Fechar"); btn_close.setFixedHeight(26)
        btn_close.setStyleSheet("color:#c0392b;font-weight:bold;")
        btn_close.clicked.connect(self.close); tb.addWidget(btn_close)
        root.addLayout(tb)

        # ── Scroll area com grade ──
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.StyledPanel)
        self._scroll_content = QWidget()
        self._grid = None   # QGridLayout — criado em _rebuild_grid
        self._scroll.setWidget(self._scroll_content)
        root.addWidget(self._scroll)

        self._rebuild_grid()

    # ------------------------------------------------------------------
    def _rebuild_grid(self):
        # Substitui todo o widget de conteúdo do scroll — forma segura de
        # recriar o grid sem tentar desanexar um QLayout já parented.
        new_content = QWidget()
        self._grid = QGridLayout(new_content)
        self._grid.setContentsMargins(6, 6, 6, 6)
        self._grid.setSpacing(8)
        self._panels = []

        cols = max(1, self._cols)
        for i, entry in enumerate(self.entries):
            row_g, col_g = divmod(i, cols)
            icon = self.COLORS[i % len(self.COLORS)]

            card = QFrame()
            card.setFrameShape(QFrame.StyledPanel)
            card.setStyleSheet("QFrame{border:1px solid #d0d0d0;border-radius:4px;background:#fafafa;}")
            cv = QVBoxLayout(card); cv.setContentsMargins(4, 4, 4, 4); cv.setSpacing(3)

            # Cabeçalho do card
            hdr = QHBoxLayout()
            lbl = QLabel(f"{icon} <b>#{i+1}</b>  "
                         f"<span style='font-size:8px;color:#444;'>"
                         f"{entry['label'].replace(chr(10),' | ')[:72]}</span>")
            lbl.setTextFormat(Qt.RichText); lbl.setWordWrap(True)
            hdr.addWidget(lbl, 1)
            btn_rm = QPushButton("✕"); btn_rm.setFixedSize(20, 20)
            btn_rm.setStyleSheet("color:#c0392b;font-size:9px;padding:0;border:none;")
            btn_rm.setToolTip("Remover este painel")
            btn_rm.clicked.connect(lambda _, idx=i: self._remove_entry(idx))
            hdr.addWidget(btn_rm)
            cv.addLayout(hdr)

            panel = ImagePanel(entry["path"])
            panel.setFixedHeight(self.PANEL_H)
            panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._panels.append(panel)
            cv.addWidget(panel)

            self._grid.addWidget(card, row_g, col_g)

        # Garante colunas com peso igual
        for c in range(cols):
            self._grid.setColumnStretch(c, 1)

        # Troca o widget do scroll (seguro — evita reuso de layout parented)
        old_widget = self._scroll.takeWidget()
        if old_widget:
            old_widget.deleteLater()
        self._scroll.setWidget(new_content)
        self._scroll_content = new_content

        QTimer.singleShot(80, self._fit_all)

    # ------------------------------------------------------------------
    def _on_cols_changed(self, val):
        self._cols = val
        self._rebuild_grid()

    def _remove_entry(self, idx):
        if len(self.entries) <= 2:
            QMessageBox.information(self, "Mínimo", "Mínimo de 2 extrações na comparação.")
            return
        self.entries.pop(idx)
        self._cols = min(self._cols, len(self.entries))
        self._spin_cols.blockSignals(True)
        self._spin_cols.setValue(self._cols)
        self._spin_cols.blockSignals(False)
        self.setWindowTitle(f"Comparação de {len(self.entries)} Séries — SITS/BDC")
        self._rebuild_grid()

    # ------------------------------------------------------------------
    def _current_zoom(self):
        return self._panels[0].get_zoom() if self._panels else 1.0

    def _set_zoom_all(self, z):
        for p in self._panels: p.set_zoom(z)
        self.zoom_label.setText(f"{int(self._current_zoom()*100)}%")

    def _step_zoom(self, d):
        self._set_zoom_all(self._current_zoom() + d)

    def _fit_all(self):
        if not self._panels: return
        for p in self._panels: p.fit_to_viewport()
        self._set_zoom_all(min(p.get_zoom() for p in self._panels))

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(150, self._fit_all)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self._step_zoom(0.1 if event.angleDelta().y() > 0 else -0.1)
            event.accept()
        else:
            super().wheelEvent(event)

    # ------------------------------------------------------------------
    def _save_combined(self):
        dest, _ = QFileDialog.getSaveFileName(
            self, "Salvar grade", "comparacao_series.png",
            "PNG (*.png);;Todos os arquivos (*)"
        )
        if not dest: return
        pixmaps = [QPixmap(e["path"]) for e in self.entries]
        cols = max(1, self._cols)
        rows = (len(pixmaps) + cols - 1) // cols
        pw   = max(p.width()  for p in pixmaps)
        ph   = max(p.height() for p in pixmaps)
        gap  = 12
        total_w = cols * pw + (cols - 1) * gap
        total_h = rows * ph + (rows - 1) * gap
        combined = QPixmap(total_w, total_h)
        combined.fill(QColor("#ffffff"))
        painter = QPainter(combined)
        for i, pm in enumerate(pixmaps):
            r, c = divmod(i, cols)
            painter.drawPixmap(c * (pw + gap), r * (ph + gap), pm)
        painter.end()
        combined.save(dest)
        QMessageBox.information(self, "Salvo", f"Grade salva em:\n{dest}")



# ==============================================================================
# DockWidget principal
# ==============================================================================
class SITSDockWidget(QDockWidget):
    BANDS = [
        "B01","B02","B03","B04","B05","B06",
        "B07","B08","B8A","B09","B11","B12",
        "NDVI","NBR","NDWI","EVI"
    ]

    def __init__(self, iface):
        super().__init__("SITS Time Series Extractor")
        self.iface              = iface
        self.canvas             = iface.mapCanvas()
        self._cache_dir         = os.path.join(tempfile.gettempdir(), 'sits_qgis_cache')
        os.makedirs(self._cache_dir, exist_ok=True)
        self._server            = None
        self._server_ready      = False
        self._pending_request   = None
        self._server_restarts   = 0
        self._poll_timer        = None
        self.map_tool           = None
        self.prev_map_tool      = None
        self.plugin_dir         = os.path.dirname(__file__)
        self._wkt_polygon       = ""
        self._wkt_layer         = ""
        self._plots_history     = []
        self._history_checkboxes = []  # populated by _refresh_history_list
        self._pending_label     = ""
        self._build_ui()

    # --------------------------------------------------------------------------
    def _build_ui(self):
        container = QWidget()
        ml = QVBoxLayout(container)
        ml.setSpacing(8)
        ml.setContentsMargins(10, 10, 10, 10)

        # ── Cabeçalho ────────────────────────────────────────────────────────
        header = QFrame()
        header.setStyleSheet("QFrame{background:#2d6a4f; border-radius:5px;}")
        header.setFixedHeight(48)
        h_layout = QVBoxLayout(header)
        h_layout.setContentsMargins(8, 3, 8, 3)
        h_layout.setSpacing(1)

        top_row = QHBoxLayout(); top_row.setSpacing(7)
        ico = QLabel("📈")
        ico.setFont(QFont("Arial", 14))
        ico.setStyleSheet("background:transparent; color:white;")
        top_row.addWidget(ico)
        lbl_plugin = QLabel("SITS Time Series  ·  BDC / Sentinel-2")
        lbl_plugin.setFont(QFont("Arial", 9, QFont.Bold))
        lbl_plugin.setStyleSheet("background:transparent; color:#d8f3dc;")
        top_row.addWidget(lbl_plugin)
        top_row.addStretch()
        h_layout.addLayout(top_row)

        self.coord_label = QLabel("")
        self.coord_label.setFont(QFont("Courier", 8))
        self.coord_label.setStyleSheet("background:transparent; color:#b7e4c7; padding-left:26px;")
        h_layout.addWidget(self.coord_label)
        ml.addWidget(header)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine); sep.setFrameShadow(QFrame.Sunken)
        ml.addWidget(sep)

        # ── Modo ──────────────────────────────────────────────────────────────
        gm = QGroupBox("🗺  Modo de Amostragem")
        ml_mode = QVBoxLayout(gm)
        self._mode_group = QButtonGroup()
        row_mode = QHBoxLayout()
        self.radio_point = QRadioButton("📍 Ponto")
        self.radio_poly  = QRadioButton("🔷 Polígono")
        self.radio_layer = QRadioButton("🗂 Camada ativa")
        self.radio_point.setChecked(True)
        self._mode_group.addButton(self.radio_point, 0)
        self._mode_group.addButton(self.radio_poly,  1)
        self._mode_group.addButton(self.radio_layer, 2)
        self.radio_point.toggled.connect(self._on_mode_changed)
        self.radio_poly.toggled.connect(self._on_mode_changed)
        self.radio_layer.toggled.connect(self._on_mode_changed)
        row_mode.addWidget(self.radio_point)
        row_mode.addWidget(self.radio_poly)
        row_mode.addWidget(self.radio_layer)
        row_mode.addStretch()
        ml_mode.addLayout(row_mode)

        self.stack = QStackedWidget()

        # Página 0: Ponto
        page_point = QWidget()
        fp = QFormLayout(page_point)
        self.lat_edit = QLineEdit("-17.162688"); self.lat_edit.setPlaceholderText("ex: -17.1626")
        self.lon_edit = QLineEdit("-55.462868"); self.lon_edit.setPlaceholderText("ex: -55.4628")
        fp.addRow("Latitude:",  self.lat_edit)
        fp.addRow("Longitude:", self.lon_edit)
        self.btn_click = QPushButton("🖱  Capturar ponto no mapa")
        self.btn_click.setCheckable(True)
        self.btn_click.setStyleSheet("QPushButton:checked{background:#3a86ff;color:white;font-weight:bold;}")
        self.btn_click.clicked.connect(self._toggle_point_tool)
        fp.addRow(self.btn_click)
        self.stack.addWidget(page_point)

        # Página 1: Polígono
        page_poly = QWidget()
        fpoly = QVBoxLayout(page_poly)
        self.btn_poly = QPushButton("✏️  Desenhar polígono no mapa")
        self.btn_poly.setCheckable(True)
        self.btn_poly.setStyleSheet("QPushButton:checked{background:#ff6b35;color:white;font-weight:bold;}")
        self.btn_poly.clicked.connect(self._toggle_poly_tool)
        fpoly.addWidget(self.btn_poly)
        hint_poly = QLabel("<small>Esq: vértice | Dir/Enter: fechar | Esc: cancelar | ⌫: desfazer vértice</small>")
        hint_poly.setStyleSheet("color:grey;"); hint_poly.setWordWrap(True)
        fpoly.addWidget(hint_poly)
        self.poly_status = QLabel("Nenhum polígono capturado")
        self.poly_status.setStyleSheet("color:#c0392b;font-size:9px;font-style:italic;")
        self.poly_status.setWordWrap(True)
        fpoly.addWidget(self.poly_status)
        btn_clear = QPushButton("🗑  Limpar polígono"); btn_clear.setFixedHeight(24)
        btn_clear.clicked.connect(self._clear_polygon); fpoly.addWidget(btn_clear)
        self.stack.addWidget(page_poly)

        # Página 2: Camada ativa
        page_layer = QWidget()
        flayer = QVBoxLayout(page_layer); flayer.setSpacing(6)
        hint_layer = QLabel("Selecione uma feição na camada vetorial ativa e clique em Capturar.\nFunciona com camadas de segmentos, classificações ou qualquer polígono.")
        hint_layer.setWordWrap(True); hint_layer.setStyleSheet("color:grey;font-size:9px;")
        flayer.addWidget(hint_layer)
        self.btn_capture_layer = QPushButton("🗂  Capturar feição selecionada")
        self.btn_capture_layer.setStyleSheet(
            "QPushButton{background:#6c5ce7;color:white;font-weight:bold;}"
            "QPushButton:hover{background:#5a4bd1;}")
        self.btn_capture_layer.clicked.connect(self._capture_from_layer)
        flayer.addWidget(self.btn_capture_layer)
        self.layer_status = QLabel("Nenhuma feição capturada")
        self.layer_status.setStyleSheet("color:#c0392b;font-size:9px;font-style:italic;")
        self.layer_status.setWordWrap(True)
        flayer.addWidget(self.layer_status)
        btn_clear_layer = QPushButton("🗑  Limpar"); btn_clear_layer.setFixedHeight(24)
        btn_clear_layer.clicked.connect(self._clear_layer_capture); flayer.addWidget(btn_clear_layer)
        self.stack.addWidget(page_layer)

        ml_mode.addWidget(self.stack)

        # ── Pontos de amostragem (visível em modo Polígono / Camada) ──
        self._pts_row = QWidget()
        pts_layout = QHBoxLayout(self._pts_row); pts_layout.setContentsMargins(0,2,0,0)
        pts_layout.addWidget(QLabel("Pontos amostrados:"))
        self._spin_max_pts = QSpinBox()
        self._spin_max_pts.setRange(3, 50); self._spin_max_pts.setValue(9)
        self._spin_max_pts.setFixedSize(56, 24)
        self._spin_max_pts.setToolTip(
            "Número máximo de pontos dentro do polígono.\n"
            "Mais pontos = média mais precisa, mas download mais lento.\n"
            "Padrão 9 é um bom equilíbrio para uso diário.")
        pts_layout.addWidget(self._spin_max_pts)
        # Labels de referência rápida
        hint_pts = QLabel("  ⚡ 3–9 = rápido  |  🎯 10–20 = preciso  |  🐢 >20 = lento")
        hint_pts.setStyleSheet("color:#777;font-size:8px;")
        pts_layout.addWidget(hint_pts); pts_layout.addStretch()
        ml_mode.addWidget(self._pts_row)
        ml.addWidget(gm)

        # Mostra/esconde a linha de pontos conforme o modo
        self._on_mode_changed()

        # ── Datas ─────────────────────────────────────────────────────────────
        gd = QGroupBox("📅 Intervalo de Datas")
        fd = QFormLayout(gd)
        self.date_ini = QDateEdit(QDate(2023, 8, 1)); self.date_ini.setCalendarPopup(True)
        self.date_ini.setDisplayFormat("yyyy-MM-dd")
        self.date_fim = QDateEdit(QDate(2025, 7, 31)); self.date_fim.setCalendarPopup(True)
        self.date_fim.setDisplayFormat("yyyy-MM-dd")
        fd.addRow("Data inicial:", self.date_ini)
        fd.addRow("Data final:",   self.date_fim)
        ml.addWidget(gd)

        # ── Bandas ────────────────────────────────────────────────────────────
        gb = QGroupBox("📊 Bandas / Índices")
        bl = QVBoxLayout(gb)
        self.band_checks = {}
        default_on = {"B08", "B11", "NDVI"}
        row_h = QHBoxLayout()
        for i, band in enumerate(self.BANDS):
            cb = QCheckBox(band); cb.setChecked(band in default_on)
            if band == "EVI":
                cb.setStyleSheet("color:#006400;font-weight:bold;")
            self.band_checks[band] = cb
            row_h.addWidget(cb)
            if (i + 1) % 4 == 0:
                bl.addLayout(row_h); row_h = QHBoxLayout()
        if row_h.count():
            bl.addLayout(row_h)
        ml.addWidget(gb)

        # ── Comparação ────────────────────────────────────────────────────────
        gc = QGroupBox("🔄 Comparação de Séries")
        cl = QVBoxLayout(gc); cl.setSpacing(4)

        self.compare_status = QLabel("Nenhum plot em memória")
        self.compare_status.setStyleSheet("color:#555;font-size:9px;font-style:italic;")
        self.compare_status.setWordWrap(True)
        cl.addWidget(self.compare_status)

        # Mini-toolbar: selecionar todos / nenhum
        sel_row = QHBoxLayout()
        btn_sel_all  = QPushButton("☑ Todos");  btn_sel_all.setFixedHeight(20)
        btn_sel_all.setStyleSheet("font-size:8px;")
        btn_sel_all.clicked.connect(lambda: self._select_all_history(True))
        btn_sel_none = QPushButton("☐ Nenhum"); btn_sel_none.setFixedHeight(20)
        btn_sel_none.setStyleSheet("font-size:8px;")
        btn_sel_none.clicked.connect(lambda: self._select_all_history(False))
        sel_row.addWidget(btn_sel_all); sel_row.addWidget(btn_sel_none); sel_row.addStretch()
        cl.addLayout(sel_row)

        # Lista rolável com checkboxes
        self.history_list_widget = QWidget()
        self._history_list_layout = QVBoxLayout(self.history_list_widget)
        self._history_list_layout.setContentsMargins(2,2,2,2)
        self._history_list_layout.setSpacing(2)
        self._history_checkboxes = []   # QCheckBox por entrada
        scroll_hist = QScrollArea(); scroll_hist.setWidgetResizable(True)
        scroll_hist.setFixedHeight(100); scroll_hist.setFrameShape(QFrame.StyledPanel)
        scroll_hist.setWidget(self.history_list_widget)
        cl.addWidget(scroll_hist)

        row_cmp = QHBoxLayout()
        self.btn_compare = QPushButton("📊  Comparar selecionados")
        self.btn_compare.setFixedHeight(28)
        self.btn_compare.setStyleSheet(
            "QPushButton{background:#1a5276;color:white;font-weight:bold;border-radius:3px;}"
            "QPushButton:hover{background:#2471a3;}"
            "QPushButton:disabled{background:#aaa;color:#ddd;}")
        self.btn_compare.setEnabled(False)
        self.btn_compare.setToolTip("Abre a grade de comparação com as extrações marcadas.")
        self.btn_compare.clicked.connect(self._open_comparison)
        row_cmp.addWidget(self.btn_compare)
        self.btn_clear_history = QPushButton("🗑  Limpar tudo")
        self.btn_clear_history.setFixedHeight(28); self.btn_clear_history.setFixedWidth(90)
        self.btn_clear_history.setStyleSheet("font-size:9px;")
        self.btn_clear_history.clicked.connect(self._clear_plots_history)
        self.btn_clear_history.setEnabled(False)
        row_cmp.addWidget(self.btn_clear_history)
        cl.addLayout(row_cmp)

        hint_cmp = QLabel("Marque as extrações desejadas e clique em 'Comparar selecionados'.")
        hint_cmp.setStyleSheet("color:grey;font-size:8px;"); hint_cmp.setWordWrap(True)
        cl.addWidget(hint_cmp)
        ml.addWidget(gc)

        # ── Config R ──────────────────────────────────────────────────────────
        gr = QGroupBox("⚙️ Configurações do R")
        rl = QVBoxLayout(gr)
        detected = self._find_rscript()
        sc = "#2d6a4f" if detected else "#c0392b"
        st = "✅ Rscript detectado" if detected else "❌ Não encontrado — use 📂"
        self.rscript_status = QLabel(st)
        self.rscript_status.setStyleSheet(f"color:{sc};font-size:9px;")
        rl.addWidget(self.rscript_status)
        row_r = QHBoxLayout()
        self.rscript_edit = QLineEdit(detected)
        self.rscript_edit.setPlaceholderText(r"C:\Program Files\R\R-x.x.x\bin\Rscript.exe")
        self.rscript_edit.editingFinished.connect(self._on_rscript_changed)
        row_r.addWidget(self.rscript_edit)
        btn_br = QPushButton("📂"); btn_br.setFixedWidth(30)
        btn_br.clicked.connect(self._browse_rscript); row_r.addWidget(btn_br)
        rl.addLayout(row_r)
        hint = QLabel(r"Windows: C:\Program Files\R\R-4.x.x\bin\Rscript.exe")
        hint.setStyleSheet("color:grey;font-size:8px;"); hint.setWordWrap(True)
        rl.addWidget(hint)

        # Status do servidor R persistente
        self.server_status_lbl = QLabel("⏳  Aguardando inicialização do servidor R…")
        self.server_status_lbl.setStyleSheet("color:#e67e22;font-size:9px;font-weight:bold;")
        self.server_status_lbl.setWordWrap(True)
        rl.addWidget(self.server_status_lbl)

        cache_info = QLabel("⚡ Cache de cubo BDC e série ativo (válido por 48h)")
        cache_info.setStyleSheet("color:#2d6a4f;font-size:8px;")
        rl.addWidget(cache_info)
        btn_clear_cache = QPushButton("🗑  Limpar cache (cubo + série)")
        btn_clear_cache.setFixedHeight(22); btn_clear_cache.setStyleSheet("font-size:9px;")
        btn_clear_cache.clicked.connect(self._clear_cache); rl.addWidget(btn_clear_cache)
        ml.addWidget(gr)

        # ── Botão extrair ─────────────────────────────────────────────────────
        run_cancel_row = QHBoxLayout(); run_cancel_row.setSpacing(6)
        self.btn_run = QPushButton("▶  Extrair e Plotar")
        self.btn_run.setMinimumHeight(38)
        self.btn_run.setStyleSheet(
            "QPushButton{background:#2d6a4f;color:white;font-weight:bold;border-radius:4px;}"
            "QPushButton:hover{background:#40916c;}"
            "QPushButton:disabled{background:#aaa;}")
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self._run)
        run_cancel_row.addWidget(self.btn_run)
        self.btn_cancel = QPushButton("⏹  Cancelar")
        self.btn_cancel.setMinimumHeight(38)
        self.btn_cancel.setFixedWidth(110)
        self.btn_cancel.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;font-weight:bold;border-radius:4px;}"
            "QPushButton:hover{background:#e74c3c;}")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel_extraction)
        run_cancel_row.addWidget(self.btn_cancel)
        ml.addLayout(run_cancel_row)

        # ── Progresso ─────────────────────────────────────────────────────────
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("font-size:9px;color:#2d6a4f;font-weight:bold;")
        ml.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 4); self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False); self.progress_bar.setFixedHeight(10)
        self.progress_bar.setStyleSheet(
            "QProgressBar{border:1px solid #ccc;border-radius:4px;background:#eee;}"
            "QProgressBar::chunk{background:#2d6a4f;border-radius:4px;}")
        self.progress_bar.setVisible(False)
        ml.addWidget(self.progress_bar)

        # ── Log ───────────────────────────────────────────────────────────────
        ml.addWidget(QLabel("Log:"))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(80); self.log_box.setMaximumHeight(200)
        self.log_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.log_box.setStyleSheet("font-family:monospace;font-size:9px;background:#1e1e1e;color:#d4d4d4;")
        ml.addWidget(self.log_box)
        ml.addStretch()

        outer_scroll = QScrollArea()
        outer_scroll.setWidgetResizable(True)
        outer_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer_scroll.setFrameShape(QFrame.NoFrame)
        outer_scroll.setWidget(container)
        self.setWidget(outer_scroll)
        self.setMinimumWidth(340)

        # Inicia o servidor R assim que a UI estiver pronta
        QTimer.singleShot(200, self._start_server)

    # --------------------------------------------------------------------------
    # Servidor R persistente
    # --------------------------------------------------------------------------
    def _start_server(self):
        rscript = self.rscript_edit.text().strip()
        if not rscript or not os.path.exists(rscript):
            self.server_status_lbl.setText("❌  Configure o caminho do Rscript para iniciar o servidor.")
            self.server_status_lbl.setStyleSheet("color:#c0392b;font-size:9px;font-weight:bold;")
            return

        # Para servidor anterior se existir
        if self._server and self._server.isRunning():
            self._server.stop()
            self._server.wait(3000)

        self._server_ready = False
        self.btn_run.setEnabled(False)
        self.server_status_lbl.setText("⏳  Aquecendo R — carregando pacotes (só na 1ª vez)…")
        self.server_status_lbl.setStyleSheet("color:#e67e22;font-size:9px;font-weight:bold;")

        server_script = os.path.join(self.plugin_dir, "run_sits_server.R")
        self._server = RServerThread(rscript, server_script, self._cache_dir)
        self._server.server_ready.connect(self._on_server_ready)
        self._server.server_died.connect(self._on_server_died)
        self._server.log_line.connect(self._log)
        self._server.start()
        self._log("⏳  Servidor R iniciando — pacotes sendo carregados…")

    @pyqtSlot()
    def _on_server_ready(self):
        self._server_ready = True
        self._server_restarts = 0  # conectou com sucesso
        self.btn_run.setEnabled(True)
        self.server_status_lbl.setText("✅  Servidor R pronto — próximas extrações serão mais rápidas")
        self.server_status_lbl.setStyleSheet("color:#2d6a4f;font-size:9px;font-weight:bold;")
        self._log("✅  Servidor R carregado! Extrações a partir de agora sem overhead de startup.")

        # Se havia request pendente (usuário clicou antes de ficar pronto)
        if self._pending_request:
            args = self._pending_request
            self._pending_request = None
            self._dispatch_request(args)

    @pyqtSlot(str)
    def _on_server_died(self, msg):
        self._server_ready = False
        self.btn_run.setEnabled(False)
        self._server_restarts += 1
        self._log(f"❌  {msg}")
        MAX_RESTARTS = 3
        if self._server_restarts <= MAX_RESTARTS:
            wait = self._server_restarts * 3000  # 3s, 6s, 9s
            self.server_status_lbl.setText(
                f"⚠️  Servidor R encerrou (tentativa {self._server_restarts}/{MAX_RESTARTS}) — reiniciando…")
            self.server_status_lbl.setStyleSheet("color:#e67e22;font-size:9px;font-weight:bold;")
            QTimer.singleShot(wait, self._start_server)
        else:
            self.server_status_lbl.setText(
                "❌  Servidor R não inicializou após 3 tentativas. "
                "Verifique o caminho do Rscript e se os pacotes sits/sf/ggplot2 estão instalados.")
            self.server_status_lbl.setStyleSheet("color:#c0392b;font-size:9px;font-weight:bold;")

    def _on_rscript_changed(self):
        """Reinicia o servidor se o path do Rscript mudar."""
        self._start_server()

    # --------------------------------------------------------------------------
    def _on_mode_changed(self, checked=None):
        if self.radio_point.isChecked():
            self.stack.setCurrentIndex(0)
            self._pts_row.setVisible(False)
        elif self.radio_poly.isChecked():
            self.stack.setCurrentIndex(1)
            self._pts_row.setVisible(True)
        else:
            self.stack.setCurrentIndex(2)
            self._pts_row.setVisible(True)
        self._deactivate_map_tool()

    # --------------------------------------------------------------------------
    def _toggle_point_tool(self, checked):
        if checked:
            self.prev_map_tool = self.canvas.mapTool()
            t = PointCaptureTool(self.canvas)
            t.pointCaptured.connect(self._on_point_captured)
            self.map_tool = t
            self.canvas.setMapTool(t)
            self._log("🖱 Clique no mapa para capturar a coordenada…")
        else:
            self._deactivate_map_tool()

    @pyqtSlot(float, float)
    def _on_point_captured(self, lon, lat):
        self.lon_edit.setText(f"{lon:.6f}")
        self.lat_edit.setText(f"{lat:.6f}")
        self.coord_label.setText(f"📍 Lat {lat:.5f}  Lon {lon:.5f}")
        self._log(f"✅ Ponto capturado → Lat: {lat:.6f}  Lon: {lon:.6f}")
        self._deactivate_map_tool()

    def _toggle_poly_tool(self, checked):
        if checked:
            self.prev_map_tool = self.canvas.mapTool()
            t = PolygonCaptureTool(self.canvas)
            t.polygonCaptured.connect(self._on_polygon_captured)
            t.vertexAdded.connect(self._on_vertex_added)
            self.map_tool = t
            self.canvas.setMapTool(t)
            self._log("✏️ Desenhe o polígono — Esq: vértice | Dir/Enter: fechar | Esc: cancelar")
        else:
            self._deactivate_map_tool()

    @pyqtSlot(int)
    def _on_vertex_added(self, count):
        self.poly_status.setText(f"⬡ Desenhando… {count} vértice(s)")
        self.poly_status.setStyleSheet("color:#ff6b35;font-size:9px;font-weight:bold;")

    @pyqtSlot(str)
    def _on_polygon_captured(self, wkt):
        self._wkt_polygon = wkt
        pts = wkt.count(",") + 1
        self.poly_status.setText(f"✅ Polígono capturado ({pts} vértices)")
        self.poly_status.setStyleSheet("color:#2d6a4f;font-size:9px;font-weight:bold;")
        self._log(f"✅ Polígono capturado: {wkt[:80]}…")
        self._deactivate_map_tool()

    def _clear_polygon(self):
        self._wkt_polygon = ""
        self.poly_status.setText("Nenhum polígono capturado")
        self.poly_status.setStyleSheet("color:#c0392b;font-size:9px;font-style:italic;")

    def _capture_from_layer(self):
        layer = self.iface.activeLayer()
        if layer is None:
            QMessageBox.warning(self, "Sem camada ativa", "Nenhuma camada está ativa no painel de camadas."); return
        if not isinstance(layer, QgsVectorLayer):
            QMessageBox.warning(self, "Tipo incompatível", f"A camada ativa '{layer.name()}' não é vetorial."); return
        feicoes = layer.selectedFeatures()
        if not feicoes:
            QMessageBox.warning(self, "Sem seleção", f"Nenhuma feição selecionada em '{layer.name()}'."); return
        if len(feicoes) > 1:
            resp = QMessageBox.question(self, "Múltiplas feições",
                f"{len(feicoes)} feições selecionadas.\nDeseja usar a união (dissolve) de todas?",
                QMessageBox.Yes | QMessageBox.No)
            if resp == QMessageBox.Yes:
                geom = feicoes[0].geometry()
                for f in feicoes[1:]: geom = geom.combine(f.geometry())
            else: return
        else:
            geom = feicoes[0].geometry()
        if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PolygonGeometry:
            QMessageBox.warning(self, "Geometria inválida", "A feição selecionada não é um polígono."); return
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if layer.crs() != wgs84:
            geom.transform(QgsCoordinateTransform(layer.crs(), wgs84, QgsProject.instance()))
        self._wkt_layer = geom.asWkt(6)
        fid = feicoes[0].id() if len(feicoes) == 1 else "dissolve"
        self.layer_status.setText(f"✅ Feição {fid} — '{layer.name()}'")
        self.layer_status.setStyleSheet("color:#2d6a4f;font-size:9px;font-weight:bold;")
        self._log(f"✅ Feição capturada de '{layer.name()}' (id={fid}): {self._wkt_layer[:80]}…")

    def _clear_layer_capture(self):
        self._wkt_layer = ""
        self.layer_status.setText("Nenhuma feição capturada")
        self.layer_status.setStyleSheet("color:#c0392b;font-size:9px;font-style:italic;")

    def _deactivate_map_tool(self):
        if self.map_tool:
            try: self.map_tool.deactivate()
            except Exception: pass
            if self.prev_map_tool: self.canvas.setMapTool(self.prev_map_tool)
            else: self.canvas.unsetMapTool(self.map_tool)
            self.map_tool = None
        self.btn_click.setChecked(False)
        self.btn_poly.setChecked(False)

    def _find_rscript(self):
        import shutil, glob
        found = shutil.which("Rscript")
        if found: return found
        for pat in [
            r"C:\Program Files\R\R-*\bin\Rscript.exe",
            r"C:\Program Files\R\R-*\bin\x64\Rscript.exe",
            r"C:\Program Files (x86)\R\R-*\bin\Rscript.exe",
        ]:
            m = sorted(glob.glob(pat), reverse=True)
            if m: return m[0]
        for p in ["/usr/bin/Rscript", "/usr/local/bin/Rscript", "/opt/homebrew/bin/Rscript"]:
            if os.path.exists(p): return p
        return ""

    def _browse_rscript(self):
        start = r"C:\Program Files\R" if os.name == "nt" else "/usr"
        path, _ = QFileDialog.getOpenFileName(
            self, "Localizar Rscript.exe", start,
            "Rscript (Rscript.exe Rscript);;Todos os arquivos (*)"
        )
        if path:
            self.rscript_edit.setText(path)
            self.rscript_status.setText("✅ Rscript configurado manualmente")
            self.rscript_status.setStyleSheet("color:#2d6a4f;font-size:9px;")
            self._start_server()

    def _clear_cache(self):
        import glob
        cache_dir = os.path.join(tempfile.gettempdir(), "sits_qgis_cache")
        files = glob.glob(os.path.join(cache_dir, "*.rds"))
        if not files:
            QMessageBox.information(self, "Cache", "Cache já está vazio."); return
        for f in files:
            try: os.remove(f)
            except Exception: pass
        self._log(f"🗑  {len(files)} arquivo(s) de cache removido(s).")
        QMessageBox.information(self, "Cache limpo", f"{len(files)} arquivo(s) removido(s).")

    # --------------------------------------------------------------------------
    def _clear_plots_history(self):
        self._plots_history = []
        self._history_checkboxes = []
        self._refresh_history_list()
        self._log("🗑  Histórico de comparação limpo.")

    def _remove_from_history(self, idx):
        if 0 <= idx < len(self._plots_history):
            self._plots_history.pop(idx)
            self._refresh_history_list()

    def _select_all_history(self, checked):
        for cb in self._history_checkboxes:
            cb.setChecked(checked)

    def _refresh_history_list(self):
        """Reconstrói a lista de extrações com checkboxes."""
        COLORS = ["🔵","🟠","🟢","🔴","🟣","🟡","⚪","🟤"]
        while self._history_list_layout.count():
            item = self._history_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._history_checkboxes = []
        for i, entry in enumerate(self._plots_history):
            icon  = COLORS[i % len(COLORS)]
            short = entry["label"].replace("\n", " | ")[:60]

            row = QHBoxLayout(); row.setContentsMargins(2, 1, 2, 1)

            cb = QCheckBox()
            cb.setChecked(True)   # marcado por padrão ao adicionar
            cb.stateChanged.connect(self._update_compare_status)
            self._history_checkboxes.append(cb)
            row.addWidget(cb)

            lbl = QLabel(f"{icon} <b>#{i+1}</b>  "
                         f"<span style='font-size:8px;color:#444;'>{short}</span>")
            lbl.setTextFormat(Qt.RichText)
            row.addWidget(lbl, 1)

            btn_rm = QPushButton("✕"); btn_rm.setFixedSize(18, 18)
            btn_rm.setStyleSheet("color:#c0392b;font-size:8px;padding:0;border:none;")
            btn_rm.setToolTip("Remover do histórico")
            btn_rm.clicked.connect(lambda _, idx=i: self._remove_from_history(idx))
            row.addWidget(btn_rm)

            w = QWidget(); w.setLayout(row)
            self._history_list_layout.addWidget(w)

        self._history_list_layout.addStretch()
        self._update_compare_status()

    def _update_compare_status(self):
        n       = len(self._plots_history)
        n_sel   = sum(1 for cb in self._history_checkboxes if cb.isChecked())
        has_any = n > 0

        if n == 0:
            self.compare_status.setText("Nenhum plot em memória")
            self.compare_status.setStyleSheet("color:#555;font-size:9px;font-style:italic;")
            self.btn_clear_history.setEnabled(False)
            self.btn_compare.setEnabled(False)
        elif n_sel < 2:
            self.compare_status.setText(
                f"📦 {n} plot(s) — marque ≥ 2 para comparar "
                f"({n_sel} selecionado{'s' if n_sel!=1 else ''})")
            self.compare_status.setStyleSheet("color:#e67e22;font-size:9px;font-weight:bold;")
            self.btn_clear_history.setEnabled(has_any)
            self.btn_compare.setEnabled(False)
        else:
            self.compare_status.setText(
                f"✅ {n_sel} de {n} selecionado(s) — pronto para comparar")
            self.compare_status.setStyleSheet("color:#1a5276;font-size:9px;font-weight:bold;")
            self.btn_clear_history.setEnabled(True)
            self.btn_compare.setEnabled(True)

    def _open_comparison(self):
        selected = [e for e, cb in zip(self._plots_history, self._history_checkboxes)
                    if cb.isChecked()]
        if len(selected) < 2:
            QMessageBox.information(self, "Seleção insuficiente",
                "Marque pelo menos 2 extrações na lista para comparar.")
            return
        dlg = PlotCompareDialog(selected, self)
        dlg.show()

    # --------------------------------------------------------------------------
    def _run(self):
        if self.radio_layer.isChecked():
            if not self._wkt_layer:
                QMessageBox.warning(self, "Feição ausente", "Capture uma feição da camada ativa."); return
            lat, lon, wkt = "", "", self._wkt_layer
            mode_label = "Camada ativa"
        elif self.radio_poly.isChecked():
            if not self._wkt_polygon:
                QMessageBox.warning(self, "Polígono ausente", "Desenhe um polígono no mapa."); return
            lat, lon, wkt = "", "", self._wkt_polygon
            mode_label = "Polígono"
        else:
            try:
                lat = float(self.lat_edit.text().replace(",", "."))
                lon = float(self.lon_edit.text().replace(",", "."))
            except ValueError:
                QMessageBox.warning(self, "Erro", "Latitude/Longitude inválida."); return
            wkt = ""
            mode_label = f"Ponto ({lat:.4f}, {lon:.4f})"
            self.coord_label.setText(f"📍 Lat {lat:.5f}  Lon {lon:.5f}")

        selected_bands = [b for b, cb in self.band_checks.items() if cb.isChecked()]
        if not selected_bands:
            QMessageBox.warning(self, "Erro", "Selecione ao menos uma banda/índice."); return

        start_str = self.date_ini.date().toString("yyyy-MM-dd")
        end_str   = self.date_fim.date().toString("yyyy-MM-dd")

        if self.date_ini.date() >= self.date_fim.date():
            QMessageBox.warning(self, "Intervalo inválido",
                f"Data inicial ({start_str}) deve ser anterior à data final ({end_str})."); return

        self._pending_label = (
            f"{mode_label}\nPeríodo: {start_str} → {end_str}\nBandas: {', '.join(selected_bands)}")

        out_file  = os.path.join(self._cache_dir, "sits_ts_plot.png")
        max_pts   = self._spin_max_pts.value() if not self.radio_point.isChecked() else 1
        args = {
            "lat": lat, "lon": lon, "start": start_str, "end": end_str,
            "bands": ",".join(selected_bands), "output": out_file,
            "wkt": wkt, "cache_dir": self._cache_dir, "max_pts": max_pts,
        }

        self.btn_run.setEnabled(False)
        self.btn_run.setText("⏳  Extraindo…")
        self.btn_cancel.setVisible(True)
        self.progress_bar.setValue(0); self.progress_bar.setVisible(True)
        self.progress_label.setText("Enviando para servidor R…")
        self.log_box.clear()
        self._log(f"▶ {mode_label}  |  {start_str} → {end_str}  |  {', '.join(selected_bands)}")

        # Limpa arquivos anteriores
        for fname in ("sits_result.txt", "sits_progress.txt", "sits_cancel.txt"):
            fp = os.path.join(self._cache_dir, fname)
            try:
                if os.path.exists(fp): os.remove(fp)
            except OSError: pass

        if self._server_ready:
            self._dispatch_request(args)
        else:
            self._pending_request = args
            self._log("⏳  Aguardando servidor R ficar pronto…")

    def _dispatch_request(self, args):
        self._server.send_request(args)
        # Start polling timer
        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(200)
            self._poll_timer.timeout.connect(self._poll_files)
        self._poll_timer.start()

    def _poll_files(self):
        """Verifica arquivos de progresso e resultado a cada 200ms."""
        # Progress
        pf = os.path.join(self._cache_dir, "sits_progress.txt")
        if os.path.exists(pf):
            try:
                with open(pf) as f: line = f.read().strip()
                parts = line.split("|", 1)
                if len(parts) == 2:
                    self._on_progress(int(parts[0]), parts[1])
            except Exception: pass

        # Result
        rf = os.path.join(self._cache_dir, "sits_result.txt")
        if os.path.exists(rf):
            try:
                with open(rf) as f: result = f.read().strip()
                os.remove(rf)
            except Exception: return
            self._poll_timer.stop()
            for fname in ("sits_progress.txt", "sits_cancel.txt"):
                try: os.remove(os.path.join(self._cache_dir, fname))
                except OSError: pass

            if result.startswith("DONE:"):
                self._on_finished(result[5:])
            elif result == "CANCELLED":
                self._on_cancelled()
            elif result.startswith("ERROR:"):
                self._on_error(result[6:])
            else:
                self._on_error(result)

    def _cancel_extraction(self):
        if self._server:
            self._server.cancel_request()
        if self._poll_timer:
            self._poll_timer.stop()
        self._on_cancelled()

    def _on_cancelled(self):
        self.btn_run.setEnabled(self._server_ready)
        self.btn_run.setText("▶  Extrair e Plotar")
        self.btn_cancel.setVisible(False)
        self.progress_bar.setVisible(False)
        self.progress_label.setText("⚠️  Cancelado pelo usuário.")
        self._log("⚠️  Extração cancelada.")

    @pyqtSlot(int, str)
    def _on_progress(self, step, label):
        self.progress_bar.setValue(step)
        self.progress_label.setText(label)

    @pyqtSlot(str)
    def _on_finished(self, plot_path):
        self.btn_run.setEnabled(True)
        self.btn_run.setText("▶  Extrair e Plotar")
        self.btn_cancel.setVisible(False)
        self.progress_bar.setValue(4)
        self.progress_label.setText("✅ Concluído!")
        self._log("✅ Extração concluída!")
        if not plot_path or not os.path.exists(plot_path):
            QMessageBox.warning(self, "Aviso", "Plot não encontrado."); return
        import shutil
        plot_id   = len(self._plots_history) + 1
        perm_path = os.path.join(self._cache_dir, f"sits_ts_plot_{plot_id}.png")
        shutil.copy2(plot_path, perm_path)
        entry = {"path": perm_path, "label": self._pending_label}
        self._plots_history.append(entry)
        self._refresh_history_list()
        dlg = PlotDialog(perm_path, self._pending_label, self)
        dlg.show()

    @pyqtSlot(str)
    def _on_error(self, msg):
        self.btn_run.setEnabled(True)
        self.btn_run.setText("▶  Extrair e Plotar")
        self.btn_cancel.setVisible(False)
        self.progress_bar.setVisible(False)
        self.progress_label.setText("")
        self._log(f"❌ {msg}")
        QMessageBox.critical(self, "Erro na extração", msg)

    def _log(self, text):
        self.log_box.append(text)
        self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())

    def cleanup(self):
        self._deactivate_map_tool()
        if self._poll_timer:
            self._poll_timer.stop()
        if self._server and self._server.isRunning():
            self._server.stop()
            self._server.wait(3000)
