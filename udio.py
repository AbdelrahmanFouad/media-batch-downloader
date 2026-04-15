


#!/usr/bin/env python3
"""
ytdown.py — WAV/JPEG Batch Downloader (Fixed for Udio/Windows filenames)

Changes:
- Uses ISOLATED temp folders per download. This fixes the "File not found" error
  caused by Udio IDs containing illegal Windows characters (?:%).
- Ignores yt-dlp exit codes if the file was successfully downloaded anyway.

Usage:
    python ytdown.py --links links.csv --outdir yt_batch_output
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
import time
import uuid
from pathlib import Path
from collections import defaultdict
from datetime import datetime

# ---------------- utils ----------------
def sanitize_filename(s: str, max_len: int = 200) -> str:
    s = (s or "").strip()
    s = s.replace("/", "-").replace("\\", "-")
    # Remove control characters
    s = re.sub(r"[\x00-\x1f\x7f]+", "", s)
    # Remove characters invalid in Windows/Linux filenames
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

# ---------------- CSV I/O ----------------
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
    return out

# ---------------- logging ----------------
log_lock = threading.Lock()

def _open_csv_append(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", newline='', encoding="utf-8")

def log_error(outdir: Path, url: str, upc: str, occ: int, stage: str, message: str):
    log_path = outdir / "error_log.csv"
    with log_lock:
        new_file = not log_path.exists()
        with _open_csv_append(log_path) as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(["timestamp", "url", "upc", "occurrence", "stage", "message"])
            writer.writerow([datetime.now().isoformat(), url, upc, occ, stage, message])

def log_manifest(outdir: Path, url: str, upc: str, occ: int, vid_id: str, wav: str, image: str):
    log_path = outdir / "manifest.csv"
    with log_lock:
        new_file = not log_path.exists()
        with _open_csv_append(log_path) as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(["timestamp", "url", "upc", "occurrence", "id", "wav", "image"])
            writer.writerow([datetime.now().isoformat(), url, upc, occ, vid_id or "", wav or "", image or ""])

# ---------------- yt-dlp helpers ----------------
def with_retries(fn, retries: int, wait_base: float, stage: str, on_error):
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as e:
            msg = f"{stage} attempt {attempt} failed: {e}"
            on_error(msg)
            if attempt >= retries:
                raise
            sleep_for = wait_base * (2 ** (attempt - 1))
            time.sleep(sleep_for)
            attempt += 1

def yt_dlp_info(url, metadata_timeout):
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--skip-download", "--print-json", url]
    code, out, err = run_subprocess(cmd, timeout=metadata_timeout)
    if code == 0 and out:
        for line in out.splitlines():
            line = line.strip()
            if not line: continue
            try:
                return json.loads(line)
            except Exception:
                continue
        raise RuntimeError("yt-dlp returned no valid JSON line")
    elif code == -9:
        raise TimeoutError(f"yt-dlp metadata timeout after {metadata_timeout}s")
    else:
        raise RuntimeError(f"yt-dlp metadata failed (code {code}) err={err}")

def yt_dlp_download(url, job_dir: Path, download_timeout):
    """
    Download into a UNIQUE job_dir.
    We don't force the filename to be the ID because IDs can be messy.
    We just tell it to download "something" into this empty folder.
    """
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--no-mtime",
        "-f", "bestaudio/best",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "-o", os.path.join(str(job_dir), "%(title)s.%(ext)s"), # Use title, easier for windows
        url
    ]
    return run_subprocess(cmd, timeout=download_timeout)

def yt_dlp_fetch_thumbnail_only(url, job_dir: Path, download_timeout):
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        "--write-all-thumbnails",
        "--convert-thumbnails", "jpg",
        "-o", os.path.join(str(job_dir), "thumb.%(ext)s"),
        url
    ]
    return run_subprocess(cmd, timeout=download_timeout)

# ---------------- ffmpeg-limited run ----------------
ffmpeg_lock = None

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
THUMB_EXTS = ["jpg", "jpeg", "webp", "png"]
AUDIO_EXTS = ["m4a", "webm", "mp4", "opus", "ogg", "wav", "aac", "flac", "mp3"]

def find_downloaded_audio(job_dir: Path):
    """
    Since job_dir is unique to this specific download, we just grab
    the first audio-like file we see.
    """
    for p in job_dir.iterdir():
        if p.is_file() and p.suffix.lower().lstrip('.') in AUDIO_EXTS:
            return p
    return None

def convert_audio_to_wav(src: Path, job_dir: Path, base_name: str):
    """
    Convert src to .wav inside job_dir.
    """
    produced = []
    ffmpeg = shutil.which("ffmpeg")
    
    wav_target = unique_path(job_dir / f"{base_name}.wav")
    
    if not ffmpeg:
        # Fallback: just copy original with safe name
        try:
            fallback = unique_path(job_dir / f"{base_name}{src.suffix}")
            shutil.copy2(src, fallback)
            produced.append(fallback)
        except Exception:
            pass
        return produced

    try:
        # Convert to WAV (PCM)
        if ffmpeg_run([ffmpeg, "-y", "-i", str(src), "-vn", str(wav_target)]):
            if wav_target.exists():
                produced.append(wav_target)
    except Exception:
        pass

    # Remove source if we successfully made a WAV
    if produced and src.exists():
        try: src.unlink()
        except: pass

    # If conversion failed, keep original
    if not produced:
        try:
            fallback = unique_path(job_dir / f"{base_name}{src.suffix}")
            shutil.copy2(src, fallback)
            produced.append(fallback)
        except: pass
        
    return produced

def convert_and_resize_thumbnail(found_thumb: Path, job_dir: Path, image_name: str):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            fallback = unique_path(job_dir / f"{image_name}{found_thumb.suffix}")
            shutil.copy2(found_thumb, fallback)
            return [fallback]
        except: return []

    target = unique_path(job_dir / f"{image_name}.jpeg")
    vf = "scale=3000:3000" # Force resize
    try:
        if ffmpeg_run([ffmpeg, "-y", "-i", str(found_thumb), "-vf", vf, str(target)]):
            if target.exists():
                return [target]
    except Exception:
        pass
    
    # Fallback
    try:
        fallback = unique_path(job_dir / f"{image_name}{found_thumb.suffix}")
        shutil.copy2(found_thumb, fallback)
        return [fallback]
    except: return []

# ---------------- helpers to move into final outdir ----------------
def move_to_final(paths, final_outdir: Path):
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

def find_any_thumb(job_dir: Path):
    # Just look for any image in this isolated folder
    for p in job_dir.iterdir():
        if p.is_file() and p.suffix.lower().lstrip('.') in THUMB_EXTS:
            return p
    return None

def ensure_thumbnail(url, job_dir, retries, wait_base, download_timeout, on_error):
    # Check if download step already grabbed one
    thumb = find_any_thumb(job_dir)
    if thumb: return thumb

    def fetch_once_all():
        rc, out, err = yt_dlp_fetch_thumbnail_only(url, job_dir, download_timeout)
        # We ignore RC here partly because we check file existence
        th = find_any_thumb(job_dir)
        if not th:
            raise RuntimeError("no thumbnail file produced")
        return th

    try:
        return with_retries(fetch_once_all, retries=retries, wait_base=wait_base, stage="thumbnail",
                            on_error=lambda m: on_error("thumbnail", m))
    except Exception as e:
        on_error("thumbnail", f"giving up: {e}")
        return None

# ---------------- main worker ----------------
def process_row_csv(row, main_tmpdir: Path, final_outdir: Path,
                    metadata_timeout: int, download_timeout: int,
                    retries: int, retry_wait: float):
    
    url, upc, upc_occurrence = row
    produced_final = []
    failures_local = []

    # CREATE UNIQUE TEMP DIR FOR THIS JOB
    # This solves the issue where filenames don't match expected IDs
    job_dir = main_tmpdir / str(uuid.uuid4())
    job_dir.mkdir(parents=True, exist_ok=True)

    def on_error(stage, message):
        failures_local.append((url, f"{stage}: {message}"))
        log_error(final_outdir, url, upc, upc_occurrence, stage, message)

    try:
        # 1) Metadata
        def _meta_once(): return yt_dlp_info(url, metadata_timeout)
        try:
            info = with_retries(_meta_once, retries=retries, wait_base=retry_wait, stage="metadata",
                                on_error=lambda m: on_error("metadata", m))
        except Exception as e:
            on_error("metadata", f"fatal: {e}")
            shutil.rmtree(job_dir, ignore_errors=True)
            return produced_final, 0, failures_local

        vid_id = info.get("id") or info.get("webpage_id") or "unknown_id"

        # 2) Download
        def _dl_once():
            rc, out, err = yt_dlp_download(url, job_dir, download_timeout)
            if rc == -9: raise TimeoutError("timeout")
            # If rc != 0 but file exists, we consider it success. 
            # yt-dlp might fail on post-processing weird formats but still drop the file.
            if rc != 0 and not find_downloaded_audio(job_dir):
                 raise RuntimeError(f"code {rc}")
            return True

        try:
            with_retries(_dl_once, retries=retries, wait_base=retry_wait, stage="download",
                         on_error=lambda m: on_error("download", m))
        except Exception as e:
            on_error("download", f"fatal: {e}")

        # 3) Thumbnail
        thumb_path = ensure_thumbnail(
            url, job_dir, retries=retries, wait_base=retry_wait,
            download_timeout=download_timeout, on_error=on_error
        )

        # Naming
        upc_base = sanitize_filename(upc)
        base_name = f"{upc_base}_{upc_occurrence}"
        
        # 4) Find Audio (Any audio file in the unique folder)
        audio_src = find_downloaded_audio(job_dir)
        produced_paths = []

        if audio_src:
            produced_paths.extend(convert_audio_to_wav(audio_src, job_dir, base_name))
        else:
            on_error("audio", f"no downloaded audio found in {job_dir}")

        # 5) Convert Thumbnail
        image_final_name = None
        if thumb_path:
            converted = convert_and_resize_thumbnail(thumb_path, job_dir, base_name)
            produced_paths.extend(converted)
            if converted:
                image_final_name = converted[0].name

        # 6) Move to final
        moved = []
        if produced_paths:
            moved = move_to_final(produced_paths, final_outdir)
            produced_final.extend(moved)

        # 7) Log
        wav_out = next((p.name for p in moved if p.suffix.lower()==".wav"), "")
        img_out = next((p.name for p in moved if p.suffix.lower() in (".jpg",".jpeg",".png",".webp")), "")
        log_manifest(final_outdir, url, upc, upc_occurrence, vid_id, wav_out, img_out)

        succeeded = 1 if moved else 0
        if not moved:
            on_error("post", "no outputs moved to final directory")
        
        return produced_final, succeeded, failures_local

    except Exception as e:
        on_error("unexpected", f"{e}")
        return produced_final, 0, failures_local
    finally:
        # Cleanup unique job dir
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except: pass

# ---------------- CLI / main ----------------
def set_hidden_windows(path: Path):
    if os.name == 'nt':
        try:
            FILE_ATTRIBUTE_HIDDEN = 0x02
            ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
        except Exception: pass

def main():
    parser = argparse.ArgumentParser(description="ytdown.py — WAV/JPEG Batch Downloader")
    parser.add_argument("--links", default="links.csv", help="CSV file (link,upc) no header")
    parser.add_argument("--outdir", default="yt_batch_output", help="output directory")
    parser.add_argument("--jobs", type=int, default=2, help="concurrent workers")
    parser.add_argument("--ffmpeg-concurrency", type=int, default=2, help="concurrent ffmpeg processes")
    parser.add_argument("--metadata-timeout", type=int, default=30, help="metadata fetch timeout")
    parser.add_argument("--download-timeout", type=int, default=300, help="download timeout")
    parser.add_argument("--retries", type=int, default=3, help="retry attempts")
    parser.add_argument("--retry-wait", type=float, default=2.0, help="backoff base")
    args = parser.parse_args()

    links_path = Path(args.links)
    if not links_path.is_file():
        print(f"ERROR: CSV file not found: {links_path.resolve()}")
        sys.exit(2)

    rows = read_links_csv(links_path)
    if not rows:
        print("No valid rows in links.csv")
        sys.exit(0)

    upc_counts = defaultdict(int)
    rows_with_occ = []
    for link, upc in rows:
        upc_counts[upc] += 1
        rows_with_occ.append((link, upc, upc_counts[upc]))

    final_outdir = Path(args.outdir)
    final_outdir.mkdir(parents=True, exist_ok=True)

    tmpdir = final_outdir / ".ytdown_tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    set_hidden_windows(tmpdir)

    global ffmpeg_lock
    ffmpeg_lock = threading.Semaphore(max(1, int(args.ffmpeg_concurrency)))

    print(f"Processing {len(rows_with_occ)} rows with {args.jobs} workers...")

    successes = 0; failures = []; produced_files = []
    lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futures = [ex.submit(process_row_csv, r, tmpdir, final_outdir,
                             args.metadata_timeout, args.download_timeout,
                             args.retries, args.retry_wait)
                   for r in rows_with_occ]
        for fut in concurrent.futures.as_completed(futures):
            produced_local, local_successes, local_failures = fut.result()
            with lock:
                produced_files.extend(produced_local)
                successes += local_successes
                failures.extend(local_failures)

    print(f"Done. {successes} entries processed successfully, {len(failures)} failed rows.")
    if produced_files:
        print(f"\nProduced {len(produced_files)} files in: {final_outdir.resolve()}")
        for p in produced_files:
            try: print(f" - {p.name}")
            except Exception: print(f" - {p}")
            
    # Cleanup main tmp if empty
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except: pass

if __name__ == "__main__":
    main()
