# ==============================================================================
# run_sits_server.R  —  Servidor R persistente (IPC via arquivos temporários)
#
# Protocolo (sem stdin):
#   Arg 1 : diretório de trabalho compartilhado com o Python
#   Arquivos criados pelo Python:
#     sits_req.txt     → campos separados por \x1F; "QUIT" para encerrar
#     sits_cancel.txt  → criado para cancelar a extração em curso
#   Arquivos criados pelo R:
#     sits_progress.txt → "passo|rótulo" (sobrescrito a cada etapa)
#     sits_result.txt   → "DONE:<path>", "ERROR:<msg>" ou "CANCELLED"
# ==============================================================================
options(warn = -1)
SEP <- "\x1f"

args    <- commandArgs(trailingOnly = TRUE)
WORKDIR <- if (length(args) >= 1 && nchar(args[1]) > 0) args[1] else {
  d <- file.path(tempdir(), "sits_qgis_cache"); dir.create(d, showWarnings=FALSE); d
}
dir.create(WORKDIR, showWarnings = FALSE, recursive = TRUE)

REQ_FILE      <- file.path(WORKDIR, "sits_req.txt")
CANCEL_FILE   <- file.path(WORKDIR, "sits_cancel.txt")
PROGRESS_FILE <- file.path(WORKDIR, "sits_progress.txt")
RESULT_FILE   <- file.path(WORKDIR, "sits_result.txt")

# Limpa resíduos de sessões anteriores
for (f in c(REQ_FILE, CANCEL_FILE, PROGRESS_FILE, RESULT_FILE))
  tryCatch(if (file.exists(f)) file.remove(f), error = function(e) NULL)

write_progress <- function(step, label) {
  writeLines(paste(step, label, sep = "|"), PROGRESS_FILE)
  cat(sprintf("▶  [%d/4] %s\n", step, label)); flush(stdout())
}

write_result <- function(text) {
  tmp <- paste0(RESULT_FILE, ".tmp")
  writeLines(text, tmp)
  file.rename(tmp, RESULT_FILE)
}

check_cancel <- function() {
  if (file.exists(CANCEL_FILE)) stop("CANCELLED_BY_USER")
}

# ==============================================================================
cat("▶  Carregando pacotes R (só na primeira vez)…\n"); flush(stdout())
suppressPackageStartupMessages({
  library("sits"); library("tibble"); library("sf")
  library("dplyr"); library("ggplot2"); library("tidyr")
})
cat("SERVER_READY\n"); flush(stdout())

# ==============================================================================
INDICES          <- list(NDVI=c("B08","B04"), NBR=c("B08","B12"),
                         NDWI=c("B03","B08"), EVI=c("B08","B04","B02"))
TODAS_ESPECTRAIS <- c("B01","B02","B03","B04","B05","B06",
                      "B07","B08","B8A","B09","B11","B12")
CACHE_MAX_HOURS  <- 48

despike_ts <- function(ts, threshold = 3.0) {
  band_cols <- setdiff(names(ts), "Index")
  for (col in band_cols) {
    x   <- ts[[col]]; med <- median(x, na.rm=TRUE)
    mad_val <- mad(x, constant=1.4826, na.rm=TRUE)
    if (is.na(mad_val) || mad_val < 1e-6) next
    outliers <- abs(x - med) > threshold * mad_val
    ts[[col]][outliers] <- NA
    if (any(is.na(ts[[col]]))) {
      idx <- seq_along(ts[[col]])
      ts[[col]] <- approx(idx, ts[[col]], idx, rule=2)$y
    }
  }
  ts
}

run_extraction <- function(lat_str, lon_str, data_ini, data_fim,
                            bands_str, output_path, wkt_str, cache_dir_arg,
                            max_pts_str = "9") {

  check_cancel()
  bandas_para_plotar <- trimws(unlist(strsplit(bands_str, ",")))
  modo_poligono      <- nchar(trimws(wkt_str)) > 0
  cache_dir_use      <- if (nchar(trimws(cache_dir_arg)) > 0 && dir.exists(cache_dir_arg))
                          cache_dir_arg else WORKDIR

  write_progress(1, "Preparando cubo BDC…")

  bandas_diretas <- intersect(bandas_para_plotar, TODAS_ESPECTRAIS)
  deps_indices   <- unique(unlist(lapply(
    intersect(bandas_para_plotar, names(INDICES)), function(i) INDICES[[i]])))
  bands_to_fetch <- unique(c(bandas_diretas, deps_indices, "CLOUD"))
  cat(sprintf("▶  Bandas: %s\n", paste(bands_to_fetch, collapse=", "))); flush(stdout())

  if (modo_poligono) {
    roi_sf  <- st_as_sfc(wkt_str, crs=4326)
    roi_obj <- st_as_sf(data.frame(geom=roi_sf)); st_geometry(roi_obj) <- "geom"; st_crs(roi_obj) <- 4326
  } else {
    lat_target <- as.numeric(lat_str); lon_target <- as.numeric(lon_str)
    roi_obj <- st_as_sf(data.frame(lon=lon_target, lat=lat_target), coords=c("lon","lat"), crs=4326)
  }

  bbox_str <- if (modo_poligono) {
    bb <- st_bbox(roi_obj); sprintf("%.3f_%.3f_%.3f_%.3f", bb["xmin"],bb["ymin"],bb["xmax"],bb["ymax"])
  } else sprintf("%.4f_%.4f", lat_target, lon_target)

  cache_key <- gsub("[^a-zA-Z0-9_]","_", paste(
    "S2_16D", paste(sort(bands_to_fetch),collapse="-"), bbox_str,
    gsub("-","",data_ini), gsub("-","",data_fim), sep="_"))
  cache_file        <- file.path(cache_dir_use, paste0(cache_key,".rds"))
  series_cache_key  <- gsub("[^a-zA-Z0-9_]","_",
    paste(cache_key, paste(sort(bandas_para_plotar),collapse="-"), sep="_SER_"))
  series_cache_file <- file.path(cache_dir_use, paste0(series_cache_key,".rds"))

  # Cache de série
  series_suave <- NULL
  if (file.exists(series_cache_file)) {
    age_h <- as.numeric(difftime(Sys.time(), file.mtime(series_cache_file), units="hours"))
    if (age_h < CACHE_MAX_HOURS) {
      cat(sprintf("▶  Série carregada do cache (%.1fh) ⚡\n", age_h)); flush(stdout())
      tryCatch({ series_suave <- readRDS(series_cache_file) },
               error = function(e) { series_suave <<- NULL })
    }
  }

  if (is.null(series_suave)) {
    check_cancel()

    # Cubo BDC
    cube_bdc <- NULL
    if (file.exists(cache_file)) {
      age_h <- as.numeric(difftime(Sys.time(), file.mtime(cache_file), units="hours"))
      if (age_h < CACHE_MAX_HOURS) {
        cat(sprintf("▶  Cubo carregado do cache (%.1fh) ⚡\n", age_h)); flush(stdout())
        tryCatch({ cube_bdc <- readRDS(cache_file) }, error = function(e) { cube_bdc <<- NULL })
      }
    }
    if (is.null(cube_bdc)) {
      write_progress(1, "Consultando STAC BDC…")
      cube_bdc <- sits_cube(source="BDC", collection="SENTINEL-2-16D",
        bands=bands_to_fetch, roi=roi_obj,
        start_date=data_ini, end_date=data_fim, progress=FALSE)
      saveRDS(cube_bdc, cache_file)
      cat("▶  Cubo salvo no cache.\n"); flush(stdout())
    }

    check_cancel()
    write_progress(2, "Extraindo série temporal…")

    if (modo_poligono) {
      poly_geom <- st_as_sfc(wkt_str, crs=4326)
      geom_type <- as.character(st_geometry_type(poly_geom))

      # Função auxiliar: amostra um polígono simples
      # • Grade inicial escala com sqrt(área): feições grandes recebem mais pontos
      # • Loop de adensamento garante MIN_PTS mesmo em formas irregulares/alongadas
      # • Limite MAX_PTS evita downloads desnecessários em feições gigantes
      MIN_PTS  <- 5L
      MAX_PTS  <- max(MIN_PTS, as.integer(tryCatch(as.integer(max_pts_str), error=function(e) 9L)))
      cat(sprintf("▶  Pontos máx.: %d\n", MAX_PTS)); flush(stdout())

      sample_one_polygon <- function(geom) {
        utm_c  <- st_crs(paste0("+proj=utm +zone=",
          floor((st_coordinates(st_centroid(geom))[1]+180)/6)+1," +datum=WGS84"))
        area_j <- as.numeric(st_area(st_transform(geom, utm_c))) / 1e6

        # Tamanho inicial da grade: cresce com sqrt(área), mínimo 4, máximo 15
        n_base <- max(4L, min(15L, as.integer(ceiling(sqrt(area_j) + 2L))))

        coords <- NULL
        for (n_try in seq(n_base, n_base + 14L, by = 2L)) {
          gpts <- st_make_grid(geom, n = c(n_try, n_try), what = "centers")
          den  <- st_within(gpts, geom, sparse = FALSE)[,1]
          gpts <- gpts[den]

          if (length(gpts) >= MIN_PTS) {
            # Limita ao máximo subamostrado regularmente
            if (length(gpts) > MAX_PTS) {
              idx  <- round(seq(1, length(gpts), length.out = MAX_PTS))
              gpts <- gpts[idx]
            }
            coords <- st_coordinates(gpts)
            break
          }
        }

        # Fallback final: centróide
        if (is.null(coords) || nrow(coords) == 0)
          coords <- st_coordinates(st_centroid(geom))

        coords
      }

      if (grepl("MULTIPOLYGON", geom_type, ignore.case=TRUE)) {
        # Divide em partes e amostra cada uma individualmente
        partes <- st_cast(st_as_sf(poly_geom), "POLYGON")
        cat(sprintf("▶  MultiPolygon: %d parte(s) detectada(s)\n", nrow(partes))); flush(stdout())
        coords_list <- lapply(seq_len(nrow(partes)), function(j) {
          sample_one_polygon(st_geometry(partes[j,]))
        })
        coords <- do.call(rbind, coords_list)
      } else {
        coords <- sample_one_polygon(poly_geom)
      }

      # Área total para o log
      utm_crs  <- st_crs(paste0("+proj=utm +zone=",
        floor((st_coordinates(st_centroid(poly_geom))[1]+180)/6)+1," +datum=WGS84"))
      area_km2 <- as.numeric(st_area(st_transform(poly_geom, utm_crs))) / 1e6

      amostras <- tibble(longitude=coords[,1], latitude=coords[,2],
                         start_date=as.Date(data_ini), end_date=as.Date(data_fim), label="Poligono_Media")
      cat(sprintf("▶  %.2f km²  →  %d ponto(s) amostrado(s)\n", area_km2, nrow(amostras))); flush(stdout())
    } else {
      amostras <- tibble(longitude=lon_target, latitude=lat_target,
                         start_date=as.Date(data_ini), end_date=as.Date(data_fim), label="Ponto_Analise")
    }

    series_raw <- tryCatch(
      sits_get_data(cube=cube_bdc, samples=amostras, multicores=1L, progress=FALSE),
      error = function(e) sits_get_data(cube=cube_bdc, samples=amostras, progress=FALSE))

    check_cancel()
    write_progress(3, "Calculando índices e filtrando…")

    indices_sol <- intersect(bandas_para_plotar, names(INDICES))
    if (length(indices_sol)>0) {
      exprs_list <- list()
      if ("NDVI" %in% indices_sol) exprs_list$NDVI <- quote((B08-B04)/(B08+B04))
      if ("NBR"  %in% indices_sol) exprs_list$NBR  <- quote((B08-B12)/(B08+B12))
      if ("NDWI" %in% indices_sol) exprs_list$NDWI <- quote((B03-B08)/(B03+B08))
      if ("EVI"  %in% indices_sol) exprs_list$EVI  <- quote(2.5*(B08-B04)/(B08+6*B04-7.5*B02+1))
      series_com_indices <- do.call(sits_apply, c(list(series_raw), exprs_list))
    } else series_com_indices <- series_raw

    n_spikes <- 0L
    series_com_indices$time_series <- lapply(series_com_indices$time_series, function(ts) {
      ts_l <- despike_ts(ts)
      n_spikes <<- n_spikes + sum(sapply(setdiff(names(ts),"Index"),
        function(col) sum(is.na(ts_l[[col]]) != is.na(ts[[col]]))))
      ts_l
    })
    if (n_spikes>0) cat(sprintf("⚡ Despiking: %d valor(es) removidos\n", n_spikes)); flush(stdout())

    n_obs  <- nrow(series_com_indices$time_series[[1]])
    sg_len <- min(9L, n_obs); if (sg_len%%2==0) sg_len <- sg_len-1L; sg_len <- max(sg_len,3L)
    series_suave <- tryCatch(
      sits_filter(series_com_indices, filter=sits_sgolay(length=sg_len, order=2)),
      error = function(e) series_com_indices)

    tryCatch(saveRDS(series_suave, series_cache_file), error=function(e) NULL)
  }

  check_cancel()
  write_progress(4, "Gerando gráfico…")

  if (modo_poligono && nrow(series_suave)>1) {
    ts_list   <- lapply(seq_len(nrow(series_suave)), function(i) series_suave$time_series[[i]])
    dados_plot <- bind_rows(ts_list) %>% group_by(Index) %>%
      summarise(across(all_of(setdiff(names(ts_list[[1]]),"Index")), ~mean(.x,na.rm=TRUE)), .groups="drop")
  } else dados_plot <- series_suave$time_series[[1]]

  dados_long <- dados_plot %>% select(Index, any_of(bandas_para_plotar)) %>%
    pivot_longer(cols=-Index, names_to="Band", values_to="Value")

  anos_presentes <- unique(format(dados_plot$Index,"%Y"))
  linhas_anos    <- as.Date(paste0(anos_presentes,"-08-01"))

  fmt_eixo <- function(breaks) vapply(breaks, function(d) {
    if (is.na(d)) return("")
    if (as.integer(format(d,"%m"))==1L) format(d,"%b\n\n%Y") else format(d,"%b")
  }, character(1))

  cores_todas <- c(
    B01="#aec7e8",B02="#1f77b4",B03="#98df8a",B04="#d62728",B05="#ff9896",
    B06="#e377c2",B07="#c5b0d5",B08="#5c7cb0",B8A="#9467bd",B09="#8c564b",
    B11="#f2a641",B12="#FF4500",NDVI="#2ca02c",NBR="#7f7f7f",NDWI="#17becf",EVI="#006400")

  subtitle_txt <- if (modo_poligono)
    sprintf("Polígono (média de %d pontos)  |  %s → %s", nrow(series_suave), data_ini, data_fim)
  else sprintf("Lat: %.6f  |  Lon: %.6f  |  %s → %s", lat_target, lon_target, data_ini, data_fim)

  grafico <- ggplot(dados_long, aes(x=Index, y=Value, color=Band)) +
    geom_line(linewidth=1.5) +
    scale_color_manual(values=cores_todas) +
    geom_vline(xintercept=linhas_anos, linetype="dashed", linewidth=0.8, color="black", alpha=0.6) +
    scale_x_date(date_breaks="1 month", labels=fmt_eixo) +
    scale_y_continuous(limits=function(x) c(min(x[1],0),1), breaks=seq(-1,1,by=0.2)) +
    labs(title="Time Series — Sentinel-2 / BDC (Savitzky-Golay)", subtitle=subtitle_txt, x="", y="") +
    theme_minimal() +
    theme(plot.title=element_text(hjust=0,face="bold",size=16,margin=margin(b=4)),
          plot.subtitle=element_text(hjust=0,color="grey40",size=10,margin=margin(b=16)),
          axis.text.x=element_text(size=10,vjust=1), axis.text.y=element_text(size=10),
          legend.position="right", legend.title=element_blank(), legend.text=element_text(size=12),
          panel.grid.minor=element_blank(), panel.grid.major.x=element_blank())

  png(filename=output_path, width=1400, height=600, res=90, type="cairo-png")
  print(grafico); dev.off()

  cat(sprintf("✅ Plot: %s\n", output_path)); flush(stdout())
  write_result(paste0("DONE:", output_path))
}

# ==============================================================================
# Loop do servidor — polling de arquivo (sem stdin, funciona em qualquer SO)
# ==============================================================================
cat(sprintf("▶  Servidor aguardando requests em: %s\n", WORKDIR)); flush(stdout())

repeat {
  if (!file.exists(REQ_FILE)) { Sys.sleep(0.1); next }

  lines <- tryCatch(readLines(REQ_FILE), error = function(e) character(0))
  if (length(lines) == 0) { Sys.sleep(0.05); next }
  tryCatch(file.remove(REQ_FILE), error = function(e) NULL)

  # Remove cancel/result anteriores
  for (f in c(CANCEL_FILE, RESULT_FILE, PROGRESS_FILE))
    tryCatch(if (file.exists(f)) file.remove(f), error = function(e) NULL)

  line <- trimws(lines[1])
  if (line == "QUIT") break
  if (nchar(line) == 0) next

  parts <- strsplit(line, SEP, fixed=TRUE)[[1]]
  while (length(parts) < 8) parts <- c(parts, "")

  tryCatch({
    run_extraction(parts[1],parts[2],parts[3],parts[4],parts[5],parts[6],parts[7],parts[8],
                   if (length(parts)>=9) parts[9] else "9")
  }, error = function(e) {
    msg <- conditionMessage(e)
    if (grepl("CANCELLED_BY_USER", msg)) {
      cat("⚠  Extração cancelada.\n"); flush(stdout())
      write_result("CANCELLED")
    } else {
      cat(paste0("REQUEST_ERROR:", msg, "\n")); flush(stdout())
      write_result(paste0("ERROR:", msg))
    }
  })
}
