# 📥 Media Batch Downloader

A production-grade batch downloader for **YouTube**, **Facebook**, and **Udio** audio content — built for music catalog and digital operations teams.

## ✨ Features

- **CSV-driven** — define URLs + identifiers in a simple `links.csv` file; no GUI required
- **Parallel processing** — configurable concurrent workers via `--jobs` flag
- **Atomic file delivery** — downloads happen in a hidden temp directory, then files are atomically moved to the output folder to prevent Explorer thrash and partial reads
- **Exponential backoff & retry** — built-in resilience against network failures and rate limits
- **Multi-format output** — converts audio to both **MP3** (192 kbps) and **FLAC** (lossless) using FFmpeg
- **Thumbnail processing** — fetches and resizes cover art to exactly **1500×1500 px** JPEG (DSP-ready)
- **Deduplication** — skips duplicate rows in the input CSV automatically
- **Full manifest** — produces `manifest.csv` (successes) and `error_log.csv` (failures) for audit trails

## 📂 Scripts

| Script | Platform | Output |
|--------|----------|--------|
| `ytdown.py` | YouTube | MP3 + FLAC + 1500×1500 JPEG |
| `fdown.py` | Facebook | MP4 ~480p via aria2c |
| `udio.py` | Udio | WAV + JPEG |

## 🚀 Quick Start

### Prerequisites

```bash
pip install yt-dlp
# Also required on PATH: ffmpeg, aria2c (for fdown.py)
```

### Usage

1. Create `links.csv` (no header row):

```csv
https://www.youtube.com/watch?v=dQw4w9WgXcQ,730170420888
https://www.youtube.com/watch?v=abcdef123456,730170420999
```

2. Run:

```bash
python ytdown.py
```

3. Find output in `yt_batch_output/`:

```
yt_batch_output/
├── 730170420888.mp3
├── 730170420888.flac
├── 730170420888.jpeg
├── manifest.csv
└── error_log.csv
```

### Advanced Options

```bash
python ytdown.py \
  --links my_tracks.csv \
  --outdir ./masters \
  --jobs 4 \
  --quality 320 \
  --download-timeout 600
```

| Flag | Default | Description |
|------|---------|-------------|
| `--links` | `links.csv` | Input CSV path |
| `--outdir` | `yt_batch_output` | Output directory |
| `--jobs` | `2` | Parallel download workers |
| `--ffmpeg-concurrency` | `2` | Parallel FFmpeg conversions |
| `--quality` | `192` | MP3 bitrate (kbps) |
| `--allow-playlists` | off | Allow playlist URLs |
| `--metadata-timeout` | `30` | Metadata fetch timeout (s) |
| `--download-timeout` | `300` | Download timeout (s) |

## 🔧 Architecture

```
links.csv
    │
    ▼
read_links_csv()         ← deduplication
    │
    ▼
ThreadPoolExecutor       ← parallel workers
    │
    ├── yt_dlp_info()    ← metadata fetch
    ├── yt_dlp_download()← download → .ytdown_tmp/
    ├── convert_audio()  ← FFmpeg → MP3 + FLAC
    ├── resize_thumbnail()← FFmpeg → 1500×1500 JPEG
    └── move_to_final()  ← atomic rename to outdir/
    │
    ▼
manifest.csv + error_log.csv
```

## 🛡️ Error Handling

- **Timeout expired** → logged to `error_log.csv`, processing continues
- **Download failure** → logged, remaining rows unaffected  
- **Missing audio** → graceful fallback, never crashes the batch
- **Temp directory cleanup** → leftover files purged per-entry to avoid disk bloat

## 📋 Requirements

```
yt-dlp
ffmpeg (system)
aria2c (system, for fdown.py only)
Pillow (optional fallback for thumbnails)
```

## 💡 Future Ideas

- [ ] Resume interrupted batches using a progress checkpoint file
- [ ] Spotify/Apple Music source support via metadata matching
- [ ] Slack/email notification on batch completion
- [ ] Dashboard UI for real-time download progress

---

> Built for music catalog operations — handling thousands of tracks across digital distribution pipelines.
