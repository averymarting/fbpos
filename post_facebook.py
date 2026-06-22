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
        
        print("🌐 Opening Facebook...")
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(8)
        await page.screenshot(path="01_facebook_home.png")

        print("🔄 Trying to open create post dialog...")

        # Try multiple ways to open post composer
        try:
            post_selectors = [
                'div[role="button"]:has-text("What\'s on your mind?")',
                'span:has-text("What\'s on your mind?")',
                '[aria-label="Create a post"]',
                'div.x1i10hfl.x1q0g3np'  # Common class pattern
            ]
            
            for selector in post_selectors:
                try:
                    post_box = await page.wait_for_selector(selector, timeout=8000)
                    if post_box:
                        await post_box.click()
                        print(f"✅ Clicked post box using: {selector}")
                        break
                except:
                    continue
            await asyncio.sleep(5)
            await page.screenshot(path="02_post_box_opened.png")
        except Exception as e:
            print(f"⚠️ Failed to open post box: {e}")

        # Type message
        try:
            await page.wait_for_selector('div[role="textbox"], div[contenteditable="true"]', timeout=10000)
            editor = page.locator('div[role="textbox"], div[contenteditable="true"]').first
            await editor.click()
            await editor.fill(message)
            print(f"✅ Typed: {message}")
            await asyncio.sleep(4)
            await page.screenshot(path="03_message_typed.png")
        except Exception as e:
            print(f"⚠️ Failed to type message: {e}")

        # Click Post button
        try:
            print("📤 Clicking Post button...")
            await page.wait_for_selector('div[role="button"]:has-text("Post")', timeout=10000)
            post_button = page.locator('div[role="button"]:has-text("Post")').last
            await post_button.click()
            print("✅ Clicked Post button")
            await asyncio.sleep(12)   # Increased wait time as requested
            await page.screenshot(path="04_after_post_click.png")
        except Exception as e:
            print(f"⚠️ Failed to click Post: {e}")

        # Extra wait to confirm post is published
        print("⏳ Waiting 15 seconds to confirm post is shared...")
        await asyncio.sleep(15)
        await page.screenshot(path="05_final_result.png")

        print("🎉 Post attempt finished. Check screenshots in artifacts.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(post_on_facebook("Hello testing"))
