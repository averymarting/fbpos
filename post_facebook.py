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


async def click_element(page, locator, label="button"):
    """
    Desktop context — NO touchscreen.
    Strategy 1: normal click (respects actionability)
    Strategy 2: force click (bypasses overlap checks)
    Strategy 3: JS click (last resort)
    """
    # Strategy 1 — normal click with short timeout
    try:
        await locator.click(timeout=5_000)
        print(f"   ✅ click: {label}")
        return True
    except Exception as e:
        print(f"   — normal click failed ({label}): {e}")

    # Strategy 2 — force click (skip actionability)
    try:
        await locator.click(force=True, timeout=5_000)
        print(f"   ✅ force-click: {label}")
        return True
    except Exception as e:
        print(f"   — force-click failed ({label}): {e}")

    # Strategy 3 — JS click
    try:
        await locator.evaluate("el => el.click()")
        print(f"   ✅ js-click: {label}")
        return True
    except Exception as e:
        print(f"   — js-click failed ({label}): {e}")

    return False


async def find_visible(page, selectors, label="element"):
    """Return first visible locator from list, or None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                print(f"   🎯 {label}: {sel}")
                return loc, sel
        except Exception:
            pass
    return None, None


# ── Confirmed selectors from HTML dumps ──────────────────────────────────────
#
# From 04_page_state.html (Create reel screen):
#   Next:  div[aria-label="Next"][role="button"][tabindex="0"]  ← inside form[method="POST"]
#   Input: input[accept*="video"][type="file"]
#
# From 08_final_page.html (Edit reel screen):
#   Caption: div[aria-placeholder="Describe your reel..."][data-lexical-editor="true"]
#   Share:   div[aria-label="Share"][role="button"]
#
# Scoped inside the modal: [aria-modal="true"] form ... to avoid hitting feed buttons

NEXT_SELECTORS = [
    # Scoped to modal form — most precise
    '[aria-modal="true"] form [aria-label="Next"][role="button"]',
    '[aria-modal="true"] [aria-label="Next"][role="button"]',
    # Fallback unscoped
    '[aria-label="Next"][role="button"]',
    'div[role="button"]:has-text("Next")',
]

CAPTION_SELECTORS = [
    # Confirmed from 08_final_page.html
    '[aria-placeholder="Describe your reel..."]',
    'div[data-lexical-editor="true"]',
    'div[role="textbox"][contenteditable="true"]',
    'div[contenteditable="true"][aria-placeholder]',
    'textarea[placeholder*="escribe" i]',
    'textarea[placeholder*="caption" i]',
    'div[contenteditable="true"]',
]

SHARE_SELECTORS = [
    # Confirmed from 08_final_page.html
    '[aria-modal="true"] [aria-label="Share"][role="button"]',
    '[aria-label="Share"][role="button"]',
    '[aria-label="Publish"][role="button"]',
    '[aria-modal="true"] [aria-label="Post"][role="button"]',
    '[aria-label="Post"][role="button"]',
    'div[role="button"]:has-text("Share")',
    'div[role="button"]:has-text("Publish")',
    'div[role="button"]:has-text("Post")',
]


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
        # DESKTOP context — no is_mobile, no has_touch, no touchscreen
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="Asia/Karachi",
            accept_downloads=True,
        )

        if not Path(COOKIES_TXT).exists():
            print(f"❌ {COOKIES_TXT} missing"); await browser.close(); return
        cookies = load_netscape_cookies(COOKIES_TXT)
        if not cookies:
            print("❌ No cookies"); await browser.close(); return
        await context.add_cookies(cookies)
        page = await context.new_page()

        # ── 1. Login check ────────────────────────────────────────────────────
        print("\n🌐 Opening Facebook…")
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(7)
        await ss(page, "01_home")

        if "login" in page.url or "checkpoint" in page.url:
            print("❌ Not logged in"); await browser.close(); return

        login_ok = False
        for s in ['[aria-label="Home"]', 'div[role="feed"]', 'span:has-text("What\'s on your mind?")']:
            if await page.locator(s).count() > 0:
                login_ok = True; print(f"   ✅ Logged in ({s})"); break
        if not login_ok:
            print("❌ Login failed"); await dump(page, "01_fail.html"); await browser.close(); return

        # ── 2. Open Reels create ──────────────────────────────────────────────
        print("\n🎬 Opening /reels/create/…")
        await page.goto("https://www.facebook.com/reels/create/", wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(7)
        await ss(page, "02_reels_page")
        print(f"   URL: {page.url}")

        # ── 3. Attach video ───────────────────────────────────────────────────
        print("\n📁 Attaching video…")
        uploaded = False

        # Confirmed from HTML: input[accept*="video"][type="file"] inside the modal
        for sel in [
            '[aria-modal="true"] input[type="file"][accept*="video"]',
            'input[type="file"][accept*="video"]',
            'input[type="file"]',
        ]:
            try:
                inp = page.locator(sel).first
                if await inp.count() > 0:
                    await inp.set_input_files(video_path)
                    print(f"   ✅ Attached: {sel}")
                    uploaded = True
                    break
            except Exception as e:
                print(f"   — {sel}: {e}")

        if not uploaded:
            # Fallback: click upload button → file chooser
            for sel in [
                'div[role="button"]:has-text("Select video")',
                '[aria-label="Select video"]',
                'span:has-text("Select video from computer")',
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
            await dump(page, "03_fail.html"); await ss(page, "03_fail")
            await browser.close(); return

        await ss(page, "03_uploaded")

        # ── 4. Wait for video to process ──────────────────────────────────────
        print("\n⏳ Waiting 30 s for video processing…")
        await asyncio.sleep(30)
        await ss(page, "04_processed")

        # ── 5. STEP LOOP ──────────────────────────────────────────────────────
        # Confirmed 3-step flow:
        #   Screen 1 "Create reel": video preview  → no caption → click Next
        #   Screen 2 "Edit reel":   caption field   → type       → click Next
        #   Screen 3:               Share button    → click Share/Post/Publish
        #
        # Loop detects what's on screen each iteration and acts accordingly.

        caption_done = False
        print("\n🔄 Step loop starting…")

        for step in range(1, 8):
            print(f"\n── Step {step} ─────────────────────────────────────")
            await ss(page, f"step{step:02d}_start")

            # (a) Type caption if field is visible and not yet done
            if not caption_done:
                cap_loc, cap_sel = await find_visible(page, CAPTION_SELECTORS, "caption field")
                if cap_loc:
                    try:
                        await cap_loc.click()
                        await asyncio.sleep(0.3)
                        await page.keyboard.press("Control+a")
                        await asyncio.sleep(0.2)
                        await page.keyboard.type(caption, delay=40)
                        caption_done = True
                        print(f"   ✅ Caption typed via: {cap_sel}")
                        await asyncio.sleep(1)
                        await ss(page, f"step{step:02d}_captioned")
                    except Exception as e:
                        print(f"   ⚠️  Caption error: {e}")

            # (b) Share/Post/Publish = final step → click and exit
            share_loc, share_sel = await find_visible(page, SHARE_SELECTORS, "Share/Post/Publish")
            if share_loc:
                # Wait if disabled
                for _ in range(3):
                    try:
                        disabled = await share_loc.get_attribute("aria-disabled")
                        if disabled != "true":
                            break
                        print("   ⏳ Share disabled, waiting 3 s…")
                        await asyncio.sleep(3)
                        share_loc, share_sel = await find_visible(page, SHARE_SELECTORS, "Share retry")
                    except Exception:
                        break

                if share_loc:
                    ok = await click_element(page, share_loc, f"Share ({share_sel})")
                    if ok:
                        print("   🎉 Share/Post clicked — waiting for publish…")
                        await asyncio.sleep(20)
                        await ss(page, "final_result")
                        await dump(page, "final_page.html")
                        break
                    else:
                        print("   ❌ Share click failed")
                        await dump(page, f"step{step:02d}_sharefail.html")
                        break

            # (c) Next button → advance to next screen
            next_loc, next_sel = await find_visible(page, NEXT_SELECTORS, "Next")
            if next_loc:
                # Wait if disabled (video still uploading)
                for wait in [0, 3, 5, 10]:
                    if wait:
                        print(f"   ⏳ Next disabled, waiting {wait} s…")
                        await asyncio.sleep(wait)
                        next_loc, next_sel = await find_visible(page, NEXT_SELECTORS, "Next retry")
                        if not next_loc:
                            break
                    try:
                        disabled = await next_loc.get_attribute("aria-disabled")
                        if disabled != "true":
                            break
                    except Exception:
                        break

                if next_loc:
                    ok = await click_element(page, next_loc, f"Next ({next_sel})")
                    if ok:
                        print("   ✅ Next clicked — waiting 6 s…")
                        await asyncio.sleep(6)
                        continue

            # Nothing actionable found
            print("   ❌ No Next or Share/Post found — stuck")
            await dump(page, f"step{step:02d}_stuck.html")
            await ss(page, f"step{step:02d}_stuck")
            break

        else:
            print("⚠️  Loop exhausted")

        # ── 6. Confirm ────────────────────────────────────────────────────────
        for sel in [
            'span:has-text("Your reel is now shared")',
            'span:has-text("Your reel was shared")',
            'span:has-text("Reel posted")',
            'span:has-text("Published")',
        ]:
            if await page.locator(sel).count() > 0:
                print(f"🎉 Confirmed: {sel}"); break
        else:
            print("🎉 Done — check final_result.png")

        await browser.close()


if __name__ == "__main__":
    cap_path = Path(CAPTIONS_TXT)
    caption = cap_path.read_text(encoding="utf-8").strip() if cap_path.exists() \
              else "Check out my latest reel! #reels"
    print(f"📝 {'Loaded from ' + CAPTIONS_TXT if cap_path.exists() else 'Default caption'}")
    asyncio.run(upload_reel(caption=caption, video_path=VIDEO_FILE))
