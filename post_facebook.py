import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_TXT = "facebook_cookies.txt"
SCREENSHOTS_DIR = Path("screenshots")


def load_netscape_cookies(txt_file: str):
    """
    Parse Netscape/Mozilla cookie format.
    Columns: domain | flag | path | secure | expiration | name | value
    """
    cookies = []
    with open(txt_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            parts = line.split('\t')
            if len(parts) < 7:
                # Try space-split as fallback
                parts = line.split()
            if len(parts) < 7:
                print(f"⚠️  Skipping malformed cookie line {line_num}: {line[:80]}")
                continue

            domain  = parts[0]
            path    = parts[2]
            secure  = parts[3].upper() == 'TRUE'
            expires = parts[4]
            name    = parts[5]
            value   = parts[6]

            cookie = {
                "name":     name,
                "value":    value,
                "domain":   domain if domain.startswith('.') else f".{domain}",
                "path":     path,
                "secure":   secure,
                "httpOnly": False,
                "sameSite": "None" if secure else "Lax",
            }
            if expires.lstrip('-').isdigit() and int(expires) > 0:
                cookie["expires"] = int(expires)

            cookies.append(cookie)

    print(f"✅ Loaded {len(cookies)} cookies from {txt_file}")
    return cookies


async def save_screenshot(page, name: str):
    """Save screenshot to screenshots/ subfolder AND root (belt+suspenders for CI)."""
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    paths = [
        SCREENSHOTS_DIR / f"{name}.png",   # subfolder (artifact glob)
        Path(f"{name}.png"),               # root fallback
    ]
    for p in paths:
        try:
            await page.screenshot(path=str(p), full_page=False)
            print(f"📸 Screenshot saved: {p}")
        except Exception as e:
            print(f"⚠️  Could not save screenshot {p}: {e}")


async def force_tap(page, locator):
    """
    Facebook mobile lite wraps elements in an overlay div that intercepts
    pointer events — standard click() times out.  We use three strategies:
    1. tap()        — touch event, bypasses most overlays on mobile contexts
    2. click(force) — skip actionability checks entirely
    3. JS click     — last resort, directly calls .click() in the DOM
    """
    box = await locator.bounding_box()
    if box:
        # Strategy 1: native touch tap at element centre
        try:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            await page.touchscreen.tap(cx, cy)
            print(f"   ✅ Tapped at ({cx:.0f}, {cy:.0f})")
            return True
        except Exception as e:
            print(f"   — tap() failed: {e}")

    # Strategy 2: force click (skips visibility/intercept checks)
    try:
        await locator.click(force=True, timeout=5_000)
        print("   ✅ force click succeeded")
        return True
    except Exception as e:
        print(f"   — force click failed: {e}")

    # Strategy 3: JavaScript click
    try:
        await locator.evaluate("el => el.click()")
        print("   ✅ JS .click() succeeded")
        return True
    except Exception as e:
        print(f"   — JS click failed: {e}")

    return False


async def post_on_facebook(message: str = "Hello testing"):
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
        )

        # ── Load cookies ──────────────────────────────────────────────────────
        if not Path(COOKIES_TXT).exists():
            print(f"❌ Cookies file '{COOKIES_TXT}' not found!")
            await browser.close()
            return

        cookies = load_netscape_cookies(COOKIES_TXT)
        if not cookies:
            print("❌ No cookies were loaded — check the file format.")
            await browser.close()
            return

        await context.add_cookies(cookies)
        page = await context.new_page()

        # ── STEP 1: Open Facebook ─────────────────────────────────────────────
        print("🌐 Opening Facebook…")
        try:
            await page.goto(
                "https://www.facebook.com/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception as e:
            print(f"❌ Failed to load Facebook: {e}")
            await save_screenshot(page, "00_load_failed")
            await browser.close()
            return

        await asyncio.sleep(8)
        await save_screenshot(page, "01_after_load")

        # ── STEP 2: Verify login ───────────────────────────────────────────────
        print("🔍 Checking login status…")
        current_url = page.url
        print(f"   Current URL: {current_url}")

        if "login" in current_url or "checkpoint" in current_url:
            print("❌ Redirected to login — cookies expired or invalid!")
            await save_screenshot(page, "02_login_redirect")
            await browser.close()
            return

        login_indicators = [
            '[aria-label="Home"]',
            '[data-pagelet="FeedUnit_0"]',
            'div[role="feed"]',
            '[aria-label="Create a post"]',
            'span:has-text("What\'s on your mind?")',
        ]
        logged_in = False
        for indicator in login_indicators:
            try:
                if await page.locator(indicator).count() > 0:
                    logged_in = True
                    print(f"   ✅ Login confirmed via: {indicator}")
                    break
            except Exception:
                continue

        if not logged_in:
            print("❌ Could not confirm login.")
            try:
                print(await page.inner_text("body"))
            except Exception:
                pass
            await save_screenshot(page, "02_login_failed")
            await browser.close()
            return

        await save_screenshot(page, "02_logged_in")

        # ── STEP 3: Open post composer ────────────────────────────────────────
        # Facebook mobile lite: the "What's on your mind?" button is INSIDE an
        # overlay container (data-mcomponent="MContainer") that intercepts all
        # pointer events.  We must use touch/force strategies via force_tap().
        print("🔄 Opening post composer…")

        composer_selectors = [
            # The actual button node (aria-label set on it)
            'div[aria-label="What\'s on your mind?"]',
            # The span text child — tap propagates up to the button
            'span:has-text("What\'s on your mind?")',
            '[aria-label="Create a post"]',
            # data-action-id is stable on FB mobile lite
            'div[data-action-id]',
        ]

        post_opened = False
        for sel in composer_selectors:
            try:
                el = page.locator(sel).first
                count = await el.count()
                if count == 0:
                    print(f"   — Not found: {sel}")
                    continue
                print(f"   🎯 Found element via: {sel} — attempting force_tap…")
                ok = await force_tap(page, el)
                if ok:
                    print(f"   ✅ Opened composer via: {sel}")
                    post_opened = True
                    break
                else:
                    print(f"   — force_tap failed for: {sel}")
            except Exception as exc:
                print(f"   — Exception for ({sel}): {exc}")
                continue

        await save_screenshot(page, "03_after_composer_attempt")

        if not post_opened:
            print("❌ Could not open post composer — dumping page HTML for debug…")
            try:
                html = await page.content()
                Path("page_debug.html").write_text(html, encoding="utf-8")
                print("   📄 page_debug.html saved")
            except Exception:
                pass
            await save_screenshot(page, "03_composer_open_failed")
            await browser.close()
            return

        await asyncio.sleep(6)
        await save_screenshot(page, "03_composer_opened")

        # ── STEP 4: Type the message ───────────────────────────────────────────
        print("⌨️  Typing message…")

        text_selectors = [
            'div[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"]',
            'div[aria-label="What\'s on your mind?"][contenteditable="true"]',
            'div[data-lexical-editor="true"]',
            # FB mobile lite uses a plain textarea sometimes
            'textarea[name="xc_message"]',
            'textarea',
        ]

        typed = False
        for sel in text_selectors:
            try:
                editor = page.locator(sel).first
                if await editor.count() == 0:
                    continue
                await editor.wait_for(state="visible", timeout=10_000)
                # tap to focus (mobile context)
                box = await editor.bounding_box()
                if box:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    await page.touchscreen.tap(cx, cy)
                else:
                    await editor.click(force=True)
                await asyncio.sleep(1)
                # keyboard.type simulates real keystrokes on contenteditable
                await page.keyboard.type(message, delay=60)
                print(f"   ✅ Typed message via: {sel}")
                typed = True
                break
            except Exception as exc:
                print(f"   — Typing selector failed ({sel}): {exc}")
                continue

        await save_screenshot(page, "04_after_type_attempt")

        if not typed:
            print("❌ Could not type message.")
            await save_screenshot(page, "04_type_failed")
            await browser.close()
            return

        await asyncio.sleep(3)
        await save_screenshot(page, "04_message_typed")

        # ── STEP 5: Click Post button ──────────────────────────────────────────
        print("📤 Clicking Post button…")
        await asyncio.sleep(2)

        post_btn_selectors = [
            'div[aria-label="Post"][role="button"]',
            'button[name="share"]',
            'input[type="submit"][value="Post"]',
            'div[role="button"]:has-text("Post")',
            'span:has-text("Post")',
        ]

        posted = False
        for sel in post_btn_selectors:
            try:
                btn = page.locator(sel).last
                if await btn.count() == 0:
                    continue
                await btn.wait_for(state="visible", timeout=8_000)
                disabled = await btn.get_attribute("aria-disabled")
                if disabled == "true":
                    print(f"   ⚠️  Button disabled: {sel}")
                    continue
                ok = await force_tap(page, btn)
                if ok:
                    print(f"   ✅ Clicked Post via: {sel}")
                    posted = True
                    break
            except Exception as exc:
                print(f"   — Post button failed ({sel}): {exc}")
                continue

        await save_screenshot(page, "05_after_post_attempt")

        if not posted:
            print("❌ Could not click Post button.")
            await save_screenshot(page, "05_post_btn_failed")
            await browser.close()
            return

        # ── STEP 6: Wait & confirm ─────────────────────────────────────────────
        print("⏳ Waiting for post to publish (15 s)…")
        await asyncio.sleep(15)
        await save_screenshot(page, "05_after_post_click")

        print("⏳ Extra 10 s safety wait…")
        await asyncio.sleep(10)
        await save_screenshot(page, "06_final_result")

        composer_still_open = any([
            await page.locator(s).count() > 0 for s in text_selectors
        ])
        if composer_still_open:
            print("⚠️  Composer may still be open — post might not have published.")
        else:
            print("🎉 Post published successfully!")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(post_on_facebook("Hello testing"))
