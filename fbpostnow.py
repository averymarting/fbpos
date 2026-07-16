import asyncio, io, json, os, random
from pathlib import Path
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
STORAGE_STATE = "storage_state.json"
UPLOADED_JSON = Path("uploaded.json")

GDRIVE_UPLOAD_FOLDER_ENV = "GDRIVE_UPLOAD_FOLDER_ID"
GOOGLE_SHEET_ID_ENV = "GOOGLE_SHEET_ID"

# One scope list covering both Drive (read) and Sheets/Drive metadata (gspread)
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

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

def load_google_credentials():
    """
    Accepts GOOGLE_CREDENTIALS_JSON in either of two formats:

    1. Service account key: {"type": "service_account", "client_email": ..., "private_key": ...}
       -> share the Drive folder + Sheet with client_email.

    2. OAuth "authorized user" token: {"token": ..., "refresh_token": ...,
       "client_id": ..., "client_secret": ..., "token_uri": ...}
       -> this is a token for whichever Google account you personally
          consented with; that account must already have access to the
          folder/sheet (no separate sharing step needed).
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON env var is missing")
    creds_data = json.loads(creds_json)

    if creds_data.get("type") == "service_account":
        return ServiceAccountCredentials.from_service_account_info(creds_data, scopes=SCOPES)

    if "refresh_token" in creds_data:
        return UserCredentials.from_authorized_user_info(creds_data, scopes=SCOPES)

    raise RuntimeError(
        "GOOGLE_CREDENTIALS_JSON doesn't look like a service account key "
        "(needs \"type\": \"service_account\") or an OAuth authorized-user "
        "token (needs a \"refresh_token\" field)."
    )

# ─────────────────────────────────────────────
# Google Drive
# ─────────────────────────────────────────────
def build_drive_service(creds):
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

def gdrive_download_video(service, file_id: str, dest_dir: str):
    dest_path = os.path.join(dest_dir, random_fb_filename())
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
    return dest_path

# ─────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────
def get_caption_and_url(creds):
    sheet_id = os.environ.get(GOOGLE_SHEET_ID_ENV)
    client = gspread.authorize(creds)

    sheet = client.open_by_key(sheet_id).sheet1
    rows = sheet.get_all_records()

    if not rows:
        raise RuntimeError("Sheet1 has no data rows")

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

        # Load the logged-in Facebook session written from the FB_STORAGE_STATE
        # secret. Without this, Playwright opens reels/create/ logged OUT.
        if Path(STORAGE_STATE).exists():
            context = await browser.new_context(storage_state=STORAGE_STATE)
        else:
            raise RuntimeError(
                f"{STORAGE_STATE} not found — the FB_STORAGE_STATE secret was "
                "not written to disk before the script ran."
            )

        page = await context.new_page()
        await page.goto("https://www.facebook.com/reels/create/")
        await page.locator('input[type="file"]').set_input_files(video_path)
        await asyncio.sleep(5)
        await page.keyboard.type(caption)
        await asyncio.sleep(5)
        # NOTE: this still doesn't click "Publish" — add that selector once
        # you've confirmed the flow manually.
        await browser.close()

# ─────────────────────────────────────────────
# Main Loop
# ─────────────────────────────────────────────
async def main():
    creds = load_google_credentials()
    drive_service = build_drive_service(creds)

    folder_id = os.environ.get(GDRIVE_UPLOAD_FOLDER_ENV)
    if not folder_id:
        raise RuntimeError("GDRIVE_UPLOAD_FOLDER_ID env var is missing")

    videos = gdrive_list_videos(drive_service, folder_id)
    if not videos:
        print("No videos found in the Drive folder.")
        return

    for v in videos:
        if already_uploaded(v["id"]):
            continue
        local_path = gdrive_download_video(drive_service, v["id"], ".")
        caption = get_caption_and_url(creds)
        await upload_reel(caption, local_path)
        mark_uploaded(v["id"])
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
