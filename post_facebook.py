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


async def dump_html(page, filename="page_debug.html"):
    try:
        html = await page.content()
        Path(filename).write_text(html, encoding="utf-8")
        print(f"   📄 {filename} saved")
    except Exception as e:
        print(f"   ⚠️  Could not dump HTML: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def upload_reel(caption: str, video_path: str):
    # Validate files exist before even opening the browser
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
                "Mozilla/5.0 (Linux; Android 14; Pixel 7 Pro) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Mobile Safari/537.36"
            ),
            viewport={"width": 412, "height": 915},
            device_scale_factor=2.75,
            is_mobile=True,
            has_touch=True,
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

        # ── STEP 2: Check login ───────────────────────────────────────────────
        print("🔍 Checking login…")
        if "login" in page.url or "checkpoint" in page.url:
            print("❌ Not logged in — cookies expired")
            await save_screenshot(page, "02_login_failed")
            await browser.close()
            return

        login_ok = False
        for sel in ['span:has-text("What\'s on your mind?")', '[aria-label="Home"]',
                    'div[role="feed"]', '[aria-label="Create a post"]']:
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

        # ── STEP 3: Navigate to Reels creation ───────────────────────────────
        # Facebook's Reels upload lives at /reels/create — works on mobile UA
        print("\n🎬 Navigating to Reels creation page…")
        try:
            await page.goto("https://www.facebook.com/reels/create", wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"❌ Could not open reels/create: {e}")
            await save_screenshot(page, "03_reels_nav_failed")
            await browser.close()
            return
        await asyncio.sleep(6)
        await save_screenshot(page, "03_reels_page")

        # If redirected away (e.g. desktop reels page) try the mobile composer route
        if "reels/create" not in page.url:
            print(f"   ⚠️  Redirected to: {page.url} — trying mobile composer")
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(5)

            # Click the Reels nav icon (bottom bar on mobile)
            reel_nav_selectors = [
                'a[href*="/reels"]',
                '[aria-label="Reels"]',
                'span:has-text("Reels")',
            ]
            for sel in reel_nav_selectors:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await force_tap(page, el)
                    await asyncio.sleep(5)
                    await save_screenshot(page, "03b_after_reels_nav")
                    break

        await save_screenshot(page, "03_reels_loaded")

        # ── STEP 4: Find & click the file upload input ────────────────────────
        # Reels create page has a hidden <input type="file"> — we set_input_files on it
        print("\n📁 Looking for file upload input…")
        await asyncio.sleep(3)

        upload_selectors = [
            'input[type="file"][accept*="video"]',
            'input[type="file"]',
        ]

        uploaded = False
        for sel in upload_selectors:
            try:
                inp = page.locator(sel).first
                if await inp.count() > 0:
                    print(f"   🎯 Found upload input: {sel}")
                    await inp.set_input_files(video_path)
                    print(f"   ✅ File set: {video_path}")
                    uploaded = True
                    break
            except Exception as e:
                print(f"   — {sel} failed: {e}")

        if not uploaded:
            # Try clicking any "Select video" / "Add video" button first to reveal the input
            print("   🔄 No direct input found — trying to click upload button…")
            upload_btn_selectors = [
                'div[role="button"]:has-text("Select video")',
                'div[role="button"]:has-text("Add video")',
                'div[role="button"]:has-text("Upload")',
                'span:has-text("Select video")',
                'span:has-text("Add video")',
                '[aria-label="Select video"]',
            ]
            for sel in upload_btn_selectors:
                el = page.locator(sel).first
                if await el.count() > 0:
                    print(f"   🎯 Found upload button: {sel}")
                    # Listen for file chooser before clicking
                    async with page.expect_file_chooser(timeout=10_000) as fc_info:
                        await force_tap(page, el)
                    fc = await fc_info.value
                    await fc.set_files(video_path)
                    print(f"   ✅ File chosen via file chooser")
                    uploaded = True
                    break
                else:
                    print(f"   — Not found: {sel}")

        await save_screenshot(page, "04_after_upload_attempt")

        if not uploaded:
            print("❌ Could not attach video file")
            await dump_html(page, "04_upload_failed.html")
            await save_screenshot(page, "04_upload_failed")
            await browser.close()
            return

        # ── STEP 5: Wait for video to process / upload ────────────────────────
        print("\n⏳ Waiting for video to upload/process (30 s)…")
        await asyncio.sleep(30)
        await save_screenshot(page, "05_after_video_upload")

        # ── STEP 6: Enter caption / description ──────────────────────────────
        print("\n✍️  Entering caption…")

        caption_selectors = [
            # Standard Reels caption field
            'div[aria-label="Describe your reel"][contenteditable="true"]',
            'div[aria-label*="caption"][contenteditable="true"]',
            'div[aria-label*="Caption"][contenteditable="true"]',
            'div[aria-label*="description"][contenteditable="true"]',
            'div[aria-label*="Description"][contenteditable="true"]',
            # Generic contenteditable fallback
            'div[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"]',
            'textarea[aria-label*="caption" i]',
            'textarea[aria-label*="description" i]',
            'textarea[placeholder*="caption" i]',
            'textarea[placeholder*="description" i]',
            'textarea',
        ]

        caption_typed = False
        for sel in caption_selectors:
            try:
                field = page.locator(sel).first
                if await field.count() == 0:
                    continue
                await field.wait_for(state="visible", timeout=8_000)
                box = await field.bounding_box()
                if box:
                    await page.touchscreen.tap(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                else:
                    await field.click(force=True)
                await asyncio.sleep(1)
                await page.keyboard.type(caption, delay=50)
                print(f"   ✅ Caption typed via: {sel}")
                caption_typed = True
                break
            except Exception as e:
                print(f"   — {sel} failed: {e}")

        await save_screenshot(page, "06_after_caption_attempt")

        if not caption_typed:
            print("⚠️  Could not type caption — will still try to publish")
            await dump_html(page, "06_caption_failed.html")
        else:
            await asyncio.sleep(2)
            await save_screenshot(page, "06_caption_typed")

        # ── STEP 7: Click Publish / Share / Next ─────────────────────────────
        # Reels flow sometimes has a Next button before the final Publish
        print("\n📤 Looking for Next / Publish / Share button…")

        # First handle any "Next" step
        next_selectors = [
            'div[role="button"]:has-text("Next")',
            'span:has-text("Next")',
            'button:has-text("Next")',
        ]
        for sel in next_selectors:
            btn = page.locator(sel).last
            if await btn.count() > 0:
                print(f"   🎯 Found Next button: {sel}")
                await force_tap(page, btn)
                await asyncio.sleep(5)
                await save_screenshot(page, "07_after_next")
                break

        # Now find the final Publish/Share button
        publish_selectors = [
            'div[role="button"]:has-text("Publish")',
            'div[role="button"]:has-text("Share")',
            'button:has-text("Publish")',
            'button:has-text("Share")',
            'input[type="submit"][value="Publish"]',
            'input[type="submit"][value="Share"]',
            'span:has-text("Publish")',
            'span:has-text("Share")',
        ]

        published = False
        for sel in publish_selectors:
            try:
                btn = page.locator(sel).last
                if await btn.count() == 0:
                    continue
                await btn.wait_for(state="visible", timeout=8_000)
                disabled = await btn.get_attribute("aria-disabled")
                if disabled == "true":
                    print(f"   ⚠️  Button disabled: {sel}")
                    continue
                print(f"   🎯 Found publish button: {sel}")
                ok = await force_tap(page, btn)
                if ok:
                    print(f"   ✅ Publish clicked via: {sel}")
                    published = True
                    break
            except Exception as e:
                print(f"   — {sel} failed: {e}")

        await save_screenshot(page, "07_after_publish_attempt")

        if not published:
            print("❌ Could not click Publish/Share button")
            await dump_html(page, "07_publish_failed.html")
            await save_screenshot(page, "07_publish_failed")
            await browser.close()
            return

        # ── STEP 8: Wait for confirmation ─────────────────────────────────────
        print("\n⏳ Waiting for reel to publish (20 s)…")
        await asyncio.sleep(20)
        await save_screenshot(page, "08_after_publish_wait")

        print("⏳ Extra 10 s safety wait…")
        await asyncio.sleep(10)
        await save_screenshot(page, "09_final_result")

        # Success indicators
        success_selectors = [
            'span:has-text("Your reel is now shared")',
            'span:has-text("Reel posted")',
            'span:has-text("Published")',
            'div:has-text("Your reel")',
        ]
        for sel in success_selectors:
            if await page.locator(sel).count() > 0:
                print(f"🎉 Reel published! (confirmed via: {sel})")
                break
        else:
            print("🎉 Process completed — check screenshots for confirmation.")

        await browser.close()


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Read caption from file
    caption_path = Path(CAPTIONS_TXT)
    if caption_path.exists():
        caption = caption_path.read_text(encoding="utf-8").strip()
        print(f"📝 Caption loaded from {CAPTIONS_TXT}")
    else:
        caption = "Check out my latest reel! #reels"
        print(f"⚠️  {CAPTIONS_TXT} not found — using default caption")

    asyncio.run(upload_reel(caption=caption, video_path=VIDEO_FILE))
