# 📥 Media Batch Downloader

Production-grade **parallel batch downloader** for YouTube, Udio, Facebook, and Instagram Reels — with multi-format audio conversion, thumbnail processing, retry logic, and manifest tracking.

## 🧰 Tools

### `ytdown.py` — YouTube Batch Downloader *(416 lines)*

The most robust downloader in the collection. Designed for batch music catalog operations where reliability matters more than speed.

```
links.csv (url, upc)
       │
       ▼
┌──────────────────────────────────┐
│  1. Metadata Extraction          │  yt-dlp --print-json
│     • Playlist unpacking         │
│     • Timeout-protected (30s)    │
└──────────────┬───────────────────┘
               ▼
┌──────────────────────────────────┐
│  2. Parallel Download            │  ThreadPoolExecutor
│     • Hidden temp dir (.ytdown)  │  (no Explorer thrash)
│     • Configurable concurrency   │
│     • ffmpeg semaphore limiting  │
└──────────────┬───────────────────┘
               ▼
┌──────────────────────────────────┐
│  3. Audio Conversion             │  ffmpeg
│     • → MP3 (configurable kbps)  │
│     • → FLAC (lossless)          │
│     • Atomic file moves          │
└──────────────┬───────────────────┘
               ▼
┌──────────────────────────────────┐
│  4. Thumbnail Processing         │
│     • → 1500×1500 JPEG           │
│     • Resize (no padding)        │
└──────────────────────────────────┘
```

**Usage:**
```bash
python ytdown.py --links links.csv --outdir output --jobs 4 --quality 320
```

| Flag | Default | Description |
|------|---------|-------------|
| `--links` | `links.csv` | Input CSV (url, upc) |
| `--outdir` | `yt_batch_output` | Output directory |
| `--jobs` | `2` | Concurrent download workers |
| `--ffmpeg-concurrency` | `2` | Concurrent FFmpeg processes |
| `--quality` | `192` | MP3 bitrate (kbps) |
| `--allow-playlists` | `false` | Allow playlist URLs |
| `--metadata-timeout` | `30` | Metadata fetch timeout (s) |
| `--download-timeout` | `300` | Download timeout (s) |

---

### `udio.py` — Udio/Universal Batch Downloader *(500 lines)*

Built for platforms with non-standard URLs (Udio, SoundCloud, etc.) where video IDs contain illegal Windows characters (`?`, `:`, `%`).

**Key difference from ytdown.py:**
- **Isolated temp dirs** — each download gets its own UUID-named temp folder, avoiding filename collisions
- **WAV output** — converts to WAV (not MP3/FLAC), designed for production audio chains
- **3000×3000 thumbnails** — DSP-grade cover art sizing
- **Exponential backoff** — configurable retries with `2^n` wait
- **Manifest + error CSV logging** — structured output tracking with timestamps

**Usage:**
```bash
python udio.py --links links.csv --outdir output --jobs 3 --retries 3
```

---

### `instagram_downloader.py` — Instagram Reel Audio Extractor *(215 lines)*

Two-phase pipeline using **Playwright** for URL resolution and **yt-dlp** for downloading.

```
Phase 1: Playwright (headed browser)
    │
    ├── Opens Instagram with persistent login session
    ├── Navigates to each audio page URL
    ├── Clicks into the first Reel using that audio
    └── Captures the direct /reel/ permalink
               │
               ▼
Phase 2: yt-dlp
    │
    ├── Downloads each Reel as WAV via yt-dlp -x
    ├── Loops short audio to minimum 60s duration
    └── Outputs to instagram_audio/ with manifest.csv
```

**Usage:**
```bash
# First run: browser opens for Instagram login (persisted)
python instagram_downloader.py
```

---

### `fdown.py` — Facebook Video Downloader *(81 lines)*

Optimized for bulk Facebook video downloads at ~480p with `aria2c` acceleration.

- 16x concurrent fragment downloads via aria2c
- Cookie support for private/group videos
- Auto-deduplication (skips existing files)
- CSV input: `link,filename`

**Usage:**
```bash
# Optional: place cookies.txt for private videos
python fdown.py
```

---

## 📋 Requirements

```
yt-dlp
playwright
pydub
```

**System dependencies:** `ffmpeg`, `aria2c` (optional, for fdown.py)

```bash
pip install yt-dlp playwright pydub
playwright install chromium
```

## 💡 Future Ideas

- [ ] SoundCloud batch downloader
- [ ] Progress dashboard (Streamlit or Rich console)
- [ ] Automatic retry queue for failed downloads

---

> Built for music catalog teams downloading thousands of tracks per week for Content ID matching and DSP delivery.
