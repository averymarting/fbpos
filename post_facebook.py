import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_TXT = "facebook_cookies.txt"


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

            parts = line.split('\t')  # Netscape format uses TABS, not spaces
            if len(parts) < 7:
                print(f"⚠️  Skipping malformed cookie line {line_num}: {line[:80]}")
                continue

            domain    = parts[0]
            # parts[1] = flag (TRUE/FALSE) — whether all machines in domain can access
            path      = parts[2]
            secure    = parts[3].upper() == 'TRUE'
            expires   = parts[4]
            name      = parts[5]
            value     = parts[6]

            cookie = {
                "name":     name,
                "value":    value,
                "domain":   domain if domain.startswith('.') else f".{domain}",
                "path":     path,
                "secure":   secure,
                "httpOnly": False,  # Netscape format doesn't encode httpOnly
                "sameSite": "None" if secure else "Lax",
            }
            if expires.lstrip('-').isdigit() and int(expires) > 0:
                cookie["expires"] = int(expires)

            cookies.append(cookie)

    print(f"✅ Loaded {len(cookies)} cookies from {txt_file}")
    return cookies


async def save_screenshot(page, name: str):
    """Helper to always save a screenshot and log it."""
    path = f"{name}.png"
    try:
        await page.screenshot(path=path, full_page=False)
        print(f"📸 Screenshot saved: {path}")
    except Exception as e:
        print(f"⚠️  Could not save screenshot {path}: {e}")


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

        # Redirect to login page means cookies are expired/invalid
        if "login" in current_url or "checkpoint" in current_url:
            print("❌ Redirected to login — cookies are expired or invalid!")
            await save_screenshot(page, "02_login_redirect")
            await browser.close()
            return

        # Look for any of the home-feed indicators
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
            print("❌ Could not confirm login. Page HTML snippet:")
            try:
                snippet = await page.inner_text("body")
                print(snippet[:500])
            except Exception:
                pass
            await save_screenshot(page, "02_login_failed")
            await browser.close()
            return

        await save_screenshot(page, "02_logged_in")

        # ── STEP 3: Click the post composer ───────────────────────────────────
        print("🔄 Opening post composer…")

        composer_selectors = [
            'div[role="button"]:has-text("What\'s on your mind?")',
            '[aria-label="Create a post"]',
            'span:has-text("What\'s on your mind?")',
            '[data-testid="status-attachment-mentions-input"]',
            'form[method="POST"] div[role="button"]',
        ]

        post_opened = False
        for sel in composer_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(1)
                    await el.click()
                    print(f"   ✅ Opened composer via: {sel}")
                    post_opened = True
                    break
            except Exception as exc:
                print(f"   — Selector failed ({sel}): {exc}")
                continue

        if not post_opened:
            print("❌ Could not open post composer.")
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
            'div[aria-label="What\'s on your mind?"]',
            'div[data-lexical-editor="true"]',
        ]

        typed = False
        for sel in text_selectors:
            try:
                editor = page.locator(sel).first
                if await editor.count() > 0:
                    await editor.wait_for(state="visible", timeout=10_000)
                    await editor.click()
                    await asyncio.sleep(1)
                    # Use keyboard.type for contenteditable — fill() often fails
                    await page.keyboard.type(message, delay=50)
                    print(f"   ✅ Typed message via: {sel}")
                    typed = True
                    break
            except Exception as exc:
                print(f"   — Typing selector failed ({sel}): {exc}")
                continue

        if not typed:
            print("❌ Could not type message.")
            await save_screenshot(page, "04_type_failed")
            await browser.close()
            return

        await asyncio.sleep(3)
        await save_screenshot(page, "04_message_typed")

        # ── STEP 5: Click Post button ──────────────────────────────────────────
        print("📤 Clicking Post button…")

        # Wait a moment so the Post button becomes active (it's disabled until text exists)
        await asyncio.sleep(2)

        post_btn_selectors = [
            'div[aria-label="Post"][role="button"]',
            'div[role="button"]:has-text("Post"):not([aria-disabled="true"])',
            'span:has-text("Post")',
        ]

        posted = False
        for sel in post_btn_selectors:
            try:
                btn = page.locator(sel).last
                if await btn.count() > 0:
                    await btn.wait_for(state="visible", timeout=10_000)
                    # Confirm it's not disabled
                    disabled = await btn.get_attribute("aria-disabled")
                    if disabled == "true":
                        print(f"   ⚠️  Button is disabled: {sel}")
                        continue
                    await btn.click()
                    print(f"   ✅ Clicked Post via: {sel}")
                    posted = True
                    break
            except Exception as exc:
                print(f"   — Post button selector failed ({sel}): {exc}")
                continue

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

        # Quick check — did composer close? (good sign)
        composer_still_open = False
        for sel in text_selectors:
            if await page.locator(sel).count() > 0:
                composer_still_open = True
                break

        if composer_still_open:
            print("⚠️  Composer may still be open — post might not have been published.")
        else:
            print("🎉 Post published successfully!")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(post_on_facebook("Hello testing"))
