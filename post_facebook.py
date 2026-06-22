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


async def dump_html(page, filename="page_debug.html"):
    try:
        html = await page.content()
        Path(filename).write_text(html, encoding="utf-8")
        print(f"   📄 {filename} saved")
    except Exception as e:
        print(f"   ⚠️  HTML dump failed: {e}")


async def dump_all_frames(page, base_name="page_debug"):
    """
    page.content() only returns the main document. If Facebook renders the
    composer inside a frame, the main-document dump will look empty/wrong
    for our purposes. This saves one file per frame so we can tell.
    """
    for i, fr in enumerate(page.frames):
        try:
            html = await fr.content()
            fname = f"{base_name}_frame{i}.html"
            Path(fname).write_text(html, encoding="utf-8")
            print(f"   📄 {fname} saved (url={fr.url[:80]!r}, {len(html)} chars)")
        except Exception as e:
            print(f"   ⚠️  Frame {i} dump failed: {e}")


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

        # ── Use DESKTOP context — mobile mbasic/lite doesn't have reel creation ──
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

        # ── STEP 1: Load Facebook home (desktop) ──────────────────────────────
        print("\n🌐 Opening Facebook (desktop)…")
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
        for sel in [
            '[aria-label="Home"]',
            '[data-pagelet="LeftRail"]',
            'div[role="feed"]',
            '[aria-label="Create"]',
            'span:has-text("What\'s on your mind?")',
        ]:
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

        # ── STEP 3: Navigate to Reels creation (desktop URL) ──────────────────
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
            await browser.close()
            return

        await asyncio.sleep(8)
        await save_screenshot(page, "03_reels_create_page")
        print(f"   URL: {page.url}")

        # ── STEP 4: Attach video file ─────────────────────────────────────────
        print("\n📁 Attaching video…")

        uploaded = False

        # Strategy A — set_input_files on hidden input directly
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

        # Strategy B — click upload button, intercept file chooser dialog
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
            await browser.close()
            return

        # ── STEP 5: Wait for video processing ────────────────────────────────
        # The countdown-leader animation (numbers in a circular reticle) is
        # Facebook's own placeholder while the upload is still transcoding —
        # it is NOT your video's actual content. We poll for signs that
        # processing has actually finished instead of guessing a fixed delay.
        print("\n⏳ Waiting for video to process…")

        max_wait = 90
        poll_every = 3
        elapsed = 0
        processing_done = False

        while elapsed < max_wait:
            await asyncio.sleep(poll_every)
            elapsed += poll_every

            # Heuristic 1: "Describe your reel" field exists and is attached
            desc_field = page.locator('div[aria-label="Describe your reel"]').first
            has_desc = await desc_field.count() > 0

            # Heuristic 2: a Next button exists and is NOT aria-disabled
            next_btn = page.locator('div[aria-label="Next"][role="button"]').first
            next_present = await next_btn.count() > 0
            next_enabled = False
            if next_present:
                disabled_attr = await next_btn.get_attribute("aria-disabled")
                next_enabled = disabled_attr != "true"

            print(f"   …{elapsed}s — caption field present: {has_desc}, "
                  f"Next present: {next_present}, Next enabled: {next_enabled}")

            if has_desc and next_present and next_enabled:
                processing_done = True
                break

        if processing_done:
            print(f"   ✅ Composer appears ready after {elapsed}s")
        else:
            print(f"   ⚠️  Composer not confirmed ready after {max_wait}s — proceeding anyway")

        await save_screenshot(page, "05_after_processing")
        await dump_html(page, "05_page_state.html")

        # ── STEP 6: Enter caption ─────────────────────────────────────────────
        print("\n✍️  Entering caption…")

        # Diagnostic: dump state across ALL frames right before we try typing,
        # so if the composer turns out to be frame-scoped we can see it here
        # instead of guessing from a top-frame-only dump after the fact.
        print(f"   🔎 Frame count on page: {len(page.frames)}")
        for i, fr in enumerate(page.frames):
            try:
                fr_url = fr.url
                fr_has_desc = await fr.locator('div[aria-label="Describe your reel"]').count()
                print(f"      frame[{i}] url={fr_url[:80]!r} has_desc_field={fr_has_desc}")
            except Exception as e:
                print(f"      frame[{i}] inspect failed: {e}")

        # Tightened selector list — no bare 'textarea' / 'div[contenteditable]'
        # at the top, since those can match hidden/duplicate nodes on FB's DOM
        # and silently grab the wrong element.
        caption_selectors = [
            'div[aria-label="Describe your reel"][contenteditable="true"]',
            'div[aria-label*="description" i][contenteditable="true"]',
            'div[aria-label*="caption" i][contenteditable="true"]',
            'div[data-lexical-editor="true"][contenteditable="true"]',
            'div[role="textbox"][contenteditable="true"]',
            'textarea[placeholder*="description" i]',
            'textarea[placeholder*="caption" i]',
        ]

        async def verify_caption_present(loc) -> bool:
            """Check the field's actual text content / value after typing."""
            try:
                txt = await loc.evaluate(
                    "el => (el.innerText || el.textContent || el.value || '').trim()"
                )
                return len(txt) > 0
            except Exception:
                return False

        async def type_via_keyboard(field) -> bool:
            try:
                await field.scroll_into_view_if_needed(timeout=5_000)
                await field.click(timeout=5_000)
                await asyncio.sleep(0.4)
                # Click again to ensure focus lands on the innermost editable
                # node rather than a wrapping role="button" container.
                await field.click(timeout=5_000)
                await asyncio.sleep(0.3)
                await page.keyboard.type(caption, delay=40)
                await asyncio.sleep(0.5)
                return await verify_caption_present(field)
            except Exception as e:
                print(f"      keyboard.type attempt failed: {e}")
                return False

        async def type_via_js_injection(field) -> bool:
            """
            Fallback for Lexical/Draft.js editors: focus, select all content,
            then try execCommand('insertText', ...) first (most Lexical builds
            honor this), and if that leaves the field empty, fall back to
            directly setting textContent and dispatching beforeinput/input/
            change events so React/Lexical state updates pick it up.
            """
            try:
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

                        if (!(el.innerText || el.textContent || '').trim()) {
                            el.textContent = text;
                            el.dispatchEvent(new InputEvent('beforeinput', {
                                bubbles: true, cancelable: true, data: text
                            }));
                            el.dispatchEvent(new InputEvent('input', {
                                bubbles: true, cancelable: true, data: text
                            }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""",
                    caption,
                )
                await asyncio.sleep(0.5)
                return await verify_caption_present(field)
            except Exception as e:
                print(f"      JS injection attempt failed: {e}")
                return False

        caption_typed = False
        search_contexts = [("main", page)] + [
            (f"frame[{i}]", fr) for i, fr in enumerate(page.frames) if fr != page.main_frame
        ]

        for ctx_name, ctx in search_contexts:
            if caption_typed:
                break
            for sel in caption_selectors:
                try:
                    field = ctx.locator(sel).first
                    if await field.count() == 0:
                        continue
                    await field.wait_for(state="visible", timeout=8_000)

                    print(f"   🎯 Trying: {sel} (in {ctx_name})")
                    ok = await type_via_keyboard(field)
                    if not ok:
                        print("      ↳ keyboard typing left field empty, trying JS injection…")
                        ok = await type_via_js_injection(field)

                    if ok:
                        print(f"   ✅ Caption confirmed present via: {sel} (in {ctx_name})")
                        caption_typed = True
                        break
                    else:
                        print(f"      ↳ field still empty after both attempts, trying next selector")
                except Exception as e:
                    print(f"   — {sel} (in {ctx_name}): {e}")

        if not caption_typed:
            print("⚠️  Could not type caption — continuing to publish anyway")
            await dump_html(page, "06_caption_failed.html")
            await dump_all_frames(page, "06_caption_failed")
        else:
            await asyncio.sleep(2)

        await save_screenshot(page, "06_after_caption")

        # ── STEP 7: Click Next (if present) then Publish/Share ────────────────
        print("\n📤 Looking for Next / Publish / Share…")

        for step_name, selectors in [
            ("Next", [
                'div[aria-label="Next"][role="button"]',
                'div[role="button"]:has-text("Next")',
                'span:has-text("Next")',
                'button:has-text("Next")',
            ]),
            ("Publish/Share", [
                'div[aria-label="Publish"][role="button"]',
                'div[aria-label="Share now"][role="button"]',
                'div[role="button"]:has-text("Publish")',
                'div[role="button"]:has-text("Share now")',
                'div[role="button"]:has-text("Share")',
                'span:has-text("Publish")',
                'span:has-text("Share now")',
                'button[type="submit"]',
            ]),
        ]:
            for sel in selectors:
                try:
                    btn = page.locator(sel).last
                    if await btn.count() == 0:
                        continue
                    await btn.wait_for(state="visible", timeout=8_000)
                    disabled = await btn.get_attribute("aria-disabled")
                    if disabled == "true":
                        print(f"   ⚠️  {step_name} button disabled: {sel}")
                        continue
                    ok = await force_tap(page, btn)
                    if ok:
                        print(f"   ✅ Clicked {step_name} via: {sel}")
                        await asyncio.sleep(5)
                        await save_screenshot(page, f"07_after_{step_name.lower().replace('/', '_')}")
                        break
                except Exception as e:
                    print(f"   — {step_name} ({sel}): {e}")

        # ── STEP 8: Wait and confirm ──────────────────────────────────────────
        print("\n⏳ Waiting for reel to publish (25 s)…")
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

        await browser.close()


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
