import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_TXT   = "facebook_cookies.txt"
STORAGE_STATE = "storage_state.json"
CAPTIONS_TXT  = "captions.txt"
VIDEO_FILE    = "testing.mp4"
SCREENSHOTS_DIR = Path("screenshots")

STORAGE_STATE_ENV_VAR = "FB_STORAGE_STATE"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def resolve_storage_state_path() -> str | None:
    env_val = os.environ.get(STORAGE_STATE_ENV_VAR)
    if env_val:
        try:
            json.loads(env_val)
            return env_val
        except json.JSONDecodeError:
            print(f"⚠️  {STORAGE_STATE_ENV_VAR} env var is not valid JSON — ignoring")
    if Path(STORAGE_STATE).exists():
        return Path(STORAGE_STATE).read_text(encoding="utf-8")
    return None


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
    print(f"✅ Loaded {len(cookies)} cookies (legacy mode)")
    return cookies


async def save_screenshot(page, name: str):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    for p in [SCREENSHOTS_DIR / f"{name}.png", Path(f"{name}.png")]:
        try:
            await page.screenshot(path=str(p), full_page=False)
            print(f"📸 {p}")
        except Exception as e:
            print(f"⚠️  Screenshot failed {p}: {e}")


async def dump_html(page, filename="page_debug.html"):
    try:
        html = await page.content()
        Path(filename).write_text(html, encoding="utf-8")
        print(f"   📄 {filename} saved")
    except Exception as e:
        print(f"   ⚠️  HTML dump failed: {e}")


async def dump_all_frames(page, base_name="page_debug"):
    for i, fr in enumerate(page.frames):
        try:
            html = await fr.content()
            fname = f"{base_name}_frame{i}.html"
            Path(fname).write_text(html, encoding="utf-8")
            print(f"   📄 {fname} saved (url={fr.url[:80]!r}, {len(html)} chars)")
        except Exception as e:
            print(f"   ⚠️  Frame {i} dump failed: {e}")


async def force_tap(page, locator):
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

        storage_state_json = resolve_storage_state_path()

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
            permissions=["clipboard-read", "clipboard-write"],
        )

        using_storage_state = False
        if storage_state_json:
            try:
                state_dict = json.loads(storage_state_json)
                context_kwargs["storage_state"] = state_dict
                using_storage_state = True
                print(f"✅ Loaded storage_state ({len(state_dict.get('cookies', []))} cookies, "
                      f"{len(state_dict.get('origins', []))} origin(s) with localStorage)")
            except json.JSONDecodeError:
                print("⚠️  storage_state content invalid — will try legacy cookie file")

        context = await browser.new_context(**context_kwargs)

        try:
            if not using_storage_state:
                if not Path(COOKIES_TXT).exists():
                    print(f"❌ Neither {STORAGE_STATE_ENV_VAR} env var, {STORAGE_STATE}, "
                          f"nor {COOKIES_TXT} found")
                    return
                cookies = load_netscape_cookies(COOKIES_TXT)
                if not cookies:
                    print("❌ No cookies loaded")
                    return
                await context.add_cookies(cookies)

            await _run_upload_flow(context, caption, video_path)

        finally:
            try:
                fresh_state = await context.storage_state()
                Path(STORAGE_STATE).write_text(json.dumps(fresh_state), encoding="utf-8")
                print(f"\n💾 Saved refreshed storage_state to {STORAGE_STATE} "
                      f"({len(fresh_state.get('cookies', []))} cookies)")
            except Exception as e:
                print(f"⚠️  Could not save refreshed storage_state: {e}")

            await browser.close()


async def _run_upload_flow(context, caption: str, video_path: str):
    page = await context.new_page()

    # ── STEP 1: Load Facebook ─────────────────────────────────────────────
    print("\n🌐 Opening Facebook…")
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print(f"❌ Load failed: {e}")
        await save_screenshot(page, "00_load_failed")
        return
    await asyncio.sleep(8)
    await save_screenshot(page, "01_after_load")
    print(f"   URL: {page.url}")

    # ── STEP 2: Login check — handle profile picker ───────────────────────
    print("🔍 Checking login state…")

    # ── 2a: Account Center profile picker (/login/device-based/login/caa/)
    # This is NOT a real logout. Facebook sees a new device fingerprint but
    # valid cookies and shows "Continue as [Name]". We must click Continue.
    # It can appear at the initial load OR after a redirect.
    for attempt in range(3):
        current_url = page.url
        print(f"   URL: {current_url}")

        if 'device-based' in current_url or '/caa/' in current_url or 'login/caa' in current_url:
            print(f"   ℹ️  Profile picker detected (attempt {attempt+1}) — clicking Continue…")
            clicked = False
            for sel in [
                '[aria-label^="Continue"]',
                'div[role="button"][aria-label^="Continue"]',
                'a[aria-label^="Continue"]',
                'div[role="button"]:has-text("Continue")',
                'span:has-text("Continue")',
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click(timeout=8_000)
                        print(f"   ✅ Clicked Continue via: {sel}")
                        await asyncio.sleep(6)
                        await save_screenshot(page, f"02{chr(97+attempt)}_after_continue")
                        clicked = True
                        break
                except Exception as e:
                    print(f"   — {sel}: {e}")

            if not clicked:
                print("   ❌ Profile picker — could not click Continue")
                await dump_html(page, "02_picker_failed.html")
                await save_screenshot(page, "02_picker_failed")
                return
            continue  # re-check URL after click

        # ── 2b: Real login wall (no cookies / expired)
        if '/login' in current_url and 'device-based' not in current_url:
            print("❌ Not logged in — storage_state cookies are expired")
            print("   → Export a fresh storage_state.json from Chrome while logged in")
            await save_screenshot(page, "02_login_failed")
            return

        # ── 2c: Account checkpoint (suspended/locked)
        if 'checkpoint' in current_url:
            print("❌ Account checkpoint — manual intervention required")
            await save_screenshot(page, "02_checkpoint")
            return

        # ── 2d: We're on a normal FB page — verify feed elements
        login_ok = False
        for sel in [
            '[aria-label="Home"]',
            '[data-pagelet="LeftRail"]',
            'div[role="feed"]',
            '[aria-label="Create"]',
            'span:has-text("What\'s on your mind?")',
            'div[aria-label="Stories"]',
            'div[aria-label="Reels"]',
        ]:
            if await page.locator(sel).count() > 0:
                login_ok = True
                print(f"   ✅ Logged in (confirmed: {sel})")
                break

        if login_ok:
            break

        # Not picker, not login wall, not feed — wait a bit more and retry
        print(f"   ⏳ Feed not ready yet (attempt {attempt+1}) — waiting…")
        await asyncio.sleep(5)
    else:
        print(f"❌ Login check exhausted — URL: {page.url}")
        await dump_html(page, "02_login_failed.html")
        await save_screenshot(page, "02_login_failed")
        return

    await save_screenshot(page, "02_logged_in")

    # ── STEP 3: Navigate to Reels creation ───────────────────────────────
    print("\n🎬 Navigating to Reels create page…")
    try:
        await page.goto(
            "https://www.facebook.com/reels/create/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
    except Exception as e:
        print(f"❌ Navigation failed: {e}")
        await save_screenshot(page, "03_nav_failed")
        return

    await asyncio.sleep(8)
    await save_screenshot(page, "03_reels_create_page")
    print(f"   URL: {page.url}")

    # ── STEP 4: Attach video file ─────────────────────────────────────────
    print("\n📁 Attaching video…")

    uploaded = False

    for sel in ['input[type="file"][accept*="video"]', 'input[type="file"]']:
        try:
            inp = page.locator(sel).first
            if await inp.count() > 0:
                print(f"   🎯 Direct file input found: {sel}")
                await inp.set_input_files(video_path)
                print(f"   ✅ File attached via direct input")
                uploaded = True
                break
        except Exception as e:
            print(f"   — Direct input failed ({sel}): {e}")

    if not uploaded:
        upload_btn_selectors = [
            'div[role="button"]:has-text("Select video")',
            'div[role="button"]:has-text("Upload")',
            'div[role="button"]:has-text("Add video")',
            'span:has-text("Select video")',
            'span:has-text("Select Video")',
            '[aria-label="Select video"]',
            'div[aria-label="Add to reel"]',
            'div:has-text("Select video from computer")',
        ]
        for sel in upload_btn_selectors:
            el = page.locator(sel).first
            if await el.count() > 0:
                print(f"   🎯 Upload button: {sel}")
                try:
                    async with page.expect_file_chooser(timeout=15_000) as fc_info:
                        await el.click(force=True)
                    fc = await fc_info.value
                    await fc.set_files(video_path)
                    print(f"   ✅ File attached via file chooser")
                    uploaded = True
                    break
                except Exception as e:
                    print(f"   — File chooser failed: {e}")
            else:
                print(f"   — Not found: {sel}")

    await save_screenshot(page, "04_after_upload_attempt")
    await dump_html(page, "04_page_state.html")

    if not uploaded:
        print("❌ Could not attach video — check 04_page_state.html")
        return

    # ── STEP 5: Click "Next" ──────────────────────────────────────────────
    print("\n➡️  Clicking Next…")

    first_next_clicked = False
    for sel in [
        'div[aria-label="Next"][role="button"]',
        'div[role="button"]:has-text("Next")',
        'span:has-text("Next")',
        'button:has-text("Next")',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            await btn.wait_for(state="visible", timeout=15_000)
            disabled = await btn.get_attribute("aria-disabled")
            if disabled == "true":
                print(f"   ⚠️  Next button disabled via {sel} — video may still be processing")
                continue
            ok = await force_tap(page, btn)
            if ok:
                print(f"   ✅ Clicked first Next via: {sel}")
                first_next_clicked = True
                break
        except Exception as e:
            print(f"   — {sel}: {e}")

    if not first_next_clicked:
        print("   ⚠️  Could not click first Next — will still try caption field")

    await asyncio.sleep(3)
    await save_screenshot(page, "05a_after_first_next")

    # ── STEP 5b: Wait for caption field ──────────────────────────────────
    print("\n⏳ Waiting for caption field…")

    CAPTION_FIELD_SELECTOR       = 'div[contenteditable="true"][aria-placeholder="Describe your reel..."]'
    CAPTION_FIELD_SELECTOR_LOOSE = 'div[contenteditable="true"][aria-placeholder*="Describe your reel" i]'

    max_wait = 60
    poll_every = 2
    elapsed = 0
    field_ready = False

    while elapsed < max_wait:
        has_field = await page.locator(CAPTION_FIELD_SELECTOR).count() > 0
        if not has_field:
            has_field = await page.locator(CAPTION_FIELD_SELECTOR_LOOSE).count() > 0
        print(f"   …{elapsed}s — caption field present: {has_field}")
        if has_field:
            field_ready = True
            break
        await asyncio.sleep(poll_every)
        elapsed += poll_every

    await save_screenshot(page, "05_after_processing")
    await dump_html(page, "05_page_state.html")

    # ── STEP 6: Enter caption ─────────────────────────────────────────────
    print("\n✍️  Entering caption…")

    caption_selectors = [
        ("aria-placeholder exact",  'div[contenteditable="true"][aria-placeholder="Describe your reel..."]'),
        ("aria-placeholder loose",  'div[contenteditable="true"][aria-placeholder*="Describe your reel" i]'),
        ("aria-placeholder any",    'div[contenteditable="true"][aria-placeholder*="reel" i]'),
        ("lexical editor",          'div[data-lexical-editor="true"][contenteditable="true"]'),
        ("role textbox",           'div[role="textbox"][contenteditable="true"]'),
        ("aria-label description", 'div[aria-label*="description" i][contenteditable="true"]'),
        ("aria-label caption",     'div[aria-label*="caption" i][contenteditable="true"]'),
        ("textarea description",  'textarea[placeholder*="description" i]'),
        ("textarea caption",      'textarea[placeholder*="caption" i]'),
        ("any contenteditable",   'div[contenteditable="true"]'),
    ]

    async def verify_caption_present(loc) -> bool:
        try:
            txt = await loc.evaluate(
                "el => (el.innerText || el.textContent || el.value || '').trim()"
            )
            return len(txt) > 0
        except Exception:
            return False

    async def method_keyboard_type(field) -> bool:
        await field.scroll_into_view_if_needed(timeout=5_000)
        await field.click(timeout=5_000)
        await asyncio.sleep(0.4)
        await field.click(timeout=5_000)
        await asyncio.sleep(0.3)
        await page.keyboard.type(caption, delay=40)
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    async def method_press_sequentially(field) -> bool:
        await field.click(timeout=5_000)
        await asyncio.sleep(0.3)
        await field.press_sequentially(caption, delay=30)
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    async def method_js_focus_then_keyboard(field) -> bool:
        await field.evaluate("el => el.focus()")
        await asyncio.sleep(0.3)
        await page.keyboard.type(caption, delay=40)
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    async def method_exec_command(field) -> bool:
        await field.click(timeout=5_000)
        await asyncio.sleep(0.3)
        await field.evaluate(
            """(el, text) => {
                el.focus();
                const sel = window.getSelection();
                sel.removeAllRanges();
                const range = document.createRange();
                range.selectNodeContents(el);
                sel.addRange(range);
                document.execCommand('insertText', false, text);
            }""",
            caption,
        )
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    async def method_direct_dom_injection(field) -> bool:
        await field.evaluate(
            """(el, text) => {
                el.focus();
                el.textContent = text;
                el.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: text }));
                el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: text }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            caption,
        )
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    async def method_paste_event(field) -> bool:
        await field.click(timeout=5_000)
        await asyncio.sleep(0.3)
        await field.evaluate(
            """(el, text) => {
                el.focus();
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                const evt = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt });
                el.dispatchEvent(evt);
            }""",
            caption,
        )
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    async def method_real_clipboard_paste(field) -> bool:
        await field.click(timeout=5_000)
        await asyncio.sleep(0.3)
        try:
            await page.evaluate("text => navigator.clipboard.writeText(text)", caption)
        except Exception as e:
            print(f"         (clipboard.writeText blocked: {e})")
            return False
        await page.keyboard.press("Control+V")
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    methods = [
        ("keyboard.type",             method_keyboard_type),
        ("press_sequentially",        method_press_sequentially),
        ("js-focus + keyboard.type",  method_js_focus_then_keyboard),
        ("execCommand insertText",    method_exec_command),
        ("direct DOM + input events", method_direct_dom_injection),
        ("synthetic paste event",     method_paste_event),
        ("real clipboard Ctrl+V",     method_real_clipboard_paste),
    ]

    caption_typed = False
    working_selector = None
    working_method = None
    attempt_log = []

    for sel_name, sel in caption_selectors:
        if caption_typed:
            break
        field = page.locator(sel).first
        count = await field.count()
        if count == 0:
            attempt_log.append((sel_name, "—", "not found"))
            continue
        try:
            await field.wait_for(state="visible", timeout=5_000)
        except Exception:
            attempt_log.append((sel_name, "—", "found but not visible"))
            continue
        print(f"   🎯 Selector candidate: {sel_name}")
        for method_name, method_fn in methods:
            try:
                await field.evaluate("el => { el.textContent = ''; el.innerText = ''; }")
                await asyncio.sleep(0.2)
                ok = await method_fn(field)
                status = "✅ SUCCESS" if ok else "✗ empty after attempt"
                print(f"      → {method_name}: {status}")
                attempt_log.append((sel_name, method_name, status))
                if ok:
                    caption_typed = True
                    working_selector = sel_name
                    working_method = method_name
                    break
            except Exception as e:
                print(f"      → {method_name}: ✗ {e}")
                attempt_log.append((sel_name, method_name, f"exception: {e}"))

    if caption_typed:
        print(f"\n   ✅ Caption entered: selector='{working_selector}', method='{working_method}'")
    else:
        print("\n⚠️  All caption methods failed — continuing to publish anyway")
        await dump_html(page, "06_caption_failed.html")

    await save_screenshot(page, "06_after_caption")

    # ── STEP 7: Next → Post ───────────────────────────────────────────────
    print("\n📤 Looking for Next / Post button…")

    for sel in [
        'div[aria-label="Next"][role="button"]',
        'div[role="button"]:has-text("Next")',
        'span:has-text("Next")',
        'button:has-text("Next")',
    ]:
        try:
            btn = page.locator(sel).last
            if await btn.count() == 0:
                continue
            await btn.wait_for(state="visible", timeout=8_000)
            disabled = await btn.get_attribute("aria-disabled")
            if disabled == "true":
                continue
            ok = await force_tap(page, btn)
            if ok:
                print(f"   ✅ Clicked Next via: {sel}")
                await asyncio.sleep(3)
                await save_screenshot(page, "07_after_next")
                break
        except Exception as e:
            print(f"   — Next ({sel}): {e}")

    post_selectors = [
        ("aria-label Post",    'div[aria-label="Post"][role="button"]'),
        ("text Post exact",    'div[role="button"]:text-is("Post")'),
        ("span Post exact",    'span:text-is("Post")'),
        ("submit button",      'button[type="submit"]'),
        ("aria-label Publish", 'div[aria-label="Publish"][role="button"]'),
        ("aria-label Share",   'div[aria-label="Share now"][role="button"]'),
        ("text Post",          'div[role="button"]:has-text("Post")'),
        ("text Publish",       'div[role="button"]:has-text("Publish")'),
        ("text Share now",     'div[role="button"]:has-text("Share now")'),
    ]

    async def settings_panel_still_open() -> bool:
        try:
            return (
                await page.locator('div[aria-label="Post"][role="button"]').count() > 0
                or await page.locator('text="Reel settings"').count() > 0
            )
        except Exception:
            return False

    async def click_dispatch(loc) -> bool:
        try:
            await loc.evaluate(
                """el => {
                    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    el.dispatchEvent(new MouseEvent('mouseup',   { bubbles: true }));
                    el.dispatchEvent(new MouseEvent('click',     { bubbles: true }));
                }"""
            )
            return True
        except Exception as e:
            print(f"      dispatch failed: {e}")
            return False

    async def click_enter(loc) -> bool:
        try:
            await loc.focus()
            await asyncio.sleep(0.2)
            await page.keyboard.press("Enter")
            return True
        except Exception as e:
            print(f"      Enter failed: {e}")
            return False

    click_methods = [
        ("force_tap",         lambda loc: force_tap(page, loc)),
        ("dispatch events",   click_dispatch),
        ("focus + Enter",     click_enter),
    ]

    post_clicked = False
    for sel_name, sel in post_selectors:
        if post_clicked:
            break
        btn = page.locator(sel).last
        if await btn.count() == 0:
            continue
        try:
            await btn.wait_for(state="visible", timeout=5_000)
        except Exception:
            continue
        if await btn.get_attribute("aria-disabled") == "true":
            continue
        print(f"   🎯 Post button candidate: {sel_name}")
        for method_name, method_fn in click_methods:
            try:
                still_open_before = await settings_panel_still_open()
                ok = await method_fn(btn)
                await asyncio.sleep(2)
                still_open_after = await settings_panel_still_open()
                if ok and still_open_before and not still_open_after:
                    print(f"      → {method_name}: ✅ SUCCESS")
                    post_clicked = True
                    break
                elif ok:
                    print(f"      → {method_name}: ⚠️ fired but panel still open")
                else:
                    print(f"      → {method_name}: ✗")
            except Exception as e:
                print(f"      → {method_name}: ✗ {e}")

    if not post_clicked:
        print("\n⚠️  Could not confirm Post click")
        await dump_html(page, "07_post_failed.html")
    await save_screenshot(page, "07_after_post_attempt")

    # ── STEP 8: Confirm ───────────────────────────────────────────────────
    print("\n⏳ Waiting 25 s for publish…")
    await asyncio.sleep(25)
    await save_screenshot(page, "08_final_result")
    await dump_html(page, "08_final_page.html")

    for sel in [
        'span:has-text("Your reel is now shared")',
        'span:has-text("Reel posted")',
        'span:has-text("Published")',
        'span:has-text("Your reel")',
        'div:has-text("Your reel was shared")',
    ]:
        if await page.locator(sel).count() > 0:
            print(f"🎉 Reel published! (confirmed: {sel})")
            break
    else:
        print("🎉 Process completed — check screenshots/08_final_result.png")


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    caption_path = Path(CAPTIONS_TXT)
    if caption_path.exists():
        caption = caption_path.read_text(encoding="utf-8").strip()
        print(f"📝 Caption loaded from {CAPTIONS_TXT}")
    else:
        caption = "Check out my latest reel! #reels"
        print(f"⚠️  {CAPTIONS_TXT} not found — using default caption")

    asyncio.run(upload_reel(caption=caption, video_path=VIDEO_FILE))
