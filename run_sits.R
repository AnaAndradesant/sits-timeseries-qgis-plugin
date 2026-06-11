# ==============================================================================
# run_sits.R  — chamado pelo plugin QGIS via Rscript
#
# Argumentos posicionais:
#   1: lat          (ou "" se modo polígono)
#   2: lon          (ou "" se modo polígono)
#   3: start_date
#   4: end_date
#   5: bands        (ex: "B08,NDVI,EVI,B11")
#   6: output_path  (PNG de saída)
#   7: wkt          (WKT do polígono WGS-84; "" = modo ponto)
#   8: cache_dir    (diretório para cache; "" = tempdir())
# ==============================================================================

# [OPT] Suprime warnings → menos overhead de I/O no loop de output
options(warn = -1)

suppressPackageStartupMessages({
  library("sits")
  library("tibble")
  library("sf")
  library("dplyr")
  library("ggplot2")
  library("tidyr")
})

# ==============================================================================
# 1. Leitura de argumentos
# ==============================================================================
args <- commandArgs(trailingOnly = TRUE)

if (length(args) < 6) {
  stop("Uso: Rscript run_sits.R <lat> <lon> <start> <end> <bands> <output> [wkt] [cache_dir]")
}

data_ini           <- args[3]
data_fim           <- args[4]
bandas_para_plotar <- trimws(unlist(strsplit(args[5], ",")))
output_path        <- args[6]
wkt_str            <- if (length(args) >= 7) trimws(args[7]) else ""
cache_dir_arg      <- if (length(args) >= 8) trimws(args[8]) else ""

modo_poligono <- nchar(wkt_str) > 0

if (modo_poligono) {
  cat("▶  Modo: Polígono\n")
  cat(sprintf("▶  WKT: %s\n", substr(wkt_str, 1, 80)))
} else {
  lat_target <- as.numeric(args[1])
  lon_target <- as.numeric(args[2])
  cat(sprintf("▶  Modo: Ponto — Lat=%.6f  Lon=%.6f\n", lat_target, lon_target))
}

cat(sprintf("▶  Período: %s → %s\n", data_ini, data_fim))
cat(sprintf("▶  Bandas selecionadas: %s\n", paste(bandas_para_plotar, collapse = ", ")))

# [OPT] Núcleos disponíveis (sem depender do pacote parallel)
# [OPT] Sequencial: para 1 ponto/polígono pequeno, spawn paralelo é overhead puro
n_cores <- 1L
cat("▶  Paralelismo: sequencial (otimizado para latência)\n")

# ==============================================================================
# 2. Resolução inteligente de bandas — inclui EVI
# ==============================================================================
INDICES <- list(
  NDVI = c("B08", "B04"),
  NBR  = c("B08", "B12"),
  NDWI = c("B03", "B08"),
  EVI  = c("B08", "B04", "B02")
)

TODAS_ESPECTRAIS <- c("B01","B02","B03","B04","B05","B06","B07","B08","B8A","B09","B11","B12")
bandas_diretas   <- intersect(bandas_para_plotar, TODAS_ESPECTRAIS)

deps_indices <- unique(unlist(lapply(
  intersect(bandas_para_plotar, names(INDICES)),
  function(idx) INDICES[[idx]]
)))

bands_to_fetch <- unique(c(bandas_diretas, deps_indices, "CLOUD"))

cat(sprintf("▶  Bandas a baixar do BDC: %s\n", paste(bands_to_fetch, collapse = ", ")))

# ==============================================================================
# 3. Definir ROI
# ==============================================================================
if (modo_poligono) {
  roi_sf  <- st_as_sfc(wkt_str, crs = 4326)
  roi_obj <- st_as_sf(data.frame(geom = roi_sf))
  st_geometry(roi_obj) <- "geom"
  st_crs(roi_obj) <- 4326
} else {
  roi_obj <- st_as_sf(
    data.frame(lon = lon_target, lat = lat_target),
    coords = c("lon", "lat"), crs = 4326
  )
}

# ==============================================================================
# 4. Diretório de cache
# ==============================================================================
cache_dir_use <- if (nchar(cache_dir_arg) > 0 && dir.exists(cache_dir_arg)) {
  cache_dir_arg
} else {
  file.path(tempdir(), "sits_qgis_cache")
}
dir.create(cache_dir_use, showWarnings = FALSE, recursive = TRUE)

bbox_str <- if (modo_poligono) {
  bb <- st_bbox(roi_obj)
  sprintf("%.3f_%.3f_%.3f_%.3f", bb["xmin"], bb["ymin"], bb["xmax"], bb["ymax"])
} else {
  sprintf("%.4f_%.4f", lat_target, lon_target)
}

cache_key <- paste(
  "S2_16D",
  paste(sort(bands_to_fetch), collapse="-"),
  bbox_str,
  gsub("-", "", data_ini),
  gsub("-", "", data_fim),
  sep = "_"
)
cache_key <- gsub("[^a-zA-Z0-9_]", "_", cache_key)

cache_file        <- file.path(cache_dir_use, paste0(cache_key, ".rds"))

series_cache_key  <- paste(cache_key, paste(sort(bandas_para_plotar), collapse="-"), sep="_SER_")
series_cache_key  <- gsub("[^a-zA-Z0-9_]", "_", series_cache_key)
series_cache_file <- file.path(cache_dir_use, paste0(series_cache_key, ".rds"))

CACHE_MAX_HOURS <- 48

# ==============================================================================
# 5. Tentar carregar série do cache
# ==============================================================================
series_suave <- NULL

if (file.exists(series_cache_file)) {
  age_hours <- as.numeric(difftime(Sys.time(), file.mtime(series_cache_file), units = "hours"))
  if (age_hours < CACHE_MAX_HOURS) {
    cat(sprintf("▶  Série temporal carregada do cache (%.1fh atrás) ⚡\n", age_hours))
    tryCatch({
      series_suave <- readRDS(series_cache_file)
    }, error = function(e) {
      cat("⚠  Cache de série corrompido, recalculando…\n")
      series_suave <<- NULL
    })
  }
}

# ==============================================================================
# 6. Se série não está em cache: cubo → extração → índices → filtro
# ==============================================================================
if (is.null(series_suave)) {

  # --- 6a. Cubo BDC ----------------------------------------------------------
  cube_bdc <- NULL

  if (file.exists(cache_file)) {
    age_hours <- as.numeric(difftime(Sys.time(), file.mtime(cache_file), units = "hours"))
    if (age_hours < CACHE_MAX_HOURS) {
      cat(sprintf("▶  Cubo BDC carregado do cache (%.1fh atrás)…\n", age_hours))
      tryCatch({
        cube_bdc <- readRDS(cache_file)
      }, error = function(e) {
        cat("⚠  Cache corrompido, recriando cubo…\n")
        cube_bdc <<- NULL
      })
    }
  }

  if (is.null(cube_bdc)) {
    cat("▶  Criando cubo BDC (query STAC)…\n")
    cube_bdc <- sits_cube(
      source     = "BDC",
      collection = "SENTINEL-2-16D",
      bands      = bands_to_fetch,
      roi        = roi_obj,
      start_date = data_ini,
      end_date   = data_fim,
      progress   = FALSE
    )
    saveRDS(cube_bdc, cache_file)
    cat("▶  Cubo salvo no cache.\n")
  }

  # --- 6b. Amostras ----------------------------------------------------------
  cat("▶  Extraindo série temporal…\n")

  if (modo_poligono) {
    poly_geom <- st_as_sfc(wkt_str, crs = 4326)

    utm_crs   <- st_crs(paste0("+proj=utm +zone=",
                  floor((st_coordinates(st_centroid(poly_geom))[1] + 180) / 6) + 1,
                  " +datum=WGS84"))
    area_km2  <- as.numeric(st_area(st_transform(poly_geom, utm_crs))) / 1e6

    grid_n <- if      (area_km2 <   1) c(2L, 2L)
              else if (area_km2 <  10) c(3L, 3L)
              else if (area_km2 < 100) c(4L, 4L)
              else                     c(5L, 5L)

    cat(sprintf("▶  Área do polígono: %.2f km²  →  grade %dx%d\n",
                area_km2, grid_n[1], grid_n[2]))

    grid_pts <- st_make_grid(poly_geom, n = grid_n, what = "centers")
    dentro   <- st_within(grid_pts, poly_geom, sparse = FALSE)[, 1]
    grid_pts <- grid_pts[dentro]

    if (length(grid_pts) == 0) {
      grid_pts <- st_centroid(poly_geom)
      cat("⚠  Grade vazia, usando centroide do polígono.\n")
    }

    coords <- st_coordinates(grid_pts)
    amostras <- tibble(
      longitude  = coords[, 1],
      latitude   = coords[, 2],
      start_date = as.Date(data_ini),
      end_date   = as.Date(data_fim),
      label      = "Poligono_Media"
    )
    cat(sprintf("▶  Amostrado %d ponto(s) dentro do polígono\n", nrow(amostras)))
  } else {
    amostras <- tibble(
      longitude  = lon_target,
      latitude   = lat_target,
      start_date = as.Date(data_ini),
      end_date   = as.Date(data_fim),
      label      = "Ponto_Analise"
    )
  }

  series_raw <- tryCatch(
    sits_get_data(cube = cube_bdc, samples = amostras, multicores = n_cores, progress = FALSE),
    error = function(e) {
      cat("⚠  multicores não suportado nesta versão do sits, usando modo sequencial.\n")
      sits_get_data(cube = cube_bdc, samples = amostras, progress = FALSE)
    }
  )

  # --- 6c. Índices -----------------------------------------------------------
  cat("▶  Calculando índices…\n")

  indices_solicitados <- intersect(bandas_para_plotar, names(INDICES))

  if (length(indices_solicitados) > 0) {
    exprs_list <- list()
    if ("NDVI" %in% indices_solicitados)
      exprs_list$NDVI <- quote((B08 - B04) / (B08 + B04))
    if ("NBR"  %in% indices_solicitados)
      exprs_list$NBR  <- quote((B08 - B12) / (B08 + B12))
    if ("NDWI" %in% indices_solicitados)
      exprs_list$NDWI <- quote((B03 - B08) / (B03 + B08))
    if ("EVI"  %in% indices_solicitados)
      exprs_list$EVI  <- quote(2.5 * (B08 - B04) / (B08 + 6 * B04 - 7.5 * B02 + 1))

    series_com_indices <- do.call(sits_apply, c(list(series_raw), exprs_list))
  } else {
    series_com_indices <- series_raw
  }

  # --- 6d. Despiking ---------------------------------------------------------
  despike_ts <- function(ts, threshold = 3.0) {
    band_cols <- setdiff(names(ts), "Index")
    for (col in band_cols) {
      x       <- ts[[col]]
      med     <- median(x, na.rm = TRUE)
      mad_val <- mad(x, constant = 1.4826, na.rm = TRUE)
      if (is.na(mad_val) || mad_val < 1e-6) next
      outliers        <- abs(x - med) > threshold * mad_val
      ts[[col]][outliers] <- NA
      if (any(is.na(ts[[col]]))) {
        idx          <- seq_along(ts[[col]])
        ts[[col]]    <- approx(idx, ts[[col]], idx, rule = 2)$y
      }
    }
    ts
  }

  n_spikes_total <- 0L
  series_com_indices$time_series <- lapply(
    series_com_indices$time_series,
    function(ts) {
      ts_limpo <- despike_ts(ts)
      n_spikes_total <<- n_spikes_total +
        sum(sapply(setdiff(names(ts), "Index"),
                   function(col) sum(is.na(ts_limpo[[col]]) != is.na(ts[[col]]))))
      ts_limpo
    }
  )
  if (n_spikes_total > 0L)
    cat(sprintf("⚡  Despiking: %d valor(es) de nuvem removidos e interpolados\n", n_spikes_total))

  # --- 6e. Filtro SG (janela=13 para suavização mais eficaz) ----------------
  n_obs  <- nrow(series_com_indices$time_series[[1]])
  sg_len <- min(9L, n_obs)            # janela menor = filtro mais rápido, suavização mantida
  if (sg_len %% 2 == 0) sg_len <- sg_len - 1L
  sg_len <- max(sg_len, 3L)

  cat(sprintf("▶  Filtro Savitzky-Golay (janela=%d)…\n", sg_len))

  series_suave <- tryCatch(
    sits_filter(series_com_indices, filter = sits_sgolay(length = sg_len, order = 2)),
    error = function(e) {
      cat("⚠  Filtro SG falhou, prosseguindo sem suavização.\n")
      series_com_indices
    }
  )

  tryCatch(
    saveRDS(series_suave, series_cache_file),
    error = function(e) cat("⚠  Não foi possível salvar cache da série.\n")
  )
  cat("▶  Série salva no cache.\n")
}

# ==============================================================================
# 7. Agregar séries
# ==============================================================================
cat("▶  Gerando gráfico…\n")

if (modo_poligono && nrow(series_suave) > 1) {
  ts_list   <- lapply(seq_len(nrow(series_suave)), function(i) series_suave$time_series[[i]])
  ts_joined <- bind_rows(ts_list)
  band_cols  <- setdiff(names(ts_joined), "Index")
  dados_plot <- ts_joined %>%
    group_by(Index) %>%
    summarise(across(all_of(band_cols), ~mean(.x, na.rm = TRUE)), .groups = "drop")
} else {
  dados_plot <- series_suave$time_series[[1]]
}

# ==============================================================================
# 8. Montar dados para o plot
# ==============================================================================
dados_long <- dados_plot %>%
  select(Index, any_of(bandas_para_plotar)) %>%
  pivot_longer(cols = -Index, names_to = "Band", values_to = "Value")

anos_presentes <- unique(format(dados_plot$Index, "%Y"))
linhas_anos    <- as.Date(paste0(anos_presentes, "-08-01"))

fmt_eixo <- function(breaks) {
  vapply(breaks, function(d) {
    if (is.na(d)) return("")
    if (as.integer(format(d, "%m")) == 1L) format(d, "%b\n\n%Y") else format(d, "%b")
  }, character(1))
}

cores_todas <- c(
  "B01"  = "#aec7e8", "B02"  = "#1f77b4", "B03"  = "#98df8a",
  "B04"  = "#d62728", "B05"  = "#ff9896", "B06"  = "#e377c2",
  "B07"  = "#c5b0d5", "B08"  = "#5c7cb0", "B8A"  = "#9467bd",
  "B09"  = "#8c564b", "B11"  = "#f2a641", "B12"  = "#FF4500",
  "NDVI" = "#2ca02c", "NBR"  = "#7f7f7f", "NDWI" = "#17becf",
  "EVI"  = "#006400"
)

n_pts_poligono <- if (modo_poligono) nrow(series_suave) else 0L

subtitle_txt <- if (modo_poligono) {
  sprintf("Polígono (média de %d pontos)  |  %s → %s",
          n_pts_poligono, data_ini, data_fim)
} else {
  sprintf("Lat: %.6f  |  Lon: %.6f  |  %s → %s",
          lat_target, lon_target, data_ini, data_fim)
}

# ==============================================================================
# 9. Plot
# ==============================================================================
grafico <- ggplot(dados_long, aes(x = Index, y = Value, color = Band)) +
  geom_line(linewidth = 1.5) +
  scale_color_manual(values = cores_todas) +
  geom_vline(
    xintercept = linhas_anos,
    linetype = "dashed", linewidth = 0.8, color = "black", alpha = 0.6
  ) +
  scale_x_date(date_breaks = "1 month", labels = fmt_eixo) +
  scale_y_continuous(
    limits = function(x) c(min(x[1], 0), 1),
    breaks = seq(-1, 1, by = 0.2)
  ) +
  labs(
    title    = "Time Series — Sentinel-2 / BDC (Savitzky-Golay)",
    subtitle = subtitle_txt,
    x = "", y = ""
  ) +
  theme_minimal() +
  theme(
    plot.title       = element_text(hjust = 0, face = "bold", size = 16, margin = margin(b = 4)),
    plot.subtitle    = element_text(hjust = 0, color = "grey40", size = 10, margin = margin(b = 16)),
    axis.text.x      = element_text(size = 10, vjust = 1),
    axis.text.y      = element_text(size = 10),
    legend.position  = "right",
    legend.title     = element_blank(),
    legend.text      = element_text(size = 12),
    panel.grid.minor   = element_blank(),
    panel.grid.major.x = element_blank()
  )

png(filename = output_path, width = 1400, height = 600, res = 90, type = "cairo-png")
print(grafico)
dev.off()

cat(sprintf("✅ OK - Plot salvo em: %s\n", output_path))
message("✅ Série temporal extraída com sucesso!")
