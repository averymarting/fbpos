import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_TXT  = "facebook_cookies.txt"
CAPTIONS_TXT = "captions.txt"
VIDEO_FILE   = "testing.mp4"
SCREENSHOTS_DIR = Path("screenshots")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_netscape_cookies(txt_file: str):
    cookies = []
    with open(txt_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 7:
                parts = line.split()
            if len(parts) < 7:
                print(f"⚠️  Skipping malformed cookie line {line_num}: {line[:80]}")
                continue
            domain, _, path, secure_s, expires, name, value = parts[:7]
            cookie = {
                "name":     name,
                "value":    value,
                "domain":   domain if domain.startswith('.') else f".{domain}",
                "path":     path,
                "secure":   secure_s.upper() == 'TRUE',
                "httpOnly": False,
                "sameSite": "None" if secure_s.upper() == 'TRUE' else "Lax",
            }
            if expires.lstrip('-').isdigit() and int(expires) > 0:
                cookie["expires"] = int(expires)
            cookies.append(cookie)
    print(f"✅ Loaded {len(cookies)} cookies")
    return cookies


async def save_screenshot(page, name: str):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    for p in [SCREENSHOTS_DIR / f"{name}.png", Path(f"{name}.png")]:
        try:
            await page.screenshot(path=str(p), full_page=False)
            print(f"📸 {p}")
        except Exception as e:
            print(f"⚠️  Screenshot failed {p}: {e}")


async def dump_html(page, filename: str):
    try:
        html = await page.content()
        Path(filename).write_text(html, encoding="utf-8")
        print(f"   📄 {filename} saved")
    except Exception as e:
        print(f"   ⚠️  HTML dump failed: {e}")


async def force_tap(page, locator):
    """Touch-tap → force-click → JS-click fallback chain."""
    box = await locator.bounding_box()
    if box:
        try:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            await page.touchscreen.tap(cx, cy)
            print(f"   ✅ tap at ({cx:.0f},{cy:.0f})")
            return True
        except Exception as e:
            print(f"   — tap failed: {e}")
    try:
        await locator.click(force=True, timeout=5_000)
        print("   ✅ force-click")
        return True
    except Exception as e:
        print(f"   — force-click failed: {e}")
    try:
        await locator.evaluate("el => el.click()")
        print("   ✅ JS click")
        return True
    except Exception as e:
        print(f"   — JS click failed: {e}")
    return False


async def click_next(page) -> bool:
    """
    Click the Next button using exact aria-label="Next".
    Waits up to 10 s for it to appear, then force-taps it.
    Returns True if clicked.
    """
    sel = '[aria-label="Next"][role="button"]'
    try:
        btn = page.locator(sel).first
        await btn.wait_for(state="visible", timeout=10_000)
        disabled = await btn.get_attribute("aria-disabled")
        if disabled == "true":
            print("   ⚠️  Next button is disabled")
            return False
        ok = await force_tap(page, btn)
        return ok
    except Exception as e:
        print(f"   — Next button not found: {e}")
        return False


async def get_modal_title(page) -> str:
    """Return the current modal/dialog title text, e.g. 'Create reel' or 'Edit reel'."""
    for sel in ['[role="dialog"] h2', '[role="dialog"] [role="heading"]',
                'h2', 'div[data-testid="reel-composer-title"]']:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                txt = (await el.inner_text()).strip()
                if txt:
                    return txt
        except Exception:
            pass
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def upload_reel(caption: str, video_path: str):
    if not Path(video_path).exists():
        print(f"❌ Video file not found: {video_path}")
        return
    print(f"🎬 Video   : {video_path}  ({Path(video_path).stat().st_size // 1024} KB)")
    print(f"📝 Caption : {caption[:80]}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ]
        )

        context = await browser.new_context(
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

        # ── Cookies ───────────────────────────────────────────────────────────
        if not Path(COOKIES_TXT).exists():
            print(f"❌ {COOKIES_TXT} not found")
            await browser.close()
            return
        cookies = load_netscape_cookies(COOKIES_TXT)
        if not cookies:
            print("❌ No cookies loaded")
            await browser.close()
            return
        await context.add_cookies(cookies)
        page = await context.new_page()

        # ── STEP 1: Load Facebook ─────────────────────────────────────────────
        print("\n🌐 Opening Facebook…")
        try:
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"❌ Load failed: {e}")
            await save_screenshot(page, "00_load_failed")
            await browser.close()
            return
        await asyncio.sleep(8)
        await save_screenshot(page, "01_after_load")

        # ── STEP 2: Verify login ──────────────────────────────────────────────
        print("🔍 Checking login…")
        if "login" in page.url or "checkpoint" in page.url:
            print("❌ Not logged in")
            await save_screenshot(page, "02_login_failed")
            await browser.close()
            return
        login_ok = False
        for sel in ['[aria-label="Home"]', '[data-pagelet="LeftRail"]',
                    'div[role="feed"]', 'span:has-text("What\'s on your mind?")']:
            if await page.locator(sel).count() > 0:
                login_ok = True
                print(f"   ✅ Logged in (via {sel})")
                break
        if not login_ok:
            print("❌ Login check failed")
            await dump_html(page, "02_login_failed.html")
            await save_screenshot(page, "02_login_failed")
            await browser.close()
            return
        await save_screenshot(page, "02_logged_in")

        # ── STEP 3: Open Reels create page ───────────────────────────────────
        print("\n🎬 Opening Reels creation page…")
        try:
            await page.goto("https://www.facebook.com/reels/create/",
                            wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"❌ Nav failed: {e}")
            await save_screenshot(page, "03_nav_failed")
            await browser.close()
            return
        await asyncio.sleep(8)
        await save_screenshot(page, "03_reels_page")
        print(f"   URL: {page.url}")
        title = await get_modal_title(page)
        print(f"   Modal title: {title}")

        # ── STEP 4: Attach video ──────────────────────────────────────────────
        print("\n📁 Attaching video…")
        uploaded = False

        # Strategy A: direct hidden file input
        for sel in ['input[type="file"][accept*="video"]', 'input[type="file"]']:
            try:
                inp = page.locator(sel).first
                if await inp.count() > 0:
                    print(f"   🎯 Direct file input: {sel}")
                    await inp.set_input_files(video_path)
                    print(f"   ✅ File attached via direct input")
                    uploaded = True
                    break
            except Exception as e:
                print(f"   — Direct input failed ({sel}): {e}")

        # Strategy B: click upload button → file chooser
        if not uploaded:
            for sel in [
                'div[role="button"]:has-text("Select video")',
                'div[role="button"]:has-text("Upload")',
                '[aria-label="Select video"]',
                'span:has-text("Select video from computer")',
            ]:
                el = page.locator(sel).first
                if await el.count() > 0:
                    print(f"   🎯 Upload button: {sel}")
                    try:
                        async with page.expect_file_chooser(timeout=15_000) as fc_info:
                            await el.click(force=True)
                        fc = await fc_info.value
                        await fc.set_files(video_path)
                        print(f"   ✅ File via chooser")
                        uploaded = True
                        break
                    except Exception as e:
                        print(f"   — Chooser failed: {e}")

        if not uploaded:
            print("❌ Could not attach video")
            await dump_html(page, "04_upload_failed.html")
            await save_screenshot(page, "04_upload_failed")
            await browser.close()
            return

        # ── STEP 5: Wait for video to process ────────────────────────────────
        print("\n⏳ Waiting for video to process (30 s)…")
        await asyncio.sleep(30)
        await save_screenshot(page, "05_video_processed")
        title = await get_modal_title(page)
        print(f"   Modal: {title}")

        # ─────────────────────────────────────────────────────────────────────
        # DYNAMIC STEP LOOP
        # We don't know how many steps FB shows. We loop: detect what's on
        # screen, act on it, then advance. Max 6 iterations to avoid loops.
        # ─────────────────────────────────────────────────────────────────────
        print("\n🔄 Starting dynamic step loop…")
        caption_done = False

        for step_num in range(1, 7):
            title = await get_modal_title(page)
            print(f"\n--- Step {step_num}: modal='{title}' ---")
            await save_screenshot(page, f"step{step_num:02d}_start")

            # ── Detect Share button (final step) ─────────────────────────────
            share_btn = page.locator('[aria-label="Share"][role="button"]').first
            share_visible = False
            try:
                share_visible = await share_btn.is_visible()
            except Exception:
                pass

            if share_visible:
                print("   🎯 Share button visible — this is the final step")
                # Type caption here if not done yet (some flows put caption here)
                if not caption_done:
                    await try_type_caption(page, caption)
                    caption_done = True
                await asyncio.sleep(1)
                print("   📤 Clicking Share…")
                ok = await force_tap(page, share_btn)
                if ok:
                    print("   ✅ Share clicked!")
                    break
                else:
                    print("   ❌ Share click failed")
                    await dump_html(page, f"step{step_num:02d}_share_failed.html")
                    break

            # ── Detect caption textarea (Edit reel step) ──────────────────────
            # From HTML: aria-placeholder="Describe your reel..." data-lexical-editor="true"
            caption_field = page.locator(
                '[aria-placeholder="Describe your reel..."],'
                '[data-lexical-editor="true"],'
                'div[role="textbox"][contenteditable="true"]'
            ).first
            caption_visible = False
            try:
                caption_visible = await caption_field.is_visible()
            except Exception:
                pass

            if caption_visible and not caption_done:
                print("   ✍️  Caption field found — typing caption…")
                typed = await try_type_caption(page, caption)
                if typed:
                    caption_done = True
                    print("   ✅ Caption typed")
                else:
                    print("   ⚠️  Caption typing failed")
                await asyncio.sleep(2)
                await save_screenshot(page, f"step{step_num:02d}_caption_typed")

            # ── Click Next to advance ─────────────────────────────────────────
            print("   ➡️  Clicking Next…")
            next_ok = await click_next(page)
            if not next_ok:
                print("   ⚠️  Next not found — dumping HTML and stopping")
                await dump_html(page, f"step{step_num:02d}_no_next.html")
                await save_screenshot(page, f"step{step_num:02d}_stuck")
                break

            print("   ✅ Next clicked — waiting 5 s for next screen…")
            await asyncio.sleep(5)

        # ── FINAL: Wait for publish confirmation ──────────────────────────────
        print("\n⏳ Waiting for publish (20 s)…")
        await asyncio.sleep(20)
        await save_screenshot(page, "09_final_result")
        await dump_html(page, "09_final_page.html")

        for sel in [
            'span:has-text("Your reel is now shared")',
            'span:has-text("Your reel was shared")',
            'span:has-text("Reel posted")',
            'span:has-text("Published")',
        ]:
            if await page.locator(sel).count() > 0:
                print(f"🎉 Reel published! (confirmed: {sel})")
                break
        else:
            print("🎉 Process completed — check 09_final_result.png")

        await browser.close()


async def try_type_caption(page, caption: str) -> bool:
    """Try all known caption field selectors and type into the first visible one."""
    selectors = [
        # Confirmed from HTML dump: aria-placeholder + data-lexical-editor
        '[aria-placeholder="Describe your reel..."]',
        'div[data-lexical-editor="true"]',
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"]',
        'textarea[placeholder*="escribe" i]',
        'textarea',
    ]
    for sel in selectors:
        try:
            field = page.locator(sel).first
            if await field.count() == 0:
                continue
            if not await field.is_visible():
                continue
            await field.click()
            await asyncio.sleep(0.5)
            # Clear any existing text first
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.2)
            await page.keyboard.type(caption, delay=40)
            print(f"      ✅ Typed via: {sel}")
            return True
        except Exception as e:
            print(f"      — {sel}: {e}")
    return False


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    caption_path = Path(CAPTIONS_TXT)
    if caption_path.exists():
        caption = caption_path.read_text(encoding="utf-8").strip()
        print(f"📝 Caption loaded from {CAPTIONS_TXT}")
    else:
        caption = "Check out my latest reel! #reels"
        print(f"⚠️  {CAPTIONS_TXT} not found — using default")

    asyncio.run(upload_reel(caption=caption, video_path=VIDEO_FILE))
