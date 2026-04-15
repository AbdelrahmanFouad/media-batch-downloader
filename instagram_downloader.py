
import re
import csv
import os
import subprocess
import time
import logging
from pydub import AudioSegment
from playwright.sync_api import sync_playwright, TimeoutError

# --- Configuration ---
LINKS_FILE = 'links.csv'
OUTPUT_DIR = 'instagram_audio'
TEMP_DIR = 'temp_audio_playwright'
MANIFEST_FILE = 'manifest.csv'
ERROR_LOG_FILE = 'error_log.csv'
USER_DATA_DIR = './playwright_user_data'
MAX_WORKERS = 1 # Playwright is not thread-safe in this context, run sequentially.
TARGET_DURATION_S = 60

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def create_directories():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)

def get_audio_duration(file_path):
    try:
        audio = AudioSegment.from_wav(file_path)
        return len(audio) / 1000
    except Exception as e:
        logging.error(f"Could not get duration for {file_path}: {e}")
        return 0

def download_and_process_video(video_url, original_url, index):
    temp_file_path = None
    try:
        logging.info(f"Downloading video from direct URL: {video_url}")
        temp_file_path_template = os.path.join(TEMP_DIR, f"temp_{index}.%(ext)s")
        
        command = [
            'yt-dlp',
            '-x', '--audio-format', 'wav',
            '-o', temp_file_path_template,
            video_url
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)

        # Find the downloaded file
        for file in os.listdir(TEMP_DIR):
            if file.startswith(f"temp_{index}") and file.endswith('.wav'):
                temp_file_path = os.path.join(TEMP_DIR, file)
                break
        
        if not temp_file_path or not os.path.exists(temp_file_path):
            raise FileNotFoundError(f"Downloaded WAV file for URL {original_url} not found.")

        # Check and loop audio
        duration = get_audio_duration(temp_file_path)
        if duration > 0 and duration < TARGET_DURATION_S:
            logging.info(f"Audio for {original_url} is {duration}s long. Looping...")
            audio = AudioSegment.from_wav(temp_file_path)
            loops_needed = int(TARGET_DURATION_S // duration) + 1
            final_audio = audio * loops_needed
        else:
            final_audio = AudioSegment.from_wav(temp_file_path)

        # Export Final Audio
        output_filename = f"audio_{index}.wav"
        output_path = os.path.join(OUTPUT_DIR, output_filename)
        final_audio.export(output_path, format='wav')

        logging.info(f"Successfully processed and saved to {output_path}")
        return output_path, None

    except Exception as e:
        logging.error(f"Error during download/process for {original_url}: {e}")
        error_message = str(e)
        if isinstance(e, subprocess.CalledProcessError):
            error_message = e.stderr
        return None, error_message
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

def main():
    create_directories()
    
    original_audio_urls = []
    try:
        with open(LINKS_FILE, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            original_audio_urls = [row[0] for row in reader if row]
    except FileNotFoundError:
        logging.error(f"'{LINKS_FILE}' not found. Please create it with your Instagram Reel audio URLs.")
        return

    # --- Phase 1: Use Playwright to get direct Reel permalinks ---
    logging.info("Phase 1: Collecting direct Reel permalinks using Playwright.")
    direct_reel_permalinks = []
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(USER_DATA_DIR, headless=False, channel="chrome")
        page = browser.new_page()

        try:
            logging.info("Checking Instagram login status...")
            page.goto("https://www.instagram.com/", timeout=60000)
            
            home_icon_selector = 'a[href="/"]'
            try:
                 page.wait_for_selector(home_icon_selector, timeout=5000)
                 logging.info("Already logged in.")
            except TimeoutError:
                logging.info("You are not logged in. Please log in to Instagram in the browser window.")
                logging.info("After you log in, the script will continue automatically.")
                page.wait_for_selector(home_icon_selector, timeout=300000) # 5 minutes timeout for login
                logging.info("Login successful.")

            try:
                save_info_button = page.get_by_role("button", name="Not Now")
                if save_info_button.is_visible():
                    logging.info("Dismissing 'Save Your Login Info' popup.")
                    save_info_button.click()
            except Exception:
                pass

            try:
                notifications_button = page.get_by_role("button", name="Not Now")
                if notifications_button.is_visible():
                    logging.info("Dismissing 'Turn on Notifications' popup.")
                    notifications_button.click()
            except Exception:
                pass
            
            for i, audio_url in enumerate(original_audio_urls, 1):
                logging.info(f"Processing audio URL ({i}/{len(original_audio_urls)}): {audio_url}")
                permalink_found = False
                try:
                    page.goto(audio_url, timeout=60000)
                    
                    reel_selector = 'a[href^="/reel/"]'
                    page.wait_for_selector(reel_selector, timeout=30000)
                    page.click(reel_selector)
                    
                    page.wait_for_url(re.compile(r"instagram\.com/reel/"), timeout=30000)
                    current_reel_url = page.url
                    direct_reel_permalinks.append(current_reel_url)
                    logging.info(f"Found direct Reel permalink: {current_reel_url}")
                    permalink_found = True

                except Exception as e:
                    logging.error(f"Failed to get direct Reel permalink for {audio_url}: {e}")
                
                if not permalink_found:
                    with open(ERROR_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                        writer = csv.writer(f)
                        writer.writerow([audio_url, f"Failed to extract permalink: {e}"])

        except Exception as e:
            logging.error(f"An unexpected error occurred during Playwright phase: {e}")
        finally:
            logging.info("Closing browser for Playwright phase.")
            browser.close()

    if not direct_reel_permalinks:
        logging.error("No direct Reel permalinks were found. Exiting.")
        return

    # Overwrite links.csv with the collected direct Reel permalinks
    with open(LINKS_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        for link in direct_reel_permalinks:
            writer.writerow([link])
    logging.info(f"'{LINKS_FILE}' updated with {len(direct_reel_permalinks)} direct Reel permalinks.")

    # --- Phase 2: Download and process audio using yt-dlp ---
    logging.info("Phase 2: Downloading and processing audio using yt-dlp.")
    processed_urls = []
    
    # Re-read links.csv to get the new direct reel permalinks
    try:
        with open(LINKS_FILE, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            processed_urls = [row[0] for row in reader if row]
    except FileNotFoundError:
        logging.error(f"'{LINKS_FILE}' not found after update. This should not happen.")
        return

    # Clear manifest and error logs for Phase 2
    with open(MANIFEST_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Original_URL', 'Output_File'])
    with open(ERROR_LOG_FILE, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Original_URL', 'Error'])

    for i, reel_url in enumerate(processed_urls, 1):
        output_path, dl_error = download_and_process_video(reel_url, reel_url, i)
        if dl_error:
            with open(ERROR_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([reel_url, dl_error])
        else:
            with open(MANIFEST_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([reel_url, output_path])

    logging.info("All tasks completed.")


if __name__ == '__main__':
    main()
