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

        # Load cookies
        if Path(COOKIES_TXT).exists():
            cookies = load_netscape_cookies(COOKIES_TXT)
            await context.add_cookies(cookies)
        else:
            print(f"❌ {COOKIES_TXT} not found!")
            await browser.close()
            return

        page = await context.new_page()
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(6)

        print("🔄 Attempting to create post...")

        # Click on "What's on your mind?" 
        try:
            # Multiple possible selectors (Facebook changes them often)
            post_box = await page.wait_for_selector(
                'div[role="button"]:has-text("What\'s on your mind?"), '
                'span:has-text("What\'s on your mind?"), '
                '[aria-label="Create a post"]',
                timeout=15000
            )
            await post_box.click()
            print("✅ Clicked on post box")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"⚠️ Could not find post box: {e}")
            await browser.close()
            return

        # Type the message
        try:
            editor = await page.wait_for_selector(
                'div[role="textbox"], div[contenteditable="true"]',
                timeout=10000
            )
            await editor.click()
            await editor.fill(message)
            print(f"✅ Typed message: {message}")
            await asyncio.sleep(2)
        except:
            print("⚠️ Could not type message")
            await browser.close()
            return

        # Click Post button
        try:
            post_button = await page.wait_for_selector(
                'div[role="button"]:has-text("Post"), button:has-text("Post")',
                timeout=10000
            )
            await post_button.click()
            print("✅ Clicked Post button")
            await asyncio.sleep(5)
        except:
            print("⚠️ Could not click Post button")

        # Take screenshot for verification
        await page.screenshot(path="facebook_post_result.png")
        print("📸 Screenshot saved: facebook_post_result.png")

        print("🎉 Post attempt completed!")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(post_on_facebook("Hello testing"))
