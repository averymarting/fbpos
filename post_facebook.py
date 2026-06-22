import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_TXT = "facebook_cookies.txt"

def load_netscape_cookies(txt_file: str):
    cookies = []
    with open(txt_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) < 7:
                continue
            domain = parts[0]
            path = parts[2]
            secure = parts[3].upper() == 'TRUE'
            expires = int(parts[4]) if parts[4].isdigit() else None
            name = parts[5]
            value = parts[6]

            cookie = {
                "name": name,
                "value": value,
                "domain": domain if domain.startswith('.') else f".{domain}",
                "path": path,
                "secure": secure,
                "httpOnly": True,
                "sameSite": "None" if secure else "Lax"
            }
            if expires:
                cookie["expires"] = expires
            cookies.append(cookie)
    print(f"✅ Loaded {len(cookies)} cookies")
    return cookies


async def post_on_facebook(message: str = "Hello testing"):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
            viewport={"width": 412, "height": 915},
            device_scale_factor=2.75,
            is_mobile=True,
            has_touch=True,
            locale="en-US",
            timezone_id="Asia/Karachi",
        )

        if Path(COOKIES_TXT).exists():
            cookies = load_netscape_cookies(COOKIES_TXT)
            await context.add_cookies(cookies)
        else:
            print("❌ Cookies file not found!")
            await browser.close()
            return

        page = await context.new_page()

        # === 1. Open Facebook & Check Login ===
        print("🌐 Opening Facebook...")
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(8)
        await page.screenshot(path="01_login_check.png")

        # Check if logged in
        logged_in = False
        try:
            if await page.locator("text=What's on your mind?").count() > 0 or \
               await page.locator('[aria-label="Create a post"]').count() > 0:
                logged_in = True
        except:
            pass

        if logged_in:
            print("✅ Successfully logged in!")
        else:
            print("❌ Login failed or cookies expired!")
            await page.screenshot(path="01_login_failed.png")
            await browser.close()
            return

        # === 2. Try to open Post Composer ===
        print("🔄 Opening post composer...")
        await page.screenshot(path="02_before_post_click.png")

        post_opened = False
        selectors = [
            'div[role="button"]:has-text("What\'s on your mind?")',
            'span:has-text("What\'s on your mind?")',
            '[aria-label="Create a post"]',
            'div.x1i10hfl.x1q0g3np',           # Common class
            '[role="button"][tabindex="0"]'    # Fallback
        ]

        for selector in selectors:
            try:
                box = await page.wait_for_selector(selector, timeout=10000)
                if box:
                    await box.click()
                    print(f"✅ Clicked using selector: {selector}")
                    post_opened = True
                    break
            except:
                continue

        if not post_opened:
            print("⚠️ Could not open post composer. Taking screenshot...")
            await page.screenshot(path="02_post_open_failed.png")
            await browser.close()
            return

        await asyncio.sleep(6)
        await page.screenshot(path="03_composer_opened.png")

        # === 3. Type Message ===
        try:
            print("⌨️ Typing message...")
            editor = page.locator('div[role="textbox"], div[contenteditable="true"]').first
            await editor.wait_for(timeout=10000)
            await editor.click()
            await editor.fill(message)
            print(f"✅ Typed message: {message}")
            await asyncio.sleep(4)
            await page.screenshot(path="04_message_typed.png")
        except Exception as e:
            print(f"⚠️ Failed to type message: {e}")
            await page.screenshot(path="04_type_failed.png")

        # === 4. Click Post ===
        try:
            print("📤 Clicking Post button...")
            await asyncio.sleep(3)
            post_btn = page.locator('div[role="button"]:has-text("Post")').last
            await post_btn.wait_for(timeout=10000)
            await post_btn.click()
            print("✅ Clicked Post button")
            await asyncio.sleep(15)   # Long wait as requested
            await page.screenshot(path="05_after_post_click.png")
        except Exception as e:
            print(f"⚠️ Failed to click Post: {e}")
            await page.screenshot(path="05_post_click_failed.png")

        # === Final Confirmation ===
        print("⏳ Waiting extra 12 seconds for post to be published...")
        await asyncio.sleep(12)
        await page.screenshot(path="06_final_result.png")

        print("🎉 Process completed. Check all screenshots in artifacts.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(post_on_facebook("Hello testing"))
