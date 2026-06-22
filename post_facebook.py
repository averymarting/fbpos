import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_TXT  = "facebook_cookies.txt"
CAPTIONS_TXT = "captions.txt"
VIDEO_FILE   = "testing.mp4"
SCREENSHOTS_DIR = Path("screenshots")


def load_netscape_cookies(txt_file):
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
                print(f"⚠️  Skipping line {line_num}")
                continue
            domain, _, path, secure_s, expires, name, value = parts[:7]
            cookie = {
                "name": name, "value": value,
                "domain": domain if domain.startswith('.') else f".{domain}",
                "path": path, "secure": secure_s.upper() == 'TRUE',
                "httpOnly": False,
                "sameSite": "None" if secure_s.upper() == 'TRUE' else "Lax",
            }
            if expires.lstrip('-').isdigit() and int(expires) > 0:
                cookie["expires"] = int(expires)
            cookies.append(cookie)
    print(f"✅ Loaded {len(cookies)} cookies")
    return cookies


async def ss(page, name):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    for p in [SCREENSHOTS_DIR / f"{name}.png", Path(f"{name}.png")]:
        try:
            await page.screenshot(path=str(p), full_page=False)
            print(f"📸 {p}")
        except Exception as e:
            print(f"⚠️  {p}: {e}")


async def dump(page, name):
    try:
        Path(name).write_text(await page.content(), encoding="utf-8")
        print(f"📄 {name}")
    except Exception as e:
        print(f"⚠️  dump {name}: {e}")


async def force_tap(page, loc):
    box = await loc.bounding_box()
    if box:
        try:
            await page.touchscreen.tap(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
            print("   ✅ tap")
            return True
        except Exception as e:
            print(f"   — tap: {e}")
    try:
        await loc.click(force=True, timeout=5_000)
        print("   ✅ force-click")
        return True
    except Exception as e:
        print(f"   — force-click: {e}")
    try:
        await loc.evaluate("el => el.click()")
        print("   ✅ js-click")
        return True
    except Exception as e:
        print(f"   — js-click: {e}")
    return False


# ── Caption field selectors confirmed from HTML dumps ─────────────────────────
# From 08_final_page.html: aria-placeholder="Describe your reel..."
# and data-lexical-editor="true" on the same div
CAPTION_SELECTORS = [
    '[aria-placeholder="Describe your reel..."]',          # ✅ confirmed
    'div[data-lexical-editor="true"]',                     # ✅ confirmed
    'div[role="textbox"][contenteditable="true"]',         # generic fallback
    'textarea[placeholder*="escribe" i]',                  # textarea variant
    'textarea[placeholder*="caption" i]',
    'div[contenteditable="true"]',                         # last resort
]

# ── Next / Share button selectors confirmed from HTML dumps ──────────────────
# From 04/08 HTML: aria-label="Next" and aria-label="Share" on role="button"
NEXT_SELECTORS = [
    '[aria-label="Next"][role="button"]',                  # ✅ confirmed
    'div[role="button"]:has-text("Next")',
    'span:has-text("Next")',
]

SHARE_SELECTORS = [
    '[aria-label="Share"][role="button"]',                 # ✅ confirmed
    'div[role="button"]:has-text("Share")',
    'div[role="button"]:has-text("Publish")',
    'span:has-text("Share")',
    'span:has-text("Publish")',
]


async def find_visible(page, selectors, label="element"):
    """Return the first visible locator from the selector list, or None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                print(f"   🎯 {label} found: {sel}")
                return loc
        except Exception:
            pass
    return None


async def type_caption(page, caption):
    """Find the caption field (whichever selector matches) and type into it."""
    loc = await find_visible(page, CAPTION_SELECTORS, "caption field")
    if not loc:
        print("   ❌ No caption field found")
        return False
    try:
        await loc.click()
        await asyncio.sleep(0.4)
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.2)
        await page.keyboard.type(caption, delay=40)
        print(f"   ✅ Caption typed ({len(caption)} chars)")
        return True
    except Exception as e:
        print(f"   ❌ Typing failed: {e}")
        return False


async def click_btn(page, selectors, label):
    """Find and click first non-disabled visible button from selector list."""
    loc = await find_visible(page, selectors, label)
    if not loc:
        print(f"   ❌ {label} button not found")
        return False
    try:
        disabled = await loc.get_attribute("aria-disabled")
        if disabled == "true":
            print(f"   ⚠️  {label} is disabled")
            return False
    except Exception:
        pass
    return await force_tap(page, loc)


# ─────────────────────────────────────────────────────────────────────────────

async def upload_reel(caption, video_path):
    if not Path(video_path).exists():
        print(f"❌ Video not found: {video_path}"); return
    print(f"🎬 {video_path} ({Path(video_path).stat().st_size // 1024} KB)")
    print(f"📝 Caption: {caption[:80]}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars", "--disable-dev-shm-usage",
        ])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US", timezone_id="Asia/Karachi", accept_downloads=True,
        )

        if not Path(COOKIES_TXT).exists():
            print(f"❌ {COOKIES_TXT} missing"); await browser.close(); return
        cookies = load_netscape_cookies(COOKIES_TXT)
        if not cookies:
            print("❌ No cookies"); await browser.close(); return
        await context.add_cookies(cookies)
        page = await context.new_page()

        # ── 1. Load Facebook ──────────────────────────────────────────────────
        print("\n🌐 Opening Facebook…")
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(8)
        await ss(page, "01_home")

        if "login" in page.url or "checkpoint" in page.url:
            print("❌ Not logged in"); await ss(page, "01_login_fail"); await browser.close(); return

        login_ok = any([
            await page.locator(s).count() > 0
            for s in ['[aria-label="Home"]', 'div[role="feed"]', 'span:has-text("What\'s on your mind?")']
        ])
        if not login_ok:
            print("❌ Login check failed"); await dump(page, "01_login_fail.html"); await browser.close(); return
        print("   ✅ Logged in")

        # ── 2. Open Reels create ──────────────────────────────────────────────
        print("\n🎬 Opening /reels/create/…")
        await page.goto("https://www.facebook.com/reels/create/", wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(8)
        await ss(page, "02_reels_page")
        print(f"   URL: {page.url}")

        # ── 3. Attach video ───────────────────────────────────────────────────
        print("\n📁 Attaching video…")
        uploaded = False

        # Try direct hidden file input first
        for sel in ['input[type="file"][accept*="video"]', 'input[type="file"]']:
            try:
                inp = page.locator(sel).first
                if await inp.count() > 0:
                    await inp.set_input_files(video_path)
                    print(f"   ✅ Attached via: {sel}")
                    uploaded = True
                    break
            except Exception as e:
                print(f"   — {sel}: {e}")

        # Fallback: click upload button → intercept file chooser
        if not uploaded:
            for sel in [
                'div[role="button"]:has-text("Select video")',
                '[aria-label="Select video"]',
                'span:has-text("Select video from computer")',
                'div[role="button"]:has-text("Upload")',
            ]:
                el = page.locator(sel).first
                if await el.count() > 0:
                    try:
                        async with page.expect_file_chooser(timeout=15_000) as fc_info:
                            await el.click(force=True)
                        await (await fc_info.value).set_files(video_path)
                        print(f"   ✅ Attached via chooser: {sel}")
                        uploaded = True
                        break
                    except Exception as e:
                        print(f"   — chooser {sel}: {e}")

        if not uploaded:
            print("❌ Could not attach video")
            await dump(page, "03_upload_fail.html"); await ss(page, "03_upload_fail")
            await browser.close(); return

        await ss(page, "03_after_upload")

        # ── 4. Wait for video to process ──────────────────────────────────────
        print("\n⏳ Waiting 30 s for video to process…")
        await asyncio.sleep(30)
        await ss(page, "04_processed")

        # ── 5. DYNAMIC STEP LOOP ──────────────────────────────────────────────
        # Detected flow from screenshots:
        #   Screen 1 "Create reel"  → no caption field → click Next
        #   Screen 2 "Edit reel"    → caption field appears → type → click Next
        #   Screen 3               → Share button → click Share
        #
        # The loop checks EVERY screen for:
        #   (a) caption field  → type if not done yet
        #   (b) Share button   → click and finish
        #   (c) Next button    → advance to next screen
        # This handles any number of steps FB may add.

        caption_done = False
        print("\n🔄 Dynamic step loop…")

        for step in range(1, 8):
            await ss(page, f"step{step:02d}")
            print(f"\n── Step {step} ──────────────────────────────────────")

            # (a) Caption field — type if visible and not done yet
            if not caption_done:
                cap_loc = await find_visible(page, CAPTION_SELECTORS, "caption")
                if cap_loc:
                    try:
                        await cap_loc.click()
                        await asyncio.sleep(0.4)
                        await page.keyboard.press("Control+a")
                        await asyncio.sleep(0.2)
                        await page.keyboard.type(caption, delay=40)
                        print(f"   ✅ Caption typed")
                        caption_done = True
                        await asyncio.sleep(1)
                        await ss(page, f"step{step:02d}_caption_typed")
                    except Exception as e:
                        print(f"   ⚠️  Caption type error: {e}")

            # (b) Share/Publish button — final step
            share_loc = await find_visible(page, SHARE_SELECTORS, "Share/Publish")
            if share_loc:
                try:
                    disabled = await share_loc.get_attribute("aria-disabled")
                    if disabled == "true":
                        print("   ⚠️  Share disabled — waiting 3 s…")
                        await asyncio.sleep(3)
                        share_loc = await find_visible(page, SHARE_SELECTORS, "Share/Publish retry")
                except Exception:
                    pass
                if share_loc:
                    ok = await force_tap(page, share_loc)
                    if ok:
                        print("   ✅ Share clicked — done!")
                        await asyncio.sleep(20)
                        await ss(page, "final_result")
                        await dump(page, "final_page.html")
                        break
                    else:
                        print("   ❌ Share click failed")
                        await dump(page, f"step{step:02d}_share_fail.html")
                        break

            # (c) Next button — advance
            next_loc = await find_visible(page, NEXT_SELECTORS, "Next")
            if next_loc:
                try:
                    disabled = await next_loc.get_attribute("aria-disabled")
                    if disabled == "true":
                        print("   ⚠️  Next disabled — waiting 3 s…")
                        await asyncio.sleep(3)
                        next_loc = await find_visible(page, NEXT_SELECTORS, "Next retry")
                except Exception:
                    pass
                if next_loc:
                    ok = await force_tap(page, next_loc)
                    if ok:
                        print("   ✅ Next clicked — waiting 5 s…")
                        await asyncio.sleep(5)
                        continue

            # Nothing found — dump and stop
            print("   ❌ No Next or Share found — stuck")
            await dump(page, f"step{step:02d}_stuck.html")
            await ss(page, f"step{step:02d}_stuck")
            break

        else:
            print("⚠️  Loop exhausted without finishing")

        # ── 6. Confirm ────────────────────────────────────────────────────────
        for sel in [
            'span:has-text("Your reel is now shared")',
            'span:has-text("Your reel was shared")',
            'span:has-text("Reel posted")',
            'span:has-text("Published")',
        ]:
            if await page.locator(sel).count() > 0:
                print(f"🎉 Confirmed published: {sel}"); break
        else:
            print("🎉 Process complete — check final_result.png")

        await browser.close()


if __name__ == "__main__":
    cap_path = Path(CAPTIONS_TXT)
    caption = cap_path.read_text(encoding="utf-8").strip() if cap_path.exists() \
              else "Check out my latest reel! #reels"
    print(f"📝 Caption: {'loaded from ' + CAPTIONS_TXT if cap_path.exists() else 'default'}")
    asyncio.run(upload_reel(caption=caption, video_path=VIDEO_FILE))
