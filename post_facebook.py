"""
post_facebook.py
────────────────
• Downloads the next video from Google Drive folder GDRIVE_UPLOAD_FOLDER_ID
• Uploads it as a Facebook Reel
• On confirmed success, moves the file to GDRIVE_UPLOADED_FOLDER_ID
• Runs every 2 hours via the built-in scheduler (or once via --once flag)

Environment variables / GitHub Secrets required:
  FB_STORAGE_STATE          – full JSON of Playwright storage_state
  GOOGLE_CREDENTIALS_JSON   – full JSON of Google OAuth credentials file
  GDRIVE_UPLOAD_FOLDER_ID   – ID of the folder that holds pending videos
  GDRIVE_UPLOADED_FOLDER_ID – ID of the "fbuploaded" destination folder
  CAPTIONS_FILE_ID          – (optional) Google Drive file ID of captions.txt
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime

# ── optional scheduler ────────────────────────────────────────────────────────
try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False

# ── Google Drive ──────────────────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    HAS_GDRIVE = True
except ImportError:
    HAS_GDRIVE = False

from playwright.async_api import async_playwright

# ─────────────────────────────────────────────────────────────────────────────
# Config — all overridable via environment
# ─────────────────────────────────────────────────────────────────────────────
STORAGE_STATE         = "storage_state.json"
COOKIES_TXT           = "facebook_cookies.txt"
CAPTIONS_TXT          = "captions.txt"
SCREENSHOTS_DIR       = Path("screenshots")

FB_STORAGE_STATE_ENV      = "FB_STORAGE_STATE"
GDRIVE_CREDS_ENV          = "GOOGLE_CREDENTIALS_JSON"
UPLOAD_FOLDER_ENV         = "GDRIVE_UPLOAD_FOLDER_ID"
UPLOADED_FOLDER_ENV       = "GDRIVE_UPLOADED_FOLDER_ID"
CAPTIONS_FILE_ENV         = "CAPTIONS_FILE_ID"

UPLOAD_INTERVAL_HOURS     = 2
VIDEO_EXTENSIONS          = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# ─────────────────────────────────────────────────────────────────────────────
# Google Drive helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_drive_service():
    """Build and return an authenticated Drive v3 service, refreshing token if needed."""
    if not HAS_GDRIVE:
        raise RuntimeError(
            "google-auth / google-api-python-client not installed.\n"
            "Run: pip install google-auth google-auth-httplib2 google-api-python-client"
        )

    creds_json = os.environ.get(GDRIVE_CREDS_ENV)
    if not creds_json:
        raise RuntimeError(f"❌ Env var {GDRIVE_CREDS_ENV} not set")

    creds_data = json.loads(creds_json)

    creds = Credentials(
        token         = creds_data.get("token"),
        refresh_token = creds_data.get("refresh_token"),
        token_uri     = creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id     = creds_data.get("client_id"),
        client_secret = creds_data.get("client_secret"),
        scopes        = creds_data.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )

    # Auto-refresh if expired
    if creds.expired and creds.refresh_token:
        print("🔄 Google token expired — refreshing…")
        creds.refresh(Request())
        print("✅ Google token refreshed")

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def gdrive_list_videos(service, folder_id: str) -> list[dict]:
    """Return list of video files in folder, oldest first."""
    ext_filter = " or ".join(
        f"name contains '{ext}'" for ext in VIDEO_EXTENSIONS
    )
    query = (
        f"'{folder_id}' in parents "
        f"and trashed = false "
        f"and ({ext_filter})"
    )
    result = service.files().list(
        q=query,
        fields="files(id, name, mimeType, createdTime, size)",
        orderBy="createdTime",
        pageSize=10,
    ).execute()
    files = result.get("files", [])
    print(f"📂 Found {len(files)} video(s) in upload folder")
    for f in files:
        size_mb = int(f.get("size", 0)) // (1024 * 1024)
        print(f"   • {f['name']}  ({size_mb} MB)  id={f['id']}")
    return files


def gdrive_download_video(service, file_id: str, file_name: str, dest_dir: str) -> str:
    """Download a Drive file to dest_dir. Returns local path."""
    dest_path = os.path.join(dest_dir, file_name)
    print(f"⬇️  Downloading {file_name} …")
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"   {int(status.progress() * 100)}%", end="\r")
    size_mb = os.path.getsize(dest_path) // (1024 * 1024)
    print(f"\n✅ Downloaded: {dest_path}  ({size_mb} MB)")
    return dest_path


def gdrive_move_to_uploaded(service, file_id: str, file_name: str,
                             src_folder_id: str, dst_folder_id: str):
    """Move a file from upload folder → fbuploaded folder."""
    print(f"📦 Moving '{file_name}' → fbuploaded folder…")
    service.files().update(
        fileId=file_id,
        addParents=dst_folder_id,
        removeParents=src_folder_id,
        fields="id, parents",
    ).execute()
    print(f"✅ Moved to fbuploaded: {file_name}")


def gdrive_get_caption(service) -> str | None:
    """Download captions.txt from Drive if CAPTIONS_FILE_ID is set."""
    file_id = os.environ.get(CAPTIONS_FILE_ENV)
    if not file_id:
        return None
    try:
        print("📝 Fetching captions.txt from Google Drive…")
        content = service.files().get_media(fileId=file_id).execute()
        text = content.decode("utf-8").strip() if isinstance(content, bytes) else content.strip()
        print(f"✅ Caption fetched ({len(text)} chars)")
        return text
    except Exception as e:
        print(f"⚠️  Could not fetch captions from Drive: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Facebook / Playwright helpers
# ─────────────────────────────────────────────────────────────────────────────

def resolve_fb_storage_state() -> str | None:
    env_val = os.environ.get(FB_STORAGE_STATE_ENV)
    if env_val:
        try:
            json.loads(env_val)
            return env_val
        except json.JSONDecodeError:
            print(f"⚠️  {FB_STORAGE_STATE_ENV} is not valid JSON — ignoring")
    if Path(STORAGE_STATE).exists():
        return Path(STORAGE_STATE).read_text(encoding="utf-8")
    return None


def load_netscape_cookies(txt_file: str):
    cookies = []
    with open(txt_file, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                parts = line.split()
            if len(parts) < 7:
                print(f"⚠️  Skipping malformed cookie line {line_num}")
                continue
            domain, _, path, secure_s, expires, name, value = parts[:7]
            cookie = {
                "name": name, "value": value,
                "domain": domain if domain.startswith(".") else f".{domain}",
                "path": path, "secure": secure_s.upper() == "TRUE",
                "httpOnly": False,
                "sameSite": "None" if secure_s.upper() == "TRUE" else "Lax",
            }
            if expires.lstrip("-").isdigit() and int(expires) > 0:
                cookie["expires"] = int(expires)
            cookies.append(cookie)
    print(f"✅ Loaded {len(cookies)} cookies (legacy)")
    return cookies


async def save_screenshot(page, name: str):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    for p in [SCREENSHOTS_DIR / f"{name}.png", Path(f"{name}.png")]:
        try:
            await page.screenshot(path=str(p), full_page=False)
            print(f"📸 {p}")
        except Exception as e:
            print(f"⚠️  Screenshot {p}: {e}")


async def dump_html(page, filename: str):
    try:
        Path(filename).write_text(await page.content(), encoding="utf-8")
        print(f"   📄 {filename}")
    except Exception as e:
        print(f"   ⚠️  HTML dump: {e}")


async def force_tap(page, locator) -> bool:
    box = await locator.bounding_box()
    if box:
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        for method in [
            lambda: page.touchscreen.tap(cx, cy),
            lambda: page.mouse.click(cx, cy),
        ]:
            try:
                await method()
                return True
            except Exception:
                pass
    for method in [
        lambda: locator.click(force=True, timeout=5_000),
        lambda: locator.evaluate("el => el.click()"),
    ]:
        try:
            await method()
            return True
        except Exception:
            pass
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Nuclear Continue-button clicker
# ─────────────────────────────────────────────────────────────────────────────

def is_picker_url(url: str) -> bool:
    return any(x in url for x in ["device-based", "/caa/", "login/caa", "login/identifier"])

def is_hard_login_url(url: str) -> bool:
    return "/login" in url and not is_picker_url(url)


async def nuke_continue_button(page, label: str) -> bool:
    """Try every possible method to click the Continue button. Returns True if URL changed."""
    print(f"   🔨 nuke_continue [{label}]")

    SELECTORS = [
        '[aria-label^="Continue"]', '[aria-label*="Continue"]',
        'div[role="button"][aria-label^="Continue"]',
        'a[role="button"][aria-label^="Continue"]', 'a[aria-label^="Continue"]',
        'div[role="button"]:has-text("Continue")', 'a[role="button"]:has-text("Continue")',
        'span:text-is("Continue")', 'span:has-text("Continue")',
        'button:has-text("Continue")',
        'div[role="button"]:first-of-type', 'a[role="button"]:first-of-type',
    ]

    # Wait up to 12 s for any selector to appear
    for _ in range(12):
        for sel in SELECTORS:
            try:
                if await page.locator(sel).count() > 0:
                    print(f"      button found via: {sel}")
                    break
            except Exception:
                pass
        else:
            await asyncio.sleep(1)
            continue
        break

    url_before = page.url

    # ── Phase 2: selector × method grid ──────────────────────────────────
    for sel in SELECTORS:
        try:
            locs = page.locator(sel)
            count = await locs.count()
            if count == 0:
                continue
            indices = list(dict.fromkeys([0, count - 1] + list(range(min(count, 5)))))
            for idx in indices:
                loc = locs.nth(idx)
                tag = f"sel={sel!r}[{idx}]"

                # A – standard
                try:
                    await loc.scroll_into_view_if_needed(timeout=3_000)
                    await loc.click(timeout=5_000)
                    print(f"      ✅ A:click {tag}")
                    await asyncio.sleep(5)
                    if page.url != url_before:
                        return True
                except Exception as e:
                    print(f"      — A {tag}: {e}")

                # B – force
                try:
                    await loc.click(force=True, timeout=5_000)
                    print(f"      ✅ B:force {tag}")
                    await asyncio.sleep(5)
                    if page.url != url_before:
                        return True
                except Exception as e:
                    print(f"      — B {tag}: {e}")

                # C – touchscreen
                try:
                    box = await loc.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        await page.touchscreen.tap(cx, cy)
                        print(f"      ✅ C:touch {tag} ({cx:.0f},{cy:.0f})")
                        await asyncio.sleep(5)
                        if page.url != url_before:
                            return True
                except Exception as e:
                    print(f"      — C {tag}: {e}")

                # D – mouse move + click
                try:
                    box = await loc.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        await page.mouse.move(cx, cy)
                        await asyncio.sleep(0.3)
                        await page.mouse.click(cx, cy)
                        print(f"      ✅ D:mouse {tag}")
                        await asyncio.sleep(5)
                        if page.url != url_before:
                            return True
                except Exception as e:
                    print(f"      — D {tag}: {e}")

                # E – JS .click()
                try:
                    await loc.evaluate("el => el.click()")
                    print(f"      ✅ E:js {tag}")
                    await asyncio.sleep(5)
                    if page.url != url_before:
                        return True
                except Exception as e:
                    print(f"      — E {tag}: {e}")

                # F – full mouse event dispatch
                try:
                    await loc.evaluate("""el => {
                        ['mouseover','mouseenter','mousemove','mousedown','mouseup','click']
                        .forEach(t => el.dispatchEvent(
                            new MouseEvent(t, {bubbles:true,cancelable:true,view:window})
                        ));
                    }""")
                    print(f"      ✅ F:events {tag}")
                    await asyncio.sleep(5)
                    if page.url != url_before:
                        return True
                except Exception as e:
                    print(f"      — F {tag}: {e}")

                # G – focus + Enter
                try:
                    await loc.focus()
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Enter")
                    print(f"      ✅ G:enter {tag}")
                    await asyncio.sleep(5)
                    if page.url != url_before:
                        return True
                except Exception as e:
                    print(f"      — G {tag}: {e}")

                # H – focus + Space
                try:
                    await loc.focus()
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Space")
                    print(f"      ✅ H:space {tag}")
                    await asyncio.sleep(5)
                    if page.url != url_before:
                        return True
                except Exception as e:
                    print(f"      — H {tag}: {e}")

        except Exception as outer_e:
            print(f"   — outer error {sel!r}: {outer_e}")

    # ── Phase 3: coordinate brute-force ──────────────────────────────────
    print("   🔨 Phase 3 — coordinate grid…")
    vw = page.viewport_size or {"width": 1280, "height": 900}
    w, h = vw["width"], vw["height"]
    for xp in [0.60, 0.70, 0.75, 0.80]:
        for yp in [0.40, 0.50, 0.55, 0.60]:
            cx, cy = int(w * xp), int(h * yp)
            try:
                await page.mouse.click(cx, cy)
                await asyncio.sleep(3)
                if page.url != url_before:
                    print(f"      ✅ coord ({cx},{cy}) worked")
                    return True
            except Exception:
                pass

    # ── Phase 4: Tab navigation ───────────────────────────────────────────
    print("   🔨 Phase 4 — Tab navigation…")
    await page.keyboard.press("Tab")
    for i in range(20):
        try:
            tag = await page.evaluate(
                "() => document.activeElement ? document.activeElement.outerHTML.slice(0,200) : ''"
            )
            if "continue" in tag.lower():
                await page.keyboard.press("Enter")
                await asyncio.sleep(4)
                if page.url != url_before:
                    print(f"      ✅ Tab#{i} + Enter")
                    return True
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.2)
        except Exception:
            pass

    # ── Phase 5: JS full-DOM text search ─────────────────────────────────
    print("   🔨 Phase 5 — JS DOM text search…")
    try:
        hit = await page.evaluate("""() => {
            const candidates = Array.from(document.querySelectorAll(
                'div[role="button"],a[role="button"],button,a,span[tabindex]'
            ));
            const btn = candidates.find(el => {
                const txt = (el.textContent||el.innerText||el.getAttribute('aria-label')||'').trim();
                return /^continue/i.test(txt) || /continue as/i.test(txt);
            });
            if (!btn) return null;
            btn.scrollIntoView({behavior:'instant',block:'center'});
            ['mousedown','mouseup','click'].forEach(t =>
                btn.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true}))
            );
            btn.click();
            return btn.outerHTML.slice(0,200);
        }""")
        if hit:
            print(f"      ✅ JS text search clicked: {hit[:80]}")
            await asyncio.sleep(5)
            if page.url != url_before:
                return True
    except Exception as e:
        print(f"      — JS text search: {e}")

    # ── Phase 6: direct bypass navigation ────────────────────────────────
    print("   🔨 Phase 6 — direct URL bypass…")
    try:
        await page.goto("https://www.facebook.com/?sk=h_chr",
                        wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(5)
        if not is_picker_url(page.url) and not is_hard_login_url(page.url):
            print(f"      ✅ Direct nav bypassed picker → {page.url}")
            return True
    except Exception as e:
        print(f"      — direct nav: {e}")

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Facebook login check
# ─────────────────────────────────────────────────────────────────────────────

FEED_SELECTORS = [
    '[aria-label="Home"]', '[data-pagelet="LeftRail"]', 'div[role="feed"]',
    '[aria-label="Create"]', 'span:has-text("What\'s on your mind?")',
    'div[aria-label="Stories"]', 'div[aria-label="Reels"]',
    'div[data-pagelet="FeedUnit_0"]', 'div[role="main"]',
]

async def ensure_logged_in(page) -> bool:
    """Handle picker / login wall. Returns True when feed confirmed."""
    print("🔍 Checking login state…")
    for outer in range(6):
        url = page.url
        print(f"   [attempt {outer+1}] URL: {url}")

        if is_hard_login_url(url):
            print("❌ Real login wall — cookies expired. Export fresh storage_state.json.")
            await save_screenshot(page, "02_login_failed")
            return False

        if "checkpoint" in url:
            print("❌ Account checkpoint — manual action required")
            await save_screenshot(page, "02_checkpoint")
            return False

        if is_picker_url(url):
            await dump_html(page, f"02_picker_attempt{outer+1}.html")
            ok = await nuke_continue_button(page, f"attempt={outer+1}")
            await save_screenshot(page, f"02_after_continue_{outer+1}")
            if not ok:
                print(f"   ⚠️  All click methods failed attempt {outer+1}")
                await asyncio.sleep(3)
            continue  # re-evaluate URL

        # Check for feed elements
        for sel in FEED_SELECTORS:
            if await page.locator(sel).count() > 0:
                print(f"   ✅ Logged in — confirmed: {sel}")
                return True

        print(f"   ⏳ Feed not ready (attempt {outer+1}) — waiting 4 s…")
        await asyncio.sleep(4)

    print("❌ Login check exhausted")
    await dump_html(page, "02_login_failed.html")
    await save_screenshot(page, "02_login_failed")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Upload flow
# ─────────────────────────────────────────────────────────────────────────────

async def upload_reel(caption: str, video_path: str) -> bool:
    """Run the full upload. Returns True on confirmed publish."""
    if not Path(video_path).exists():
        print(f"❌ Video not found: {video_path}")
        return False

    size_mb = Path(video_path).stat().st_size // (1024 * 1024)
    print(f"🎬 Video   : {video_path}  ({size_mb} MB)")
    print(f"📝 Caption : {caption[:80]}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars", "--disable-dev-shm-usage",
            ]
        )

        storage_state_json = resolve_fb_storage_state()
        context_kwargs = dict(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="Asia/Karachi",
            accept_downloads=True,
            permissions=["clipboard-read", "clipboard-write"],
        )

        using_storage = False
        if storage_state_json:
            try:
                state = json.loads(storage_state_json)
                context_kwargs["storage_state"] = state
                using_storage = True
                print(f"✅ storage_state: {len(state.get('cookies',[]))} cookies, "
                      f"{len(state.get('origins',[]))} origin(s)")
            except json.JSONDecodeError:
                print("⚠️  storage_state invalid — trying legacy cookies")

        context = await browser.new_context(**context_kwargs)
        published = False

        try:
            if not using_storage:
                if not Path(COOKIES_TXT).exists():
                    print(f"❌ No session source found")
                    return False
                await context.add_cookies(load_netscape_cookies(COOKIES_TXT))

            published = await _run_upload_flow(context, caption, video_path)

        finally:
            try:
                fresh = await context.storage_state()
                Path(STORAGE_STATE).write_text(json.dumps(fresh), encoding="utf-8")
                print(f"\n💾 Refreshed storage_state saved ({len(fresh.get('cookies',[]))} cookies)")
            except Exception as e:
                print(f"⚠️  Could not save storage_state: {e}")
            await browser.close()

    return published


async def _run_upload_flow(context, caption: str, video_path: str) -> bool:
    page = await context.new_page()
    published = False

    # ── Step 1: Load Facebook ─────────────────────────────────────────────
    print("\n🌐 Opening Facebook…")
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print(f"❌ Load failed: {e}")
        await save_screenshot(page, "00_load_failed")
        return False
    await asyncio.sleep(8)
    await save_screenshot(page, "01_after_load")
    print(f"   URL: {page.url}")

    # ── Step 2: Login check ───────────────────────────────────────────────
    if not await ensure_logged_in(page):
        return False
    await save_screenshot(page, "02_logged_in")

    # ── Step 3: Navigate to Reels create ─────────────────────────────────
    print("\n🎬 Navigating to Reels create page…")
    try:
        await page.goto("https://www.facebook.com/reels/create/",
                        wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print(f"❌ Nav failed: {e}")
        await save_screenshot(page, "03_nav_failed")
        return False
    await asyncio.sleep(8)
    await save_screenshot(page, "03_reels_create_page")
    print(f"   URL: {page.url}")

    # ── Step 4: Attach video ──────────────────────────────────────────────
    print("\n📁 Attaching video…")
    uploaded = False

    for sel in ['input[type="file"][accept*="video"]', 'input[type="file"]']:
        try:
            inp = page.locator(sel).first
            if await inp.count() > 0:
                await inp.set_input_files(video_path)
                print(f"   ✅ Direct file input: {sel}")
                uploaded = True
                break
        except Exception as e:
            print(f"   — direct input {sel}: {e}")

    if not uploaded:
        for sel in [
            'div[role="button"]:has-text("Select video")',
            'div[role="button"]:has-text("Upload")',
            'div[role="button"]:has-text("Add video")',
            'span:has-text("Select video")', 'span:has-text("Select Video")',
            '[aria-label="Select video"]', 'div[aria-label="Add to reel"]',
            'div:has-text("Select video from computer")',
        ]:
            el = page.locator(sel).first
            if await el.count() > 0:
                print(f"   🎯 Upload button: {sel}")
                try:
                    async with page.expect_file_chooser(timeout=15_000) as fc_info:
                        await el.click(force=True)
                    fc = await fc_info.value
                    await fc.set_files(video_path)
                    print("   ✅ File chooser upload")
                    uploaded = True
                    break
                except Exception as e:
                    print(f"   — file chooser {sel}: {e}")

    await save_screenshot(page, "04_after_upload_attempt")
    await dump_html(page, "04_page_state.html")
    if not uploaded:
        print("❌ Could not attach video")
        return False

    # ── Step 5: Click Next ────────────────────────────────────────────────
    print("\n➡️  Clicking Next…")
    for sel in [
        'div[aria-label="Next"][role="button"]', 'div[role="button"]:has-text("Next")',
        'span:has-text("Next")', 'button:has-text("Next")',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            await btn.wait_for(state="visible", timeout=15_000)
            if await btn.get_attribute("aria-disabled") == "true":
                continue
            if await force_tap(page, btn):
                print(f"   ✅ Next via: {sel}")
                break
        except Exception as e:
            print(f"   — Next {sel}: {e}")

    await asyncio.sleep(3)
    await save_screenshot(page, "05a_after_first_next")

    # ── Step 5b: Wait for caption field ──────────────────────────────────
    print("\n⏳ Waiting for caption field…")
    caption_selectors_wait = [
        'div[contenteditable="true"][aria-placeholder="Describe your reel..."]',
        'div[contenteditable="true"][aria-placeholder*="Describe your reel" i]',
    ]
    for elapsed in range(0, 60, 2):
        if any([await page.locator(s).count() > 0 for s in caption_selectors_wait]):
            print(f"   ✅ Caption field ready after {elapsed}s")
            break
        print(f"   …{elapsed}s")
        await asyncio.sleep(2)

    await save_screenshot(page, "05_after_processing")

    # ── Step 6: Enter caption ─────────────────────────────────────────────
    print("\n✍️  Entering caption…")

    caption_selectors = [
        ("aria-placeholder exact",  'div[contenteditable="true"][aria-placeholder="Describe your reel..."]'),
        ("aria-placeholder loose",  'div[contenteditable="true"][aria-placeholder*="Describe your reel" i]'),
        ("aria-placeholder reel",   'div[contenteditable="true"][aria-placeholder*="reel" i]'),
        ("lexical editor",          'div[data-lexical-editor="true"][contenteditable="true"]'),
        ("role textbox",            'div[role="textbox"][contenteditable="true"]'),
        ("aria-label description",  'div[aria-label*="description" i][contenteditable="true"]'),
        ("aria-label caption",      'div[aria-label*="caption" i][contenteditable="true"]'),
        ("textarea description",    'textarea[placeholder*="description" i]'),
        ("textarea caption",        'textarea[placeholder*="caption" i]'),
        ("any contenteditable",     'div[contenteditable="true"]'),
    ]

    async def verify_text(loc) -> bool:
        try:
            txt = await loc.evaluate("el => (el.innerText||el.textContent||el.value||'').trim()")
            return len(txt) > 0
        except Exception:
            return False

    async def try_type(field) -> bool:
        await field.scroll_into_view_if_needed(timeout=5_000)
        await field.click(timeout=5_000); await asyncio.sleep(0.4)
        await field.click(timeout=5_000); await asyncio.sleep(0.3)
        await page.keyboard.type(caption, delay=40); await asyncio.sleep(0.5)
        return await verify_text(field)

    async def try_seq(field) -> bool:
        await field.click(timeout=5_000); await asyncio.sleep(0.3)
        await field.press_sequentially(caption, delay=30); await asyncio.sleep(0.5)
        return await verify_text(field)

    async def try_js_type(field) -> bool:
        await field.evaluate("el => el.focus()"); await asyncio.sleep(0.3)
        await page.keyboard.type(caption, delay=40); await asyncio.sleep(0.5)
        return await verify_text(field)

    async def try_execcmd(field) -> bool:
        await field.click(timeout=5_000); await asyncio.sleep(0.3)
        await field.evaluate("""(el, t) => {
            el.focus();
            const s = window.getSelection(); s.removeAllRanges();
            const r = document.createRange(); r.selectNodeContents(el); s.addRange(r);
            document.execCommand('insertText', false, t);
        }""", caption); await asyncio.sleep(0.5)
        return await verify_text(field)

    async def try_dom(field) -> bool:
        await field.evaluate("""(el, t) => {
            el.focus(); el.textContent = t;
            ['beforeinput','input','change'].forEach(ev =>
                el.dispatchEvent(new InputEvent(ev, {bubbles:true,cancelable:true,data:t}))
            );
        }""", caption); await asyncio.sleep(0.5)
        return await verify_text(field)

    async def try_paste(field) -> bool:
        await field.click(timeout=5_000); await asyncio.sleep(0.3)
        await field.evaluate("""(el, t) => {
            el.focus();
            const dt = new DataTransfer(); dt.setData('text/plain', t);
            el.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:dt}));
        }""", caption); await asyncio.sleep(0.5)
        return await verify_text(field)

    async def try_clipboard(field) -> bool:
        await field.click(timeout=5_000); await asyncio.sleep(0.3)
        try:
            await page.evaluate("t => navigator.clipboard.writeText(t)", caption)
        except Exception as e:
            print(f"         clipboard.writeText blocked: {e}"); return False
        await page.keyboard.press("Control+V"); await asyncio.sleep(0.5)
        return await verify_text(field)

    caption_methods = [
        ("keyboard.type",         try_type),
        ("press_sequentially",    try_seq),
        ("js-focus+keyboard",     try_js_type),
        ("execCommand",           try_execcmd),
        ("DOM injection",         try_dom),
        ("synthetic paste",       try_paste),
        ("clipboard Ctrl+V",      try_clipboard),
    ]

    caption_ok = False
    for sel_name, sel in caption_selectors:
        if caption_ok:
            break
        field = page.locator(sel).first
        if await field.count() == 0:
            continue
        try:
            await field.wait_for(state="visible", timeout=5_000)
        except Exception:
            continue
        print(f"   🎯 {sel_name}")
        for m_name, m_fn in caption_methods:
            try:
                await field.evaluate("el => { el.textContent=''; el.innerText=''; }")
                await asyncio.sleep(0.2)
                if await m_fn(field):
                    print(f"      ✅ {m_name}")
                    caption_ok = True
                    break
                else:
                    print(f"      — {m_name}: empty")
            except Exception as e:
                print(f"      — {m_name}: {e}")

    if not caption_ok:
        print("⚠️  Caption failed — continuing anyway")
        await dump_html(page, "06_caption_failed.html")
    await save_screenshot(page, "06_after_caption")

    # ── Step 7: Next → Post ───────────────────────────────────────────────
    print("\n📤 Clicking Next then Post…")
    for sel in [
        'div[aria-label="Next"][role="button"]', 'div[role="button"]:has-text("Next")',
        'span:has-text("Next")', 'button:has-text("Next")',
    ]:
        try:
            btn = page.locator(sel).last
            if await btn.count() == 0: continue
            await btn.wait_for(state="visible", timeout=8_000)
            if await btn.get_attribute("aria-disabled") == "true": continue
            if await force_tap(page, btn):
                print(f"   ✅ Next via: {sel}")
                await asyncio.sleep(3)
                await save_screenshot(page, "07_after_next")
                break
        except Exception as e:
            print(f"   — Next {sel}: {e}")

    async def panel_open() -> bool:
        try:
            return (await page.locator('div[aria-label="Post"][role="button"]').count() > 0
                    or await page.locator('text="Reel settings"').count() > 0)
        except Exception:
            return False

    post_selectors = [
        ("aria-label Post",    'div[aria-label="Post"][role="button"]'),
        ("text Post exact",    'div[role="button"]:text-is("Post")'),
        ("span Post",          'span:text-is("Post")'),
        ("submit",             'button[type="submit"]'),
        ("aria-label Publish", 'div[aria-label="Publish"][role="button"]'),
        ("aria-label Share",   'div[aria-label="Share now"][role="button"]'),
        ("text Post",          'div[role="button"]:has-text("Post")'),
        ("text Publish",       'div[role="button"]:has-text("Publish")'),
        ("text Share now",     'div[role="button"]:has-text("Share now")'),
    ]

    post_clicked = False
    for sel_name, sel in post_selectors:
        if post_clicked: break
        btn = page.locator(sel).last
        if await btn.count() == 0: continue
        try:
            await btn.wait_for(state="visible", timeout=5_000)
        except Exception:
            continue
        if await btn.get_attribute("aria-disabled") == "true": continue
        print(f"   🎯 Post candidate: {sel_name}")
        for m_name, m_fn in [
            ("force_tap",    lambda b: force_tap(page, b)),
            ("dispatch",     lambda b: b.evaluate("""el => {
                ['mousedown','mouseup','click'].forEach(t =>
                    el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true})));
            }""")),
            ("focus+Enter",  lambda b: (b.focus(), asyncio.sleep(0.2), page.keyboard.press("Enter"))),
        ]:
            try:
                was_open = await panel_open()
                await m_fn(btn)
                await asyncio.sleep(2)
                if was_open and not await panel_open():
                    print(f"      ✅ {m_name} — panel closed")
                    post_clicked = True
                    break
                print(f"      — {m_name}: panel still open")
            except Exception as e:
                print(f"      — {m_name}: {e}")

    if not post_clicked:
        print("⚠️  Could not confirm Post click")
        await dump_html(page, "07_post_failed.html")
    await save_screenshot(page, "07_after_post")

    # ── Step 8: Confirm publish ───────────────────────────────────────────
    print("\n⏳ Waiting 30 s for publish confirmation…")
    await asyncio.sleep(30)
    await save_screenshot(page, "08_final_result")
    await dump_html(page, "08_final_page.html")

    confirm_selectors = [
        'span:has-text("Your reel is now shared")',
        'span:has-text("Reel posted")',
        'span:has-text("Published")',
        'span:has-text("Your reel")',
        'div:has-text("Your reel was shared")',
    ]
    for sel in confirm_selectors:
        if await page.locator(sel).count() > 0:
            print(f"🎉 PUBLISHED! (confirmed: {sel})")
            published = True
            break
    else:
        # Absence of the settings panel + absence of login page = likely published
        if post_clicked and not await panel_open():
            print("🎉 PUBLISHED (inferred — panel closed, no error detected)")
            published = True
        else:
            print("⚠️  Could not confirm — check 08_final_result.png")

    return published


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator: Drive → upload → move
# ─────────────────────────────────────────────────────────────────────────────

def run_once():
    """Download one video from Drive, upload to FB, move on success."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"🚀 Run started at {ts}")
    print(f"{'='*60}")

    upload_folder   = os.environ.get(UPLOAD_FOLDER_ENV)
    uploaded_folder = os.environ.get(UPLOADED_FOLDER_ENV)

    if not upload_folder:
        print(f"❌ Env var {UPLOAD_FOLDER_ENV} not set")
        return
    if not uploaded_folder:
        print(f"❌ Env var {UPLOADED_FOLDER_ENV} not set")
        return

    # ── Build Drive service ───────────────────────────────────────────────
    try:
        service = build_drive_service()
    except Exception as e:
        print(f"❌ Drive auth failed: {e}")
        return

    # ── Get caption ───────────────────────────────────────────────────────
    caption = gdrive_get_caption(service)
    if not caption:
        cap_path = Path(CAPTIONS_TXT)
        if cap_path.exists():
            caption = cap_path.read_text(encoding="utf-8").strip()
            print(f"📝 Caption from local {CAPTIONS_TXT}")
        else:
            caption = "Check out my latest reel! #reels #viral"
            print("📝 Using default caption")

    # ── List videos in upload folder ──────────────────────────────────────
    try:
        videos = gdrive_list_videos(service, upload_folder)
    except Exception as e:
        print(f"❌ Could not list Drive folder: {e}")
        return

    if not videos:
        print("ℹ️  No videos in upload folder — nothing to do")
        return

    # Pick the oldest video
    video_meta = videos[0]
    file_id   = video_meta["id"]
    file_name = video_meta["name"]
    print(f"\n🎯 Picked: {file_name}  (id={file_id})")

    # ── Download to temp dir ──────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        try:
            local_path = gdrive_download_video(service, file_id, file_name, tmp)
        except Exception as e:
            print(f"❌ Download failed: {e}")
            return

        # ── Upload to Facebook ────────────────────────────────────────────
        try:
            published = asyncio.run(upload_reel(caption=caption, video_path=local_path))
        except Exception as e:
            print(f"❌ Upload exception: {e}")
            published = False

    # ── Move to fbuploaded on success ─────────────────────────────────────
    if published:
        try:
            gdrive_move_to_uploaded(service, file_id, file_name,
                                    upload_folder, uploaded_folder)
        except Exception as e:
            print(f"⚠️  Move to fbuploaded failed: {e}")
    else:
        print(f"⚠️  Upload not confirmed — file stays in upload folder for retry")

    print(f"\n✅ Run complete. Published={published}")


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────

def run_scheduled():
    if not HAS_SCHEDULE:
        print("❌ 'schedule' package not installed. Run: pip install schedule")
        sys.exit(1)

    print(f"⏰ Scheduler started — uploading every {UPLOAD_INTERVAL_HOURS} hours")
    print("   Run --once to execute immediately without scheduling")

    # Run immediately on start, then every 2 hours
    run_once()
    schedule.every(UPLOAD_INTERVAL_HOURS).hours.do(run_once)

    while True:
        schedule.run_pending()
        time.sleep(30)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--once" in sys.argv or os.environ.get("RUN_ONCE"):
        run_once()
    else:
        run_scheduled()
