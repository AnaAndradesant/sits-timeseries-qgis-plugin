# 📈 SITS Time Series Extractor

Plugin QGIS para extração e visualização de séries temporais Sentinel-2 via [Brazil Data Cube (BDC)](https://brazildatacube.org/), integrado ao pacote R [`sits`](https://github.com/e-sensing/sits).

---

## Sumário

- [Visão Geral](#visão-geral)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Interface](#interface)
- [Modos de Amostragem](#modos-de-amostragem)
- [Bandas e Índices Disponíveis](#bandas-e-índices-disponíveis)
- [Cache](#cache)
- [Comparação de Séries](#comparação-de-séries)
- [Arquitetura Técnica](#arquitetura-técnica)
- [Troubleshooting](#troubleshooting)

---

## Visão Geral

O plugin conecta o QGIS ao Brazil Data Cube para extrair séries temporais do satélite **Sentinel-2** (coleção `SENTINEL-2-16D`), aplicar filtro **Savitzky-Golay** para suavização e gerar gráficos prontos para análise ou publicação.

Principais funcionalidades:

- Extração por **ponto**, **polígono desenhado** ou **feição da camada ativa**
- Suporte a **MultiPolygon** (dissolve de múltiplas feições)
- Cálculo automático de índices espectrais: NDVI, NBR, NDWI, EVI
- Remoção de pixels de nuvem via **despiking MAD** antes da suavização
- **Servidor R persistente** — pacotes carregados uma única vez, extrações subsequentes sem overhead de startup
- **Botão Cancelar** — interrompe a extração em qualquer etapa
- **Cache de 48h** para cubo BDC e série temporal
- Visualizador com zoom, salvar PNG e **comparação em grade** de N séries lado a lado
- Amostragem adaptativa com **spinbox de pontos** configurável pelo usuário

---

## Requisitos

### QGIS
- QGIS ≥ 3.16 (testado em 3.40 Bratislava)

### R e pacotes
- R ≥ 4.1
- Pacotes obrigatórios:

```r
install.packages(c("sits", "sf", "dplyr", "ggplot2", "tidyr", "tibble"))
```

## Instalação

1. Baixe o arquivo `.zip` do plugin (releases ou clone do repositório)
2. No QGIS: **Plugins → Gerenciar e Instalar Complementos → Instalar a partir de ZIP**
3. Selecione o arquivo `.zip` e clique em **Instalar Complemento**
4. Ative o plugin na lista de complementos instalados

O painel aparecerá automaticamente na barra lateral do QGIS.

---

## Interface

```
┌─────────────────────────────────────┐
│  📈 SITS Time Series · BDC/Sentinel-2│
├─────────────────────────────────────┤
│  🗺 Modo de Amostragem              │
│    ○ Ponto  ● Polígono  ○ Camada    │
│    [Desenhar polígono no mapa]       │
│    Pontos amostrados: [9 ▲▼]        │
│    ⚡ 3-9=rápido | 🎯 10-20=preciso │
├─────────────────────────────────────┤
│  📅 Intervalo de Datas              │
│    Inicial: [2023-08-01]             │
│    Final:   [2025-07-31]             │
├─────────────────────────────────────┤
│  📊 Bandas / Índices                │
│    □B01 □B02 □B03 ☑B04 □B05 □B06    │
│    □B07 ☑B08 □B8A □B09 ☑B11 □B12   │
│    ☑NDVI □NBR □NDWI □EVI            │
├─────────────────────────────────────┤
│  🔄 Comparação de Séries            │
│    [☑] #1 Ponto | 2023-08-01→...    │
│    [☑] #2 Polígono | 2024-01-01→... │
│    [📊 Comparar selecionados]       │
├─────────────────────────────────────┤
│  ⚙ Configurações do R               │
│    ✅ Servidor R pronto             │
│    [Caminho do Rscript.exe]         │
├─────────────────────────────────────┤
│  [▶ Extrair e Plotar] [⏹ Cancelar] │
│  ████████░░ 3/4 — Calculando...     │
└─────────────────────────────────────┘
```

---

## Modos de Amostragem

### 📍 Ponto
Insira latitude/longitude manualmente ou clique no botão **"Capturar ponto no mapa"** e clique diretamente na tela do QGIS. Extrai 1 pixel na coordenada exata.

### 🔷 Polígono
Desenhe um polígono diretamente na tela do QGIS:
- **Botão esquerdo** — adiciona vértice
- **Botão direito / Enter** — fecha o polígono
- **Esc** — cancela o desenho
- **Backspace** — desfaz o último vértice

O plugin cria uma grade de pontos adaptativa dentro do polígono e calcula a **média espacial** das séries.

**Amostragem adaptativa:**

| Área | Grade inicial | Comportamento |
|---|---|---|
| < 1 km² | 4×4 | Adensa até ≥ min. pontos |
| 1–10 km² | 4–5×4–5 | Adensa se necessário |
| 10–100 km² | 5–12×5–12 | Cap em `max_pts` |
| > 100 km² | Até 15×15 | Cap em `max_pts` |

O spinbox **"Pontos amostrados"** controla o máximo de pontos (padrão: 9). Valores maiores aumentam a representatividade mas também o tempo de download.

> **Sobre resolução:** cada ponto amostrado corresponde a **1 pixel** do Sentinel-2 (10m para B02/B03/B04/B08, 20m para B11/B12/etc.).

### 🗂 Camada ativa
Selecione uma feição na camada vetorial ativa (segmentos, polígonos de classificação, etc.) e clique em **"Capturar feição selecionada"**. Funciona com polígonos simples e também com **múltiplas feições selecionadas** (com opção de dissolve para MultiPolygon).

> Quando o dissolve gera um MultiPolygon, cada parte é amostrada individualmente com sua própria grade, e os pontos são combinados.

---

## Bandas e Índices Disponíveis

| Código | Descrição | Resolução |
|---|---|---|
| B01 | Aerossóis costeiros | 60m |
| B02 | Azul | 10m |
| B03 | Verde | 10m |
| B04 | Vermelho | 10m |
| B05 | Red Edge 1 | 20m |
| B06 | Red Edge 2 | 20m |
| B07 | Red Edge 3 | 20m |
| B08 | NIR | 10m |
| B8A | NIR estreito | 20m |
| B09 | Vapor d'água | 60m |
| B11 | SWIR 1 | 20m |
| B12 | SWIR 2 | 20m |
| **NDVI** | `(B08-B04)/(B08+B04)` | — |
| **NBR** | `(B08-B12)/(B08+B12)` | — |
| **NDWI** | `(B03-B08)/(B03+B08)` | — |
| **EVI** | `2.5*(B08-B04)/(B08+6*B04-7.5*B02+1)` | — |

---

## Cache

O plugin mantém cache em disco (pasta `sits_qgis_cache` no diretório temporário do sistema) com validade de **48 horas**:

- **Cubo BDC** (`*.rds`) — resultado da query STAC, evita re-consulta ao servidor
- **Série temporal** (`*_SER_*.rds`) — série processada (despiking + SG), evita re-download e re-processamento

Na segunda extração da mesma área/período/bandas o resultado chega em **~3 segundos**.

O botão **"🗑 Limpar cache"** remove todos os arquivos `.rds` armazenados.

---

## Comparação de Séries

Execute múltiplas extrações (diferentes áreas, períodos ou bandas). Cada resultado é salvo automaticamente no painel de comparação com um checkbox.

1. Marque as extrações que deseja comparar (mínimo 2)
2. Use **"☑ Todos"** / **"☐ Nenhum"** para seleção rápida
3. Clique em **"📊 Comparar selecionados"**

O visualizador de comparação abre uma **grade configurável** com:
- **Spinbox de colunas** (1–4): controla o layout lado a lado
- Zoom sincronizado em todos os painéis
- **"✕ Remover"** por painel para excluir da comparação sem fechar
- **"💾 Salvar grade..."** exporta todos os gráficos em um único PNG

---

## Arquitetura Técnica

### Servidor R persistente
O plugin inicia um processo R em background quando o QGIS carrega. Os pacotes (`sits`, `sf`, `ggplot2`, `dplyr`, `tidyr`) são carregados **uma única vez** — o botão "Extrair" só fica ativo após esse warmup.

Extrações subsequentes enviam apenas os parâmetros e recebem o resultado, sem overhead de startup do R.

### IPC via arquivos
A comunicação Python ↔ R usa arquivos temporários (sem stdin/stdout para comandos), o que garante compatibilidade com Windows:

```
sits_req.txt      ← Python escreve os parâmetros
sits_result.txt   → R escreve o resultado (DONE/ERROR/CANCELLED)
sits_progress.txt → R atualiza a etapa atual
sits_cancel.txt   ← Python cria para cancelar
```

### Pipeline de processamento

```
sits_cube()        — query STAC no BDC
    ↓
sits_get_data()    — download das séries temporais
    ↓
sits_apply()       — cálculo de índices (NDVI, NBR, NDWI, EVI)
    ↓
despiking MAD      — remoção de outliers (pixels de nuvem)
    ↓
sits_sgolay()      — filtro Savitzky-Golay (janela adaptativa)
    ↓
ggplot2            — gráfico PNG (1400×600 px)
```

### Tempos típicos (Pantanal, 2 anos, 3 bandas, 9 pontos)

| Etapa | 1ª extração | Com cache |
|---|---|---|
| Warmup R (pacotes) | ~20s (só uma vez) | — |
| Query STAC | ~4s | ~0s |
| Download série | ~25s | ~0s |
| Processamento + plot | ~3s | ~3s |
| **Total** | **~50s** | **~3s** |

---

## Troubleshooting

**"Rscript não encontrado"**
Configure o caminho manualmente no campo da seção ⚙️. No Windows, o caminho típico é:
```
C:\Program Files\R\R-4.x.x\bin\Rscript.exe
```

**"Servidor R encerrou após 3 tentativas"**
Verifique se os pacotes R estão instalados corretamente:
```r
library(sits); library(sf); library(ggplot2); library(dplyr); library(tidyr)
```

**"The closed date time provided is not in correct interval"**
A data inicial deve ser anterior à data final. O plugin valida isso antes de enviar ao R.

**Extração muito lenta**
- Reduza o número de bandas selecionadas
- Reduza o spinbox de pontos amostrados (padrão 9)
- Reduza o período de datas
- Verifique sua conexão com o BDC

**Gráfico não abre após a extração**
Verifique se o `cairo` está disponível no R:
```r
capabilities("cairo")
```
Se retornar `FALSE`, instale as dependências do sistema (Linux: `libcairo2-dev`).

---

## Estrutura do Repositório

```
sits_timeseries_v10_plugin/
├── __init__.py               # entry point do plugin QGIS
├── sits_timeseries.py        # classe principal do plugin
├── sits_dockwidget.py        # interface gráfica (PyQGIS)
├── run_sits_server.R         # servidor R persistente
├── icon.png                  # ícone do plugin
├── metadata.txt              # metadados QGIS
└── README.md
```

---

## Licença

MIT License — sinta-se livre para usar, modificar e distribuir.

---

## Referências

- [Brazil Data Cube](https://brazildatacube.org/)
- [sits — Satellite Image Time Series](https://github.com/e-sensing/sits)
- [QGIS Python API (PyQGIS)](https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/)
- [Sentinel-2 — ESA](https://sentinel.esa.int/web/sentinel/missions/sentinel-2)
