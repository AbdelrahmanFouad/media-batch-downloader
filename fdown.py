#!/usr/bin/env python3
"""
fdown.py — read links.csv (link,filename) and download FB videos at ~480p by default.
Usage: python fdown.py
CSV: headered link,filename
"""
import csv, shutil
from pathlib import Path
import yt_dlp

CSV = Path("links.csv")
OUT = Path("fb_videos"); OUT.mkdir(exist_ok=True)
COOKIES = Path("cookies.txt")
FMT = "bestvideo[height<=480]+bestaudio/best"   # prefer <=480p, else best
MERGE_EXT = "mp4"

if not CSV.is_file():
    print("Missing links.csv (columns: link,filename)"); raise SystemExit(1)

def unique(p):
    if not p.exists(): return p
    base, i = p.stem, 1
    while True:
        q = p.with_name(f"{base}_{i}{p.suffix}")
        if not q.exists(): return q
        i += 1

rows = [r for r in csv.DictReader(CSV.open(encoding="utf-8-sig"))]
if not rows:
    print("No rows in links.csv"); raise SystemExit(0)

ydl_opts = {
    "format": FMT,
    "outtmpl": str(OUT / "%(id)s.%(ext)s"),
    "merge_output_format": MERGE_EXT,
    "noplaylist": True,
    # ---- speed tweaks ----
    "concurrent_fragment_downloads": 16,
    "fragment_retries": 5,
    "fragment_retry_wait": 1,
    "external_downloader": "aria2c",
    "external_downloader_args": ["-x", "16", "-s", "16", "-k", "1M"],
    # ----------------------
    "ignoreerrors": True,
    "no_warnings": True,
}
if COOKIES.is_file():
    ydl_opts["cookiefile"] = str(COOKIES); print("Using cookies.txt")

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    for i, r in enumerate(rows, 1):
        link = (r.get("link") or "").strip()
        name = (r.get("filename") or f"fb_video_{i}").strip()
        if not link:
            print(f"[{i}] empty link, skip"); continue
        if not Path(name).suffix:  # auto-append .mp4 if missing
            name += f".{MERGE_EXT}"
        target = OUT / name
        if target.exists():
            print(f"[{i}] {name} exists, skip"); continue
        print(f"[{i}] Downloading -> {name}")
        try:
            info = ydl.extract_info(link, download=True)
            if not info:
                raise RuntimeError("no info")
            if "entries" in info and info["entries"]:
                info = next(e for e in info["entries"] if e)
            vid = info.get("id") or ""
            ext = info.get("ext") or MERGE_EXT
            candidates = list(OUT.glob(f"{vid}.*"))
            src = candidates[0] if candidates else OUT / f"{vid}.{ext}"
            if not src.exists():
                raise RuntimeError("downloaded file not found")
            dst = unique(target)
            shutil.move(str(src), str(dst))
            print(f"   saved -> {dst.name}")
        except Exception as e:
            print(f"   error: {e}")

print("Done.")
