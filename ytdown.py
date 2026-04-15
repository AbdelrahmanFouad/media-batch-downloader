#!/usr/bin/env python3
"""
ytdown.py — CSV-only robust downloader (run: python ytdown.py)

Behaviour unchanged except:
- Downloads/conversions are performed inside a hidden temp dir (outdir/.ytdown_tmp),
  then final files are atomically moved into outdir when ready to avoid Explorer thrash.
- Thumbnails are resized to exactly 1500x1500 ignoring aspect ratio (no padding).
"""
import argparse
import concurrent.futures
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import re
import ctypes
from pathlib import Path

# ---------------- utils ----------------
def sanitize_filename(s: str, max_len: int = 200) -> str:
    s = (s or "").strip()
    s = s.replace("/", "-").replace("\\", "-")
    s = re.sub(r"[\x00-\x1f\x7f]+", "", s)
    s = re.sub(r"[^\w\s\-\.\#\(\)\[\]]+", "", s)
    s = re.sub(r"\s+", " ", s)
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "untitled"

def unique_path(p: Path) -> Path:
    """Return a Path that does not yet exist by appending (n) if needed."""
    if not p.exists():
        return p
    base = p.stem
    ext = p.suffix
    parent = p.parent
    i = 1
    while True:
        candidate = parent / f"{base} ({i}){ext}"
        if not candidate.exists():
            return candidate
        i += 1

def run_subprocess(cmd, timeout=None):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -9, None, None
    except Exception as e:
        return -2, None, str(e)

# ---------------- I/O ----------------
def read_links_csv(path: Path):
    out = []
    with path.open(newline='', encoding='utf-8', errors='ignore') as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row:
                continue
            cells = [c.strip() for c in row]
            if not cells or cells[0].startswith("#"):
                continue
            if len(cells) < 2:
                print(f"Skipping malformed CSV row (need link,upc): {row}")
                continue
            link, upc = cells[0], cells[1]
            if not link or not upc:
                print(f"Skipping row with empty link or upc: {row}")
                continue
            out.append((link, upc))
    # dedupe preserving order
    seen = set(); dedup = []
    for pair in out:
        if pair in seen: continue
        seen.add(pair); dedup.append(pair)
    return dedup

# ---------------- yt-dlp subprocess helpers ----------------
def yt_dlp_info(url, metadata_timeout, tmpdir: Path):
    """Return list of info dicts (one per entry) or raise on error/timeout."""
    cmd = ["yt-dlp", "--no-warnings", "--skip-download", "--print-json", url]
    code, out, err = run_subprocess(cmd, timeout=metadata_timeout)
    if code == 0 and out:
        infos = []
        for line in out.splitlines():
            line = line.strip()
            if not line: continue
            try:
                infos.append(json.loads(line))
            except Exception:
                continue
        return infos
    elif code == -9:
        raise TimeoutError(f"yt-dlp metadata timeout after {metadata_timeout}s")
    else:
        raise RuntimeError(f"yt-dlp metadata failed (code {code}) err={err}")

def yt_dlp_download(url, tmpdir: Path, allow_playlists, download_timeout):
    """
    Download into tmpdir (so final folder isn't spammed).
    Returns (returncode, stdout, stderr)
    """
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "-f", "bestaudio",
        "--write-thumbnail",
        "-o", os.path.join(str(tmpdir), "%(id)s.%(ext)s"),
        url
    ]
    if not allow_playlists:
        cmd.insert(1, "--no-playlist")
    return run_subprocess(cmd, timeout=download_timeout)

# ---------------- ffmpeg-limited run ----------------
ffmpeg_lock = None  # initialized in main

def ffmpeg_run(args_list):
    global ffmpeg_lock
    if ffmpeg_lock is None:
        return subprocess.run(args_list, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    acquired = ffmpeg_lock.acquire(timeout=600)
    try:
        if not acquired:
            return False
        res = subprocess.run(args_list, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    finally:
        if acquired:
            ffmpeg_lock.release()

# ---------------- file find & conversions ----------------
AUDIO_EXT_BLACKLIST = {"jpg", "jpeg", "png", "webp", "webm.thumb", "part", "info.json"}
THUMB_EXTS = ["jpg", "jpeg", "webp", "png"]

def find_downloaded_audio(search_dir: Path, vid_id: str):
    """Look for downloaded container/audio in the provided search_dir (tmpdir)."""
    candidates = [p for p in search_dir.iterdir() if p.is_file() and p.name.startswith(vid_id + ".")]
    preferred = ["m4a", "webm", "mp4", "opus", "ogg", "wav", "aac", "flac", "mp3"]
    for ext in preferred:
        for c in candidates:
            if c.suffix.lower().lstrip('.') == ext:
                return c
    return candidates[0] if candidates else None

def convert_audio_to_formats(src: Path, workdir: Path, base_name: str, quality_kbps: str):
    """
    Convert inside workdir (tmpdir). Returns list of produced Paths in workdir.
    We will move them to the final outdir after conversion.
    """
    produced = []
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return produced
    mp3_target = unique_path(workdir / f"{base_name}.mp3")
    flac_target = unique_path(workdir / f"{base_name}.flac")
    src_ext = src.suffix.lower()
    try:
        if src_ext == ".mp3":
            shutil.copy2(src, mp3_target); produced.append(mp3_target)
            if ffmpeg_run([ffmpeg, "-y", "-i", str(src), "-compression_level", "5", str(flac_target)]):
                produced.append(flac_target)
        elif src_ext == ".flac":
            shutil.copy2(src, flac_target); produced.append(flac_target)
            if ffmpeg_run([ffmpeg, "-y", "-i", str(src), "-b:a", f"{quality_kbps}k", str(mp3_target)]):
                produced.append(mp3_target)
        else:
            cmd = [ffmpeg, "-y", "-i", str(src), "-vn",
                   "-b:a", f"{quality_kbps}k", str(mp3_target),
                   "-compression_level", "5", str(flac_target)]
            if ffmpeg_run(cmd):
                if mp3_target.exists(): produced.append(mp3_target)
                if flac_target.exists(): produced.append(flac_target)
    except Exception:
        pass

    # remove original container if conversion succeeded
    try:
        if src.exists() and src_ext not in (".mp3", ".flac") and any(p.exists() for p in (mp3_target, flac_target)):
            try: src.unlink()
            except Exception: pass
    except Exception:
        pass

    if not produced:
        try:
            fallback = unique_path(workdir / f"{base_name}{src.suffix}")
            shutil.copy2(src, fallback); produced.append(fallback)
        except Exception:
            pass
    return produced

def convert_and_resize_thumbnail(found_thumb: Path, workdir: Path, image_name: str):
    """
    Convert and resize inside workdir. Return produced path(s) in workdir.
    Resizes to exactly 1500x1500 ignoring aspect ratio (no padding).
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            fallback = unique_path(workdir / f"{image_name}{found_thumb.suffix}")
            found_thumb.rename(fallback)
            return [fallback]
        except Exception:
            return []
    target = unique_path(workdir / f"{image_name}.jpeg")
    # Resize to exact 1500x1500, ignoring aspect ratio (stretches/squashes image).
    vf = "scale=1500:1500"
    try:
        if ffmpeg_run([ffmpeg, "-y", "-i", str(found_thumb), "-vf", vf, str(target)]):
            if target.exists():
                try: found_thumb.unlink()
                except Exception: pass
                return [target]
    except Exception:
        pass
    try:
        fallback = unique_path(workdir / f"{image_name}{found_thumb.suffix}")
        found_thumb.rename(fallback)
        return [fallback]
    except Exception:
        return []

# ---------------- helpers to move into final outdir ----------------
def move_to_final(paths, final_outdir: Path):
    """
    Atomically move produced paths (in tmpdir) into final_outdir.
    Returns list of final Path objects (where each file was moved).
    """
    final_paths = []
    for p in paths:
        dest = unique_path(final_outdir / p.name)
        try:
            os.replace(str(p), str(dest))
            final_paths.append(dest)
        except Exception:
            try:
                shutil.move(str(p), str(dest))
                final_paths.append(dest)
            except Exception:
                continue
    return final_paths

def cleanup_tmp_for_id(tmpdir: Path, vid_id: str):
    """Remove files like vid_id.* leftover in tmpdir to avoid accumulation."""
    for p in list(tmpdir.glob(f"{vid_id}.*")):
        try:
            if p.is_file():
                p.unlink()
        except Exception:
            pass

# ---------------- main worker ----------------
def process_row_csv(row, tmpdir: Path, final_outdir: Path, allow_playlists: bool, quality: str,
                    metadata_timeout: int, download_timeout: int):
    """
    Downloads & converts a single CSV row. Work happens in tmpdir, then files are moved into final_outdir.
    Returns (produced_final_paths(list), success_count, failures_list)
    """
    url, upc = row
    produced_final = []
    failures_local = []
    try:
        # 1) fetch metadata
        try:
            infos = yt_dlp_info(url, metadata_timeout, tmpdir)
        except Exception as e:
            failures_local.append((url, f"metadata error: {e}"))
            return produced_final, 0, failures_local

        # 2) download into tmpdir
        rc, out, err = yt_dlp_download(url, tmpdir, allow_playlists, download_timeout)
        if rc == -9:
            failures_local.append((url, f"download timeout after {download_timeout}s"))
            return produced_final, 0, failures_local
        if rc != 0:
            failures_local.append((url, f"yt-dlp download failed (code {rc})"))

        # 3) convert & move each entry
        entries = infos if isinstance(infos, list) else [infos]
        total = len(entries)
        success_count = 0
        for idx, entry in enumerate(entries, start=1):
            entry['_playlist_len'] = total
            vid_id = entry.get("id") or entry.get("webpage_id")
            if not vid_id:
                failures_local.append((url, "entry missing id"))
                continue

            base_audio = f"{sanitize_filename(upc)}_{idx}"
            image_name = sanitize_filename(upc) if (idx == 1 and total == 1) else f"{sanitize_filename(upc)}_{idx}"

            # find the downloaded audio container in tmpdir
            audio_src = None
            reported_ext = entry.get("ext")
            if reported_ext:
                cand = tmpdir / f"{vid_id}.{reported_ext}"
                if cand.exists(): audio_src = cand
            if not audio_src:
                audio_src = find_downloaded_audio(tmpdir, vid_id)

            produced_paths = []
            if audio_src:
                produced_paths.extend(convert_audio_to_formats(audio_src, tmpdir, base_audio, quality))
            else:
                failures_local.append((url, f"no downloaded audio found for id={vid_id}"))

            # thumbnail (tmpdir)
            found_thumb = None
            for ext in THUMB_EXTS:
                p = tmpdir / f"{vid_id}.{ext}"
                if p.exists(): found_thumb = p; break
            if found_thumb:
                produced_paths.extend(convert_and_resize_thumbnail(found_thumb, tmpdir, image_name))

            # move produced files into final_outdir atomically
            if produced_paths:
                moved = move_to_final(produced_paths, final_outdir)
                produced_final.extend(moved)

            # cleanup any leftover files for this vid_id in tmpdir (containers, thumbs, info)
            cleanup_tmp_for_id(tmpdir, vid_id)

            success_count += 1
        return produced_final, success_count, failures_local
    except Exception as e:
        failures_local.append((url, f"unexpected error: {e}"))
        return produced_final, 0, failures_local

# ---------------- CLI / main ----------------
def set_hidden_windows(path: Path):
    """Try to hide a folder on Windows (no-op on other OS)."""
    if os.name == 'nt':
        try:
            FILE_ATTRIBUTE_HIDDEN = 0x02
            ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass

def main():
    parser = argparse.ArgumentParser(description="ytdown.py — CSV-only robust batch downloader")
    parser.add_argument("--links", default="links.csv", help="CSV file (link,upc) no header (default links.csv)")
    parser.add_argument("--outdir", default="yt_batch_output", help="output directory")
    parser.add_argument("--jobs", type=int, default=2, help="concurrent workers (default 2)")
    parser.add_argument("--ffmpeg-concurrency", type=int, default=2, help="concurrent ffmpeg processes (default 1)")
    parser.add_argument("--allow-playlists", action="store_true", help="allow playlist links")
    parser.add_argument("--quality", default="192", help="MP3 bitrate kbps")
    parser.add_argument("--metadata-timeout", type=int, default=30, help="metadata fetch timeout (s)")
    parser.add_argument("--download-timeout", type=int, default=300, help="download timeout (s)")
    args = parser.parse_args()

    links_path = Path(args.links)
    if not links_path.is_file():
        print(f"ERROR: CSV file not found: {links_path.resolve()}\nCreate links.csv (each row: url,upc) and run `python ytdown.py`")
        sys.exit(2)

    rows = read_links_csv(links_path)
    if not rows:
        print("No valid rows in links.csv")
        sys.exit(0)

    final_outdir = Path(args.outdir)
    final_outdir.mkdir(parents=True, exist_ok=True)

    # temporary working directory (hidden) for downloads/conversions
    tmpdir = final_outdir / ".ytdown_tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    set_hidden_windows(tmpdir)

    # initialize ffmpeg semaphore
    global ffmpeg_lock
    ffmpeg_lock = threading.Semaphore(max(1, int(args.ffmpeg_concurrency)))

    print(f"Processing {len(rows)} rows with {args.jobs} workers (ffmpeg concurrency={args.ffmpeg_concurrency})")
    successes = 0; failures = []; produced_files = []
    lock = threading.Lock()

    # Submit tasks: each worker writes into tmpdir and moves finished files into final_outdir
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futures = [ex.submit(process_row_csv, r, tmpdir, final_outdir, args.allow_playlists, args.quality,
                             args.metadata_timeout, args.download_timeout) for r in rows]
        for fut in concurrent.futures.as_completed(futures):
            produced_local, local_successes, local_failures = fut.result()
            with lock:
                produced_files.extend(produced_local)
                successes += local_successes
                failures.extend(local_failures)

    print(f"Done. {successes} entries processed, {len(failures)} failed rows.")
    if produced_files:
        print(f"\nProduced {len(produced_files)} files in: {final_outdir.resolve()}")
        for p in produced_files:
            try: print(f" - {p.name}")
            except Exception: print(f" - {p}")
    if failures:
        print("\nFailures:")
        for u,e in failures:
            print(f"- {u} -> {e}")

    # optional: remove tmpdir if empty
    try:
        if any(tmpdir.iterdir()):
            pass
        else:
            tmpdir.rmdir()
    except Exception:
        pass

if __name__ == "__main__":
    main()
