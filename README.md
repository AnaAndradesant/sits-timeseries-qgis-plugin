# SITS Time Series Extractor for QGIS

A QGIS plugin for extracting and visualizing Sentinel-2 time series from Brazil Data Cube (BDC) using the SITS package in R.

## Features

- Point extraction by map click
- Manual coordinate input
- Polygon extraction
- Extraction from selected features
- Sentinel-2 spectral bands:
  - B03 (Green)
  - B04 (Red)
  - B08 (NIR)
  - B11 (SWIR1)
  - B12 (SWIR2)
- Spectral indices:
  - NDVI
  - NBR
  - NDWI
  - EVI
- Savitzky-Golay smoothing
- Time interval selection
- Comparison between multiple extractions
- Interactive visualization

## Requirements

### QGIS

- QGIS 3.16 or newer

### R

- R 4.3 or newer

Required packages:

```r
install.packages(c(
  "argparse",
  "tibble",
  "sf",
  "dplyr",
  "ggplot2",
  "tidyr",
  "scales"
))

options(
  repos = c(
    SITS = "https://e-sensing.r-universe.dev",
    CRAN = "https://cloud.r-project.org/"
  )
)

install.packages("sits")
```

## Installation

### Option 1 - Install from ZIP

1. Download the latest release.
2. Open QGIS.
3. Plugins → Manage and Install Plugins.
4. Install from ZIP.
5. Select the downloaded file.

## Usage

1. Open the plugin from the QGIS toolbar.
2. Select a location by:
   - Clicking on the map
   - Entering coordinates manually
   - Selecting a polygon/feature
3. Choose the date range.
4. Select the desired bands or indices.
5. Verify the Rscript path.
6. Click Extract and Plot.

## Brazil Data Cube Authentication

The plugin requires a valid Brazil Data Cube access key.

In R:

```r
Sys.setenv(BDC_ACCESS_KEY = "YOUR_KEY")
```

Or add to your `.Renviron`:

```text
BDC_ACCESS_KEY=YOUR_KEY
```

Access keys can be obtained from:

https://brazildatacube.dpi.inpe.br/

## Screenshots

Add screenshots here.

## Author

Ana Carolina Santos de Andrade

Technologist in Geoprocessing – FATEC Jacareí

## License

MIT License
