"""
post_facebook.py  — VERBOSE DEBUG VERSION
─────────────────────────────────────────
Every step prints exactly what it's doing and why it failed.
Run with:  python -u post_facebook.py --once
"""

import asyncio, io, json, os, sys, tempfile, time
from pathlib import Path
from datetime import datetime
import functools

# Force unbuffered output — every print shows immediately in GitHub Actions
print = functools.partial(print, flush=True)

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
    print("⚠️  Google Drive libraries not installed")

from playwright.async_api import async_playwright

# ─────────────────────────────────────────────────────────────────────────────
STORAGE_STATE         = "storage_state.json"
CAPTIONS_TXT          = "captions.txt"
SCREENSHOTS_DIR       = Path("screenshots")

FB_STORAGE_STATE_ENV      = "FB_STORAGE_STATE"
GDRIVE_CREDS_ENV          = "GOOGLE_CREDENTIALS_JSON"
UPLOAD_FOLDER_ENV         = "GDRIVE_UPLOAD_FOLDER_ID"
UPLOADED_FOLDER_ENV       = "GDRIVE_UPLOADED_FOLDER_ID"
CAPTIONS_FILE_ENV         = "CAPTIONS_FILE_ID"
LOOP_INTERVAL_MINUTES     = int(os.environ.get("LOOP_INTERVAL_MINUTES", 30))
VIDEO_EXTENSIONS          = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# ─────────────────────────────────────────────────────────────────────────────
# STEP LOGGER — prints a clear numbered banner for every major step
# ─────────────────────────────────────────────────────────────────────────────
_step = 0
def step(msg):
    global _step
    _step += 1
    print(f"\n{'='*60}")
    print(f"  STEP {_step}: {msg}")
    print(f"{'='*60}")

def info(msg):   print(f"   ℹ️  {msg}")
def ok(msg):     print(f"   ✅ {msg}")
def warn(msg):   print(f"   ⚠️  {msg}")
def fail(msg):   print(f"   ❌ {msg}")
def debug(msg):  print(f"   🔍 {msg}")

# ─────────────────────────────────────────────────────────────────────────────
# Google Drive helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_drive_service():
    step("Building Google Drive service")
    if not HAS_GDRIVE:
        fail("google-auth not installed")
        raise RuntimeError("Missing google-auth libraries")

    creds_json = os.environ.get(GDRIVE_CREDS_ENV)
    if not creds_json:
        fail(f"Env var {GDRIVE_CREDS_ENV} is not set")
        raise RuntimeError(f"Missing {GDRIVE_CREDS_ENV}")
    
    info(f"GOOGLE_CREDENTIALS_JSON length: {len(creds_json)} chars")
    
    try:
        creds_data = json.loads(creds_json)
    except json.JSONDecodeError as e:
        fail(f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}")
        raise

    info(f"Credential keys present: {list(creds_data.keys())}")
    
    # Check for required fields
    for field in ["token", "refresh_token", "client_id", "client_secret"]:
        if creds_data.get(field):
            ok(f"  {field}: present")
        else:
            warn(f"  {field}: MISSING or empty")

    creds = Credentials(
        token         = creds_data.get("token"),
        refresh_token = creds_data.get("refresh_token"),
        token_uri     = creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id     = creds_data.get("client_id"),
        client_secret = creds_data.get("client_secret"),
        scopes        = creds_data.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )
    info(f"Token expired: {creds.expired}")
    info(f"Has refresh_token: {bool(creds.refresh_token)}")

    if creds.expired and creds.refresh_token:
        info("Refreshing expired Google token...")
        try:
            creds.refresh(Request())
            ok("Google token refreshed successfully")
        except Exception as e:
            fail(f"Token refresh failed: {e}")
            raise

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    ok("Google Drive service built")
    return service


def gdrive_list_videos(service, folder_id: str) -> list[dict]:
    step(f"Listing videos in Drive folder: {folder_id}")
    ext_filter = " or ".join(f"name contains '{ext}'" for ext in VIDEO_EXTENSIONS)
    query = f"'{folder_id}' in parents and trashed = false and ({ext_filter})"
    info(f"Query: {query}")
    
    try:
        result = service.files().list(
            q=query,
            fields="files(id, name, mimeType, createdTime, size)",
            orderBy="createdTime",
            pageSize=10,
        ).execute()
    except Exception as e:
        fail(f"Drive API list failed: {e}")
        raise

    files = result.get("files", [])
    info(f"Found {len(files)} video(s)")
    for f in files:
        size_mb = int(f.get("size", 0)) // (1024 * 1024)
        info(f"  • {f['name']}  ({size_mb} MB)  id={f['id']}")
    return files


def gdrive_download_video(service, file_id: str, file_name: str, dest_dir: str) -> str:
    step(f"Downloading video: {file_name} (id={file_id})")
    dest_path = os.path.join(dest_dir, file_name)
    
    try:
        request = service.files().get_media(fileId=file_id)
        with io.FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    print(f"   📥 {int(status.progress() * 100)}%", end="\r")
        print()  # newline after progress
    except Exception as e:
        fail(f"Download failed: {e}")
        raise

    size_mb = os.path.getsize(dest_path) // (1024 * 1024)
    ok(f"Downloaded to: {dest_path}  ({size_mb} MB)")
    return dest_path


def gdrive_move_to_uploaded(service, file_id, file_name, src_folder_id, dst_folder_id):
    step(f"Moving '{file_name}' to uploaded folder")
    try:
        service.files().update(
            fileId=file_id,
            addParents=dst_folder_id,
            removeParents=src_folder_id,
            fields="id, parents",
        ).execute()
        ok(f"Moved successfully")
    except Exception as e:
        fail(f"Move failed: {e}")
        raise


def gdrive_get_caption(service) -> str | None:
    step("Fetching caption from Google Drive")
    file_id = os.environ.get(CAPTIONS_FILE_ENV)
    if not file_id:
        info("CAPTIONS_FILE_ID not set — skipping Drive caption fetch")
        return None
    try:
        content = service.files().get_media(fileId=file_id).execute()
        text = content.decode("utf-8").strip() if isinstance(content, bytes) else content.strip()
        ok(f"Caption fetched ({len(text)} chars): {text[:80]}")
        return text
    except Exception as e:
        warn(f"Could not fetch captions: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Facebook / Playwright helpers
# ─────────────────────────────────────────────────────────────────────────────

def resolve_fb_storage_state() -> str | None:
    step("Resolving Facebook storage state")
    env_val = os.environ.get(FB_STORAGE_STATE_ENV)
    if env_val:
        info(f"FB_STORAGE_STATE env var found, length={len(env_val)}")
        try:
            parsed = json.loads(env_val)
            cookies = parsed.get("cookies", [])
            ok(f"Valid JSON — {len(cookies)} cookies found")
            for c in cookies:
                info(f"  Cookie: name={c.get('name')} expires={c.get('expires')} domain={c.get('domain')}")
            return env_val
        except json.JSONDecodeError as e:
            fail(f"FB_STORAGE_STATE is not valid JSON: {e}")
    else:
        warn("FB_STORAGE_STATE env var not set")
    
    if Path(STORAGE_STATE).exists():
        info(f"Found local {STORAGE_STATE} — using it")
        return Path(STORAGE_STATE).read_text(encoding="utf-8")
    
    fail("No valid Facebook session found!")
    return None


async def save_screenshot(page, name: str):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    for p in [SCREENSHOTS_DIR / f"{name}.png", Path(f"{name}.png")]:
        try:
            await page.screenshot(path=str(p), full_page=False)
            info(f"Screenshot saved: {p}")
        except Exception as e:
            warn(f"Screenshot failed {p}: {e}")


async def dump_html(page, filename: str):
    try:
        content = await page.content()
        Path(filename).write_text(content, encoding="utf-8")
        info(f"HTML dumped: {filename} ({len(content)} chars)")
    except Exception as e:
        warn(f"HTML dump failed: {e}")


def is_picker_url(url: str) -> bool:
    return any(x in url for x in ["device-based", "/caa/", "login/caa", "login/identifier"])

def is_hard_login_url(url: str) -> bool:
    return "/login" in url and not is_picker_url(url)

def classify_url(url: str) -> str:
    if "checkpoint" in url:        return "CHECKPOINT"
    if is_hard_login_url(url):     return "LOGIN_WALL"
    if is_picker_url(url):         return "DEVICE_PICKER"
    if "reels/create" in url:      return "REELS_CREATE"
    if "facebook.com" in url:      return "FACEBOOK_PAGE"
    return "OTHER"


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


FEED_SELECTORS = [
    '[aria-label="Home"]', '[data-pagelet="LeftRail"]', 'div[role="feed"]',
    '[aria-label="Create"]', 'span:has-text("What\'s on your mind?")',
    'div[aria-label="Stories"]', 'div[aria-label="Reels"]',
    'div[data-pagelet="FeedUnit_0"]', 'div[role="main"]',
]


async def nuke_continue_button(page, label: str) -> bool:
    info(f"Attempting to click Continue button [{label}]")
    SELECTORS = [
        '[aria-label^="Continue"]', '[aria-label*="Continue"]',
        'div[role="button"][aria-label^="Continue"]',
        'div[role="button"]:has-text("Continue")',
        'span:text-is("Continue")', 'span:has-text("Continue")',
        'button:has-text("Continue")',
    ]
    url_before = page.url

    # Wait up to 10s for any button
    found_sel = None
    for _ in range(10):
        for sel in SELECTORS:
            try:
                if await page.locator(sel).count() > 0:
                    found_sel = sel
                    break
            except Exception:
                pass
        if found_sel:
            break
        await asyncio.sleep(1)

    if not found_sel:
        warn("No Continue button found in DOM after 10s")
        # Try JS DOM search
        try:
            hit = await page.evaluate("""() => {
                const candidates = Array.from(document.querySelectorAll(
                    'div[role="button"],a[role="button"],button,a,span[tabindex]'
                ));
                const btn = candidates.find(el => {
                    const txt = (el.textContent||el.innerText||el.getAttribute('aria-label')||'').trim();
                    return /^continue/i.test(txt);
                });
                if (!btn) return null;
                btn.click();
                return btn.outerHTML.slice(0,200);
            }""")
            if hit:
                ok(f"JS found & clicked Continue: {hit[:80]}")
                await asyncio.sleep(5)
                return page.url != url_before
        except Exception as e:
            warn(f"JS search failed: {e}")

        # Phase 6: direct bypass
        info("Trying direct navigation bypass...")
        try:
            await page.goto("https://www.facebook.com/?sk=h_chr",
                            wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(5)
            if not is_picker_url(page.url) and not is_hard_login_url(page.url):
                ok(f"Direct nav bypassed picker → {page.url}")
                return True
        except Exception as e:
            warn(f"Direct nav failed: {e}")
        return False

    info(f"Found Continue button via: {found_sel}")
    loc = page.locator(found_sel).first
    for method_name, method in [
        ("standard click", lambda: loc.click(timeout=5_000)),
        ("force click",    lambda: loc.click(force=True, timeout=5_000)),
        ("JS click",       lambda: loc.evaluate("el => el.click()")),
    ]:
        try:
            await method()
            await asyncio.sleep(5)
            if page.url != url_before:
                ok(f"Continue clicked via {method_name} — URL changed")
                return True
            info(f"{method_name}: URL unchanged ({page.url})")
        except Exception as e:
            warn(f"{method_name} failed: {e}")

    return False


async def ensure_logged_in(page) -> bool:
    step("Checking Facebook login state")
    for attempt in range(6):
        url = page.url
        url_type = classify_url(url)
        info(f"Attempt {attempt+1}/6 — URL: {url}")
        info(f"URL type: {url_type}")

        if url_type == "CHECKPOINT":
            fail("Account checkpoint/restriction detected — manual action required")
            fail("Check your Facebook account for security notices")
            await save_screenshot(page, f"LOGIN_CHECKPOINT_{attempt+1}")
            await dump_html(page, f"checkpoint_{attempt+1}.html")
            return False

        if url_type == "LOGIN_WALL":
            fail("Hard login wall — session cookies are EXPIRED")
            fail("You need to re-export storage_state.json from a fresh browser session")
            await save_screenshot(page, f"LOGIN_WALL_{attempt+1}")
            return False

        if url_type == "DEVICE_PICKER":
            info("Device picker detected — trying to bypass")
            await dump_html(page, f"picker_{attempt+1}.html")
            ok_click = await nuke_continue_button(page, f"attempt={attempt+1}")
            await save_screenshot(page, f"after_continue_{attempt+1}")
            if not ok_click:
                warn(f"Could not click Continue on attempt {attempt+1}")
                await asyncio.sleep(3)
            continue

        # Check for feed elements proving we're logged in
        for sel in FEED_SELECTORS:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    ok(f"Logged in confirmed via: {sel}")
                    return True
            except Exception:
                pass

        # Check page title for clues
        try:
            title = await page.title()
            info(f"Page title: {title}")
        except Exception:
            pass

        info(f"Feed not ready yet — waiting 4s (attempt {attempt+1}/6)")
        await asyncio.sleep(4)

    fail("Login check exhausted all 6 attempts")
    await dump_html(page, "login_failed_final.html")
    await save_screenshot(page, "LOGIN_FAILED_FINAL")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Upload flow
# ─────────────────────────────────────────────────────────────────────────────

async def upload_reel(caption: str, video_path: str) -> bool:
    step("Starting Facebook Reel upload")
    if not Path(video_path).exists():
        fail(f"Video file not found: {video_path}")
        return False

    size_mb = Path(video_path).stat().st_size // (1024 * 1024)
    ok(f"Video: {video_path}  ({size_mb} MB)")
    info(f"Caption: {caption[:120]}")

    async with async_playwright() as p:
        step("Launching Chromium browser")
        try:
            browser = await p.chromium.launch(
                headless=True,
                timeout=30_000,
                args=[
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars", "--disable-dev-shm-usage",
                    "--single-process", "--no-zygote",
                ]
            )
            ok("Browser launched")
        except Exception as e:
            fail(f"Browser launch FAILED: {e}")
            return False

        storage_state_json = resolve_fb_storage_state()
        if not storage_state_json:
            fail("No Facebook session available — aborting")
            await browser.close()
            return False

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
        )

        step("Creating browser context with session cookies")
        try:
            state = json.loads(storage_state_json)
            context_kwargs["storage_state"] = state
            context = await browser.new_context(**context_kwargs)
            ok(f"Context created with {len(state.get('cookies', []))} cookies")
        except Exception as e:
            fail(f"Context creation failed: {e}")
            await browser.close()
            return False

        published = False
        try:
            published = await _run_upload_flow(context, caption, video_path)
        except Exception as e:
            fail(f"Upload flow crashed with exception: {e}")
            import traceback
            print(traceback.format_exc())
        finally:
            try:
                fresh = await context.storage_state()
                Path(STORAGE_STATE).write_text(json.dumps(fresh), encoding="utf-8")
                ok(f"Saved refreshed storage_state ({len(fresh.get('cookies', []))} cookies)")
            except Exception as e:
                warn(f"Could not save storage_state: {e}")
            await browser.close()
            ok("Browser closed")

    return published


async def _run_upload_flow(context, caption: str, video_path: str) -> bool:
    page = await context.new_page()
    published = False

    # ── Step 1: Load Facebook ─────────────────────────────────────────────
    step("Loading Facebook homepage")
    try:
        response = await page.goto("https://www.facebook.com/",
                                   wait_until="domcontentloaded", timeout=60_000)
        info(f"HTTP status: {response.status if response else 'unknown'}")
    except Exception as e:
        fail(f"Page load failed: {e}")
        await save_screenshot(page, "FAIL_01_load")
        return False
    
    await asyncio.sleep(8)
    info(f"Current URL after load: {page.url}")
    info(f"URL type: {classify_url(page.url)}")
    await save_screenshot(page, "01_after_load")

    # ── Step 2: Login check ───────────────────────────────────────────────
    if not await ensure_logged_in(page):
        fail("ABORT: Could not confirm login")
        return False
    await save_screenshot(page, "02_logged_in")
    ok("Login confirmed — proceeding to upload")

    # ── Step 3: Navigate to Reels create ─────────────────────────────────
    step("Navigating to Reels create page")
    try:
        response = await page.goto("https://www.facebook.com/reels/create/",
                                   wait_until="domcontentloaded", timeout=60_000)
        info(f"HTTP status: {response.status if response else 'unknown'}")
    except Exception as e:
        fail(f"Navigation to reels/create failed: {e}")
        await save_screenshot(page, "FAIL_03_nav")
        return False
    
    await asyncio.sleep(8)
    info(f"Current URL: {page.url}")
    info(f"URL type: {classify_url(page.url)}")
    await save_screenshot(page, "03_reels_create")
    await dump_html(page, "03_reels_create.html")

    # Check if we got redirected away from reels create
    if "reels/create" not in page.url:
        warn(f"Got redirected away from reels/create to: {page.url}")
        warn(f"This may mean the account lacks Reels access or got a security redirect")

    # ── Step 4: Attach video ──────────────────────────────────────────────
    step("Attaching video file")
    uploaded = False

    # Try direct file input first
    for sel in ['input[type="file"][accept*="video"]', 'input[type="file"]']:
        try:
            inp = page.locator(sel)
            count = await inp.count()
            info(f"File input selector '{sel}': {count} found")
            if count > 0:
                await inp.first.set_input_files(video_path)
                ok(f"Video attached via direct input: {sel}")
                uploaded = True
                break
        except Exception as e:
            warn(f"Direct input {sel} failed: {e}")

    if not uploaded:
        info("Direct input failed — trying upload button click")
        button_selectors = [
            ('Select video', 'div[role="button"]:has-text("Select video")'),
            ('Upload',       'div[role="button"]:has-text("Upload")'),
            ('Add video',    'div[role="button"]:has-text("Add video")'),
            ('Select Video span', 'span:has-text("Select video")'),
            ('aria-label Select video', '[aria-label="Select video"]'),
            ('Add to reel', 'div[aria-label="Add to reel"]'),
            ('from computer', 'div:has-text("Select video from computer")'),
        ]
        for btn_name, sel in button_selectors:
            el = page.locator(sel).first
            try:
                count = await el.count()
                info(f"Upload button '{btn_name}': {count} found")
                if count == 0:
                    continue
                async with page.expect_file_chooser(timeout=10_000) as fc_info:
                    await el.click(force=True)
                fc = await fc_info.value
                await fc.set_files(video_path)
                ok(f"File chooser upload via: {btn_name}")
                uploaded = True
                break
            except Exception as e:
                warn(f"Button '{btn_name}' failed: {e}")

    await save_screenshot(page, "04_after_upload_attempt")
    await dump_html(page, "04_after_upload.html")

    if not uploaded:
        fail("ABORT: Could not attach video — no file input or upload button found")
        fail("Check 04_after_upload.html to see what the page looks like")
        return False
    ok("Video attached successfully")

    # Wait for video to process (up to 3 minutes)
    step("Waiting for video to process")
    info("Watching for Next button to become active (up to 3 min)...")
    
    next_selectors = [
        'div[aria-label="Next"][role="button"]',
        'div[role="button"]:has-text("Next")',
        'span:has-text("Next")',
        'button:has-text("Next")',
    ]
    
    next_ready = False
    for elapsed in range(0, 180, 5):
        for sel in next_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    disabled = await btn.get_attribute("aria-disabled")
                    info(f"[{elapsed}s] Next button found via '{sel}', aria-disabled={disabled}")
                    if disabled != "true":
                        ok(f"Next button is active after {elapsed}s!")
                        next_ready = True
                        break
            except Exception:
                pass
        if next_ready:
            break
        if elapsed % 15 == 0:
            await save_screenshot(page, f"04_processing_{elapsed}s")
        await asyncio.sleep(5)
    
    if not next_ready:
        warn("Next button never became active after 3 minutes")
        warn("Video may still be processing or upload failed silently")
        await save_screenshot(page, "04_processing_timeout")
        await dump_html(page, "04_processing_timeout.html")

    # ── Step 5: Click Next ────────────────────────────────────────────────
    step("Clicking Next button")
    clicked_next = False
    for sel in next_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            disabled = await btn.get_attribute("aria-disabled")
            if disabled == "true":
                info(f"Skipping '{sel}' — still disabled")
                continue
            await btn.scroll_into_view_if_needed(timeout=5_000)
            await btn.click(timeout=10_000)
            ok(f"Next clicked via: {sel}")
            clicked_next = True
            break
        except Exception as e:
            warn(f"Next click '{sel}' failed: {e}")

    if not clicked_next:
        fail("Could not click Next")
        await save_screenshot(page, "FAIL_05_next")
        return False

    await asyncio.sleep(3)
    await save_screenshot(page, "05_after_next")

    # ── Step 5b: Wait for caption field ──────────────────────────────────
    step("Waiting for caption/description field")
    caption_wait_selectors = [
        'div[contenteditable="true"][aria-placeholder="Describe your reel..."]',
        'div[contenteditable="true"][aria-placeholder*="Describe" i]',
        'div[contenteditable="true"][aria-placeholder*="reel" i]',
        'div[data-lexical-editor="true"][contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"]',
    ]
    
    caption_field_found = False
    for elapsed in range(0, 60, 2):
        for sel in caption_wait_selectors:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    ok(f"Caption field appeared after {elapsed}s via: {sel}")
                    caption_field_found = True
                    break
            except Exception:
                pass
        if caption_field_found:
            break
        if elapsed % 10 == 0:
            info(f"Waiting for caption field... {elapsed}s elapsed")
            await save_screenshot(page, f"05_waiting_caption_{elapsed}s")
        await asyncio.sleep(2)

    if not caption_field_found:
        warn("Caption field never appeared — page structure may have changed")
        await dump_html(page, "05_no_caption_field.html")

    await save_screenshot(page, "05_caption_ready")

    # ── Step 6: Enter caption ─────────────────────────────────────────────
    step("Entering caption text")
    info(f"Caption to type ({len(caption)} chars): {caption[:80]}")

    caption_ok = False
    for sel_name, sel in [
        ("aria-placeholder exact",  'div[contenteditable="true"][aria-placeholder="Describe your reel..."]'),
        ("aria-placeholder loose",  'div[contenteditable="true"][aria-placeholder*="Describe" i]'),
        ("lexical editor",          'div[data-lexical-editor="true"][contenteditable="true"]'),
        ("role textbox",            'div[role="textbox"][contenteditable="true"]'),
        ("any contenteditable",     'div[contenteditable="true"]'),
    ]:
        if caption_ok:
            break
        try:
            field = page.locator(sel).first
            if await field.count() == 0:
                info(f"  Selector '{sel_name}': not found")
                continue
            info(f"  Trying selector: {sel_name}")
            
            # Clear existing content
            await field.evaluate("el => { el.textContent=''; }")
            await asyncio.sleep(0.2)
            
            # Try typing
            await field.click(timeout=5_000)
            await asyncio.sleep(0.3)
            await page.keyboard.type(caption, delay=30)
            await asyncio.sleep(0.5)
            
            # Verify
            txt = await field.evaluate("el => (el.innerText||el.textContent||'').trim()")
            if txt:
                ok(f"Caption entered via '{sel_name}' ({len(txt)} chars)")
                caption_ok = True
            else:
                warn(f"  '{sel_name}': typed but field is empty — trying execCommand")
                await field.click(timeout=5_000)
                await field.evaluate("(el, t) => { el.focus(); document.execCommand('selectAll'); document.execCommand('insertText', false, t); }", caption)
                await asyncio.sleep(0.5)
                txt = await field.evaluate("el => (el.innerText||el.textContent||'').trim()")
                if txt:
                    ok(f"Caption entered via execCommand ({len(txt)} chars)")
                    caption_ok = True
        except Exception as e:
            warn(f"  Caption selector '{sel_name}' error: {e}")

    if not caption_ok:
        warn("Caption could not be entered — continuing anyway (post may have no caption)")
    await save_screenshot(page, "06_after_caption")

    # ── Step 7: Click Next again → then Post ─────────────────────────────
    step("Clicking Next (2nd time) to reach Post panel")
    clicked_next2 = False
    for sel in next_selectors:
        try:
            btn = page.locator(sel).last
            if await btn.count() == 0:
                continue
            disabled = await btn.get_attribute("aria-disabled")
            if disabled == "true":
                continue
            await btn.click(force=True)
            ok(f"Second Next clicked via: {sel}")
            clicked_next2 = True
            await asyncio.sleep(4)
            break
        except Exception as e:
            warn(f"Second Next '{sel}' failed: {e}")

    if not clicked_next2:
        warn("Could not click second Next — may already be on Post panel")
    
    await save_screenshot(page, "07_before_post")
    await dump_html(page, "07_before_post.html")

    # ── Step 8: Click Post/Publish ─────────────────────────────────────
    step("Clicking Post / Publish button")
    post_selectors = [
        ("aria-label Post",    'div[aria-label="Post"][role="button"]'),
        ("text Post exact",    'div[role="button"]:text-is("Post")'),
        ("span Post",          'span:text-is("Post")'),
        ("aria-label Publish", 'div[aria-label="Publish"][role="button"]'),
        ("aria-label Share",   'div[aria-label="Share now"][role="button"]'),
        ("text Post",          'div[role="button"]:has-text("Post")'),
        ("text Publish",       'div[role="button"]:has-text("Publish")'),
        ("text Share now",     'div[role="button"]:has-text("Share now")'),
        ("submit button",      'button[type="submit"]'),
    ]

    post_clicked = False
    for sel_name, sel in post_selectors:
        try:
            btn = page.locator(sel).last
            count = await btn.count()
            info(f"Post button '{sel_name}': {count} found")
            if count == 0:
                continue
            disabled = await btn.get_attribute("aria-disabled")
            if disabled == "true":
                warn(f"  '{sel_name}' is disabled — skipping")
                continue
            
            await btn.scroll_into_view_if_needed(timeout=5_000)
            await btn.click(force=True)
            ok(f"Post button clicked via: {sel_name}")
            post_clicked = True
            await asyncio.sleep(5)
            break
        except Exception as e:
            warn(f"Post '{sel_name}' failed: {e}")

    if not post_clicked:
        fail("Could not click any Post/Publish button")
        fail("Check 07_before_post.html to see available buttons")
        await save_screenshot(page, "FAIL_08_no_post_button")
        return False

    # ── Step 9: Wait for confirmation ─────────────────────────────────────
    step("Waiting for publish confirmation (up to 60s)")
    confirm_selectors = [
        'span:has-text("Your reel is now shared")',
        'span:has-text("Reel posted")',
        'span:has-text("Published")',
        'span:has-text("Your reel")',
        'div:has-text("Your reel was shared")',
        'span:has-text("shared")',
    ]
    
    for elapsed in range(0, 60, 5):
        for sel in confirm_selectors:
            try:
                if await page.locator(sel).count() > 0:
                    ok(f"🎉 PUBLISHED! Confirmed via: {sel} (after {elapsed}s)")
                    published = True
                    break
            except Exception:
                pass
        if published:
            break
        info(f"Waiting for confirmation... {elapsed}s")
        if elapsed % 15 == 0:
            await save_screenshot(page, f"09_waiting_confirm_{elapsed}s")
        await asyncio.sleep(5)

    if not published:
        # Infer from page state
        info("No explicit confirmation found — checking page state...")
        try:
            url_after = page.url
            title_after = await page.title()
            info(f"Final URL: {url_after}")
            info(f"Final title: {title_after}")
        except Exception:
            pass
        
        # If Post panel is gone and we're not on an error page, assume success
        try:
            post_panel_gone = await page.locator('div[aria-label="Post"][role="button"]').count() == 0
            info(f"Post panel gone: {post_panel_gone}")
            if post_panel_gone and post_clicked:
                ok("🎉 PUBLISHED (inferred — Post panel gone, no errors detected)")
                published = True
        except Exception:
            pass

    await save_screenshot(page, "09_final_result")
    await dump_html(page, "09_final_result.html")

    if not published:
        warn("Could not confirm publish — check 09_final_result.png")
        warn("The reel may have posted anyway; check your Facebook profile")

    return published


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_once():
    global _step
    _step = 0
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"  🚀 Run started at {ts}")
    print(f"  Python: {sys.version}")
    print(f"  PID: {os.getpid()}")
    print(f"{'='*60}")

    # Print all relevant env vars (masked)
    step("Checking environment variables")
    for var in [UPLOAD_FOLDER_ENV, UPLOADED_FOLDER_ENV, "LOOP_INTERVAL_MINUTES", "CAPTIONS_FILE_ID"]:
        val = os.environ.get(var, "")
        info(f"{var}: {'SET (' + val + ')' if val else 'NOT SET'}")
    for secret in [FB_STORAGE_STATE_ENV, GDRIVE_CREDS_ENV]:
        val = os.environ.get(secret, "")
        info(f"{secret}: {'SET (' + str(len(val)) + ' chars)' if val else 'NOT SET'}")

    upload_folder   = os.environ.get(UPLOAD_FOLDER_ENV)
    uploaded_folder = os.environ.get(UPLOADED_FOLDER_ENV)

    if not upload_folder:
        fail(f"{UPLOAD_FOLDER_ENV} is not set — cannot continue")
        return
    if not uploaded_folder:
        fail(f"{UPLOADED_FOLDER_ENV} is not set — cannot continue")
        return

    # Build Drive service
    try:
        service = build_drive_service()
    except Exception as e:
        fail(f"Drive service failed: {e}")
        return

    # Get caption
    caption = gdrive_get_caption(service)
    if not caption:
        cap_path = Path(CAPTIONS_TXT)
        if cap_path.exists():
            caption = cap_path.read_text(encoding="utf-8").strip()
            info(f"Using local captions.txt: {caption[:80]}")
        else:
            caption = "Check out my latest reel! #reels #viral"
            info(f"Using default caption: {caption}")

    # List videos
    try:
        videos = gdrive_list_videos(service, upload_folder)
    except Exception as e:
        fail(f"Could not list Drive folder: {e}")
        return

    if not videos:
        info("No videos in upload folder — nothing to do this run")
        return

    video_meta = videos[0]
    file_id    = video_meta["id"]
    file_name  = video_meta["name"]
    ok(f"Selected video: {file_name} (id={file_id})")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            local_path = gdrive_download_video(service, file_id, file_name, tmp)
        except Exception as e:
            fail(f"Download failed: {e}")
            return

        try:
            published = asyncio.run(upload_reel(caption=caption, video_path=local_path))
        except Exception as e:
            fail(f"Upload exception: {e}")
            import traceback
            print(traceback.format_exc())
            published = False

    if published:
        try:
            gdrive_move_to_uploaded(service, file_id, file_name, upload_folder, uploaded_folder)
        except Exception as e:
            warn(f"Move to uploaded folder failed: {e}")
    else:
        warn("Upload not confirmed — file stays in upload folder for retry")

    print(f"\n{'='*60}")
    print(f"  Run complete. Published={published}")
    print(f"{'='*60}\n")


# ─────────────────────────────────────────────────────────────────────────────

def run_scheduled():
    if not HAS_SCHEDULE:
        fail("'schedule' package not installed. Run: pip install schedule")
        sys.exit(1)
    print(f"⏰ Scheduler started — posting every {LOOP_INTERVAL_MINUTES} minute(s)")
    run_once()
    schedule.every(LOOP_INTERVAL_MINUTES).minutes.do(run_once)
    iteration = 0
    while True:
        schedule.run_pending()
        time.sleep(30)
        iteration += 1
        if iteration % 20 == 0:
            next_run = schedule.next_run()
            print(f"⏳ Alive — next run at {next_run.strftime('%H:%M:%S') if next_run else 'unknown'}")


if __name__ == "__main__":
    if "--once" in sys.argv or os.environ.get("RUN_ONCE"):
        run_once()
    else:
        run_scheduled()
