import asyncio, io, json, os, random, time
from pathlib import Path
from datetime import datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
STORAGE_STATE = "storage_state.json"
UPLOADED_JSON = Path("uploaded.json")
SCREENSHOTS_DIR = Path("screenshots")

GDRIVE_UPLOAD_FOLDER_ENV = "GDRIVE_UPLOAD_FOLDER_ID"
GOOGLE_SHEET_ID_ENV = "GOOGLE_SHEET_ID"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def random_fb_filename():
    return f"{random.randint(10**14, 10**15-1)}.mp4"

def mark_uploaded(file_id):
    uploaded = json.loads(UPLOADED_JSON.read_text()) if UPLOADED_JSON.exists() else {}
    uploaded[file_id] = True
    UPLOADED_JSON.write_text(json.dumps(uploaded))

def already_uploaded(file_id):
    if not UPLOADED_JSON.exists():
        return False
    uploaded = json.loads(UPLOADED_JSON.read_text())
    return uploaded.get(file_id, False)

# ─────────────────────────────────────────────
# Google Drive
# ─────────────────────────────────────────────
def build_drive_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_data = json.loads(creds_json)
    creds = Credentials.from_authorized_user_info(creds_data)
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def gdrive_list_videos(service, folder_id: str):
    query = f"'{folder_id}' in parents and trashed = false"
    result = service.files().list(
        q=query,
        fields="files(id, name, mimeType, createdTime, size)",
        orderBy="createdTime",
        pageSize=10,
    ).execute()
    return result.get("files", [])

def gdrive_download_video(service, file_id: str, file_name: str, dest_dir: str):
    dest_path = os.path.join(dest_dir, random_fb_filename())
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8*1024*1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
    return dest_path

# ─────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────
def get_caption_and_url():
    sheet_id = os.environ.get(GOOGLE_SHEET_ID_ENV)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_data = json.loads(creds_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_data, scope)
    client = gspread.authorize(creds)

    sheet = client.open_by_key(sheet_id).sheet1
    rows = sheet.get_all_records()

    # Expect headers: caption, urls
    caption_template = rows[0]["caption"]
    urls = [row["urls"] for row in rows if row.get("urls")]

    used_urls = Path("used_urls.json")
    used = json.loads(used_urls.read_text()) if used_urls.exists() else []

    for url in urls:
        if url not in used:
            used.append(url)
            used_urls.write_text(json.dumps(used))
            return caption_template.replace("https://example.com", url)

    return caption_template  # fallback

# ─────────────────────────────────────────────
# Upload Flow (simplified)
# ─────────────────────────────────────────────
async def upload_reel(caption: str, video_path: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.facebook.com/reels/create/")
        await page.locator('input[type="file"]').set_input_files(video_path)
        await asyncio.sleep(5)
        await page.keyboard.type(caption)
        await asyncio.sleep(5)
        await browser.close()

# ─────────────────────────────────────────────
# Main Loop
# ─────────────────────────────────────────────
async def main():
    service = build_drive_service()
    folder_id = os.environ.get(GDRIVE_UPLOAD_FOLDER_ENV)
    videos = gdrive_list_videos(service, folder_id)

    for v in videos:
        if already_uploaded(v["id"]):
            continue
        local_path = gdrive_download_video(service, v["id"], v["name"], ".")
        caption = get_caption_and_url()
        await upload_reel(caption, local_path)
        mark_uploaded(v["id"])
        await asyncio.sleep(60)  # interval from sheet can be added here

if __name__ == "__main__":
    asyncio.run(main())
