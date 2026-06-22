import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_TXT      = "facebook_cookies.txt"      # legacy fallback only
STORAGE_STATE    = "storage_state.json"        # preferred — cookies + localStorage
CAPTIONS_TXT     = "captions.txt"
VIDEO_FILE       = "testing.mp4"
SCREENSHOTS_DIR  = Path("screenshots")

# In CI, the full storage_state JSON is passed via this env var (GitHub secret)
# rather than committed to the repo. Falls back to a local file for local runs.
STORAGE_STATE_ENV_VAR = "FB_STORAGE_STATE"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def resolve_storage_state_path() -> str | None:
    """
    Returns the raw storage_state JSON text, preferring the GitHub Actions
    secret (env var) over a local file, so CI never depends on a committed
    session file. Returns None if neither source is available.
    """
    env_val = os.environ.get(STORAGE_STATE_ENV_VAR)
    if env_val:
        try:
            json.loads(env_val)  # validate it's real JSON before trusting it
            return env_val
        except json.JSONDecodeError:
            print(f"⚠️  {STORAGE_STATE_ENV_VAR} env var is not valid JSON — ignoring")
    if Path(STORAGE_STATE).exists():
        return Path(STORAGE_STATE).read_text(encoding="utf-8")
    return None


def load_netscape_cookies(txt_file: str):
    """Legacy path — only used if no storage_state is available at all."""
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

        # ── Session: prefer storage_state (cookies + localStorage) ────────────
        # This survives Facebook's token rotation better than a flat cookie
        # export because it also carries localStorage flags FB uses to
        # recognize a previously-trusted browser/device.
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
            # Needed for the real-clipboard Ctrl+V caption-typing fallback.
            # Harmless if the browser/origin ignores it.
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
            # ── Legacy fallback: flat cookie file ──────────────────────────────
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
            # ── Always save the (possibly rotated) session before closing ──────
            # Facebook frequently rotates cookies/tokens during a session.
            # Capturing the state here — success OR failure — means next
            # run starts with fresher tokens instead of the original stale
            # export. Print the JSON to logs so a workflow step can grab it
            # and push it back into the GitHub secret.
            try:
                fresh_state = await context.storage_state()
                Path(STORAGE_STATE).write_text(json.dumps(fresh_state), encoding="utf-8")
                print(f"\n💾 Saved refreshed storage_state to {STORAGE_STATE} "
                      f"({len(fresh_state.get('cookies', []))} cookies)")
                print("STORAGE_STATE_JSON_START")
                print(json.dumps(fresh_state))
                print("STORAGE_STATE_JSON_END")
            except Exception as e:
                print(f"⚠️  Could not save refreshed storage_state: {e}")

            await browser.close()


async def _run_upload_flow(context, caption: str, video_path: str):
    """
    The actual upload steps, given an already-configured browser context.
    Browser/context lifecycle (creation, storage_state save, closing) is
    handled by the caller (upload_reel) so that state-saving always runs
    via try/finally regardless of where this function returns.
    """
    page = await context.new_page()

    # ── STEP 1: Load Facebook home (desktop) ──────────────────────────────
    print("\n🌐 Opening Facebook (desktop)…")
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        print(f"❌ Load failed: {e}")
        await save_screenshot(page, "00_load_failed")
        return
    await asyncio.sleep(8)
    await save_screenshot(page, "01_after_load")

    # ── STEP 2: Check login ───────────────────────────────────────────────
    print("🔍 Checking login…")
    if "login" in page.url or "checkpoint" in page.url:
        print("❌ Not logged in — cookies expired")
        await save_screenshot(page, "02_login_failed")
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
        return

    # ── STEP 5: Click "Next" to advance from Create→Edit panel ───────────
    # KEY FINDING from diagnostic HTML dumps: the caption field does NOT
    # exist in the DOM on the initial "Create reel" upload screen at all —
    # not as a timing issue, but structurally. It only gets mounted after
    # advancing to the "Edit reel" panel (same one where Trim video, Closed
    # captions, Audio description, etc. appear — see your screenshots).
    # The real element looks like:
    #   <div contenteditable="true" role="textbox" data-lexical-editor="true"
    #        aria-placeholder="Describe your reel...">
    # Note: aria-PLACEHOLDER, not aria-label — our old selector was checking
    # the wrong attribute, which is why it never matched at any point.
    print("\n➡️  Clicking Next to reach Edit-reel panel (where caption field lives)…")

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
        print("   ⚠️  Could not click first Next — video may still be processing. "
              "Will still try to find the caption field in case the panel already changed.")

    await asyncio.sleep(3)
    await save_screenshot(page, "05a_after_first_next")

    # ── STEP 5b: Poll for the Edit-reel panel / caption field to mount ────
    print("\n⏳ Waiting for Edit-reel panel (caption field) to appear…")

    CAPTION_FIELD_SELECTOR = 'div[contenteditable="true"][aria-placeholder="Describe your reel..."]'
    CAPTION_FIELD_SELECTOR_LOOSE = 'div[contenteditable="true"][aria-placeholder*="Describe your reel" i]'

    max_wait = 60
    poll_every = 2
    elapsed = 0
    field_ready = False

    while elapsed < max_wait:
        has_field = await page.locator(CAPTION_FIELD_SELECTOR).count() > 0
        if not has_field:
            has_field = await page.locator(CAPTION_FIELD_SELECTOR_LOOSE).count() > 0
        print(f"   …{elapsed}s — caption field present (aria-placeholder match): {has_field}")
        if has_field:
            field_ready = True
            break
        await asyncio.sleep(poll_every)
        elapsed += poll_every

    if field_ready:
        print(f"   ✅ Caption field appeared after {elapsed}s")
    else:
        print(f"   ⚠️  Caption field never appeared after {max_wait}s — will still try fallback selectors below")

    await save_screenshot(page, "05_after_processing")
    await dump_html(page, "05_page_state.html")

    # ── STEP 6: Enter caption — battery of selectors × battery of methods ──
    print("\n✍️  Entering caption…")

    # Ordered by confidence, based on the ACTUAL element confirmed present
    # in captured DOM dumps (aria-placeholder, not aria-label).
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
        ("any contenteditable",   'div[contenteditable="true"]'),  # last resort, broadest
    ]

    async def verify_caption_present(loc) -> bool:
        try:
            txt = await loc.evaluate(
                "el => (el.innerText || el.textContent || el.value || '').trim()"
            )
            return len(txt) > 0
        except Exception:
            return False

    # ── Method 1: standard click + keyboard.type ──────────────────────────
    async def method_keyboard_type(field) -> bool:
        await field.scroll_into_view_if_needed(timeout=5_000)
        await field.click(timeout=5_000)
        await asyncio.sleep(0.4)
        await field.click(timeout=5_000)  # second click: ensure innermost node focused
        await asyncio.sleep(0.3)
        await page.keyboard.type(caption, delay=40)
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    # ── Method 2: click + press_sequentially (Playwright's char-by-char API,
    #    dispatches real key events differently than .type() internally) ───
    async def method_press_sequentially(field) -> bool:
        await field.click(timeout=5_000)
        await asyncio.sleep(0.3)
        await field.press_sequentially(caption, delay=30)
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    # ── Method 3: focus via JS, then keyboard.type (sometimes .click()
    #    targets a wrapper; el.focus() in-page guarantees the right node) ──
    async def method_js_focus_then_keyboard(field) -> bool:
        await field.evaluate("el => el.focus()")
        await asyncio.sleep(0.3)
        await page.keyboard.type(caption, delay=40)
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    # ── Method 4: execCommand insertText (Lexical/Draft.js honor this) ─────
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

    # ── Method 5: direct textContent + synthetic input events ──────────────
    async def method_direct_dom_injection(field) -> bool:
        await field.evaluate(
            """(el, text) => {
                el.focus();
                el.textContent = text;
                el.dispatchEvent(new InputEvent('beforeinput', {
                    bubbles: true, cancelable: true, data: text
                }));
                el.dispatchEvent(new InputEvent('input', {
                    bubbles: true, cancelable: true, data: text
                }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            caption,
        )
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    # ── Method 6: clipboard paste simulation (some Lexical builds only
    #    accept text via a real 'paste' ClipboardEvent, not insertText) ─────
    async def method_paste_event(field) -> bool:
        await field.click(timeout=5_000)
        await asyncio.sleep(0.3)
        await field.evaluate(
            """(el, text) => {
                el.focus();
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                const evt = new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: dt
                });
                el.dispatchEvent(evt);
            }""",
            caption,
        )
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    # ── Method 7: OS-level clipboard + Ctrl+V via keyboard (closest to a
    #    real human paste — needs clipboard permissions on the context) ────
    async def method_real_clipboard_paste(field) -> bool:
        await field.click(timeout=5_000)
        await asyncio.sleep(0.3)
        try:
            await page.evaluate(
                "text => navigator.clipboard.writeText(text)", caption
            )
        except Exception as e:
            print(f"         (clipboard.writeText blocked: {e})")
            return False
        await page.keyboard.press("Control+V")
        await asyncio.sleep(0.5)
        return await verify_caption_present(field)

    methods = [
        ("keyboard.type",            method_keyboard_type),
        ("press_sequentially",       method_press_sequentially),
        ("js-focus + keyboard.type", method_js_focus_then_keyboard),
        ("execCommand insertText",   method_exec_command),
        ("direct DOM + input events", method_direct_dom_injection),
        ("synthetic paste event",    method_paste_event),
        ("real clipboard Ctrl+V",    method_real_clipboard_paste),
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

        print(f"   🎯 Selector candidate: {sel_name}  ({sel})")

        for method_name, method_fn in methods:
            try:
                # Clear any partial text from a previous failed method before
                # trying the next one, so verify_caption_present isn't fooled
                # by leftovers from an earlier attempt on the same field.
                await field.evaluate(
                    "el => { el.textContent = ''; el.innerText = ''; }"
                )
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
                print(f"      → {method_name}: ✗ exception: {e}")
                attempt_log.append((sel_name, method_name, f"exception: {e}"))

    print("\n   📊 Caption attempt summary:")
    for sel_name, method_name, status in attempt_log:
        print(f"      [{sel_name}] x [{method_name}] -> {status}")

    if caption_typed:
        print(f"\n   ✅ WORKING COMBINATION FOUND: selector='{working_selector}', "
              f"method='{working_method}'")
        await asyncio.sleep(1)
    else:
        print("\n⚠️  All selector × method combinations failed — continuing to publish anyway")
        await dump_html(page, "06_caption_failed.html")
        await dump_all_frames(page, "06_caption_failed")

    await save_screenshot(page, "06_after_caption")

    # ── STEP 7: Click through any remaining Next steps, then click Post ───
    # KEY FINDING from your screenshots: the final submission button's
    # actual visible text is "Post" — NOT "Publish" or "Share now", which
    # is why the previous selector list never matched it. The flow has
    # THREE screens: Edit reel (caption) -> Reel settings (Public, Tag and
    # collaborate, Boost reel, Scheduling options, ...) -> the "Post" button
    # is at the bottom of THIS settings screen, not a separate Publish step.
    print("\n📤 Looking for Next, then the final Post button…")

    # First: click "Next" to leave the Edit-reel/caption panel.
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
                print(f"   ⚠️  Next button disabled: {sel}")
                continue
            ok = await force_tap(page, btn)
            if ok:
                print(f"   ✅ Clicked Next via: {sel}")
                await asyncio.sleep(3)
                await save_screenshot(page, "07_after_next")
                break
        except Exception as e:
            print(f"   — Next ({sel}): {e}")

    # ── Now find and click the real submit button: "Post" ─────────────────
    # Battery of selectors, ordered by confidence (exact aria-label/text
    # match first, broad fallbacks last), each tried with multiple click
    # methods, with every combination tracked so failures are diagnosable.
    post_selectors = [
        ("aria-label Post exact",   'div[aria-label="Post"][role="button"]'),
        ("button text Post exact",  'div[role="button"]:text-is("Post")'),
        ("span text Post exact",    'span:text-is("Post")'),
        ("button[type=submit]",     'button[type="submit"]'),
        ("aria-label Publish",      'div[aria-label="Publish"][role="button"]'),
        ("aria-label Share now",    'div[aria-label="Share now"][role="button"]'),
        ("text contains Post",      'div[role="button"]:has-text("Post")'),
        ("text contains Publish",   'div[role="button"]:has-text("Publish")'),
        ("text contains Share now", 'div[role="button"]:has-text("Share now")'),
    ]

    async def click_method_force_tap(loc) -> bool:
        return await force_tap(page, loc)

    async def click_method_dispatch_event(loc) -> bool:
        try:
            await loc.evaluate(
                """el => {
                    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                }"""
            )
            return True
        except Exception as e:
            print(f"      dispatch_event failed: {e}")
            return False

    async def click_method_keyboard_enter(loc) -> bool:
        try:
            await loc.focus()
            await asyncio.sleep(0.2)
            await page.keyboard.press("Enter")
            return True
        except Exception as e:
            print(f"      keyboard Enter failed: {e}")
            return False

    click_methods = [
        ("force_tap (touch/force-click/JS chain)", click_method_force_tap),
        ("synthetic mouse event dispatch",          click_method_dispatch_event),
        ("focus + keyboard Enter",                  click_method_keyboard_enter),
    ]

    async def settings_panel_still_open() -> bool:
        """
        Used to verify a click actually had an effect: if 'Reel settings'
        text or the Post button itself is still present after a click
        attempt, the click didn't really submit anything.
        """
        try:
            still_has_post_btn = await page.locator(
                'div[aria-label="Post"][role="button"]'
            ).count() > 0
            still_has_heading = await page.locator(
                'text="Reel settings"'
            ).count() > 0
            return still_has_post_btn or still_has_heading
        except Exception:
            return False

    post_clicked = False
    post_attempt_log = []

    for sel_name, sel in post_selectors:
        if post_clicked:
            break
        btn = page.locator(sel).last
        count = await btn.count()
        if count == 0:
            post_attempt_log.append((sel_name, "—", "not found"))
            continue

        try:
            await btn.wait_for(state="visible", timeout=5_000)
        except Exception:
            post_attempt_log.append((sel_name, "—", "found but not visible"))
            continue

        disabled = await btn.get_attribute("aria-disabled")
        if disabled == "true":
            post_attempt_log.append((sel_name, "—", "disabled"))
            continue

        print(f"   🎯 Selector candidate: {sel_name}  ({sel})")

        for method_name, method_fn in click_methods:
            try:
                still_open_before = await settings_panel_still_open()
                ok = await method_fn(btn)
                await asyncio.sleep(2)
                still_open_after = await settings_panel_still_open()

                # A real successful submit should make the settings panel
                # / Post button disappear (page navigates to feed or shows
                # a "Published" confirmation).
                if ok and still_open_before and not still_open_after:
                    status = "✅ SUCCESS (panel closed)"
                    print(f"      → {method_name}: {status}")
                    post_attempt_log.append((sel_name, method_name, status))
                    post_clicked = True
                    break
                elif ok:
                    status = "⚠️ click fired but panel still open"
                    print(f"      → {method_name}: {status}")
                    post_attempt_log.append((sel_name, method_name, status))
                else:
                    status = "✗ click failed"
                    print(f"      → {method_name}: {status}")
                    post_attempt_log.append((sel_name, method_name, status))
            except Exception as e:
                print(f"      → {method_name}: ✗ exception: {e}")
                post_attempt_log.append((sel_name, method_name, f"exception: {e}"))

    print("\n   📊 Post-button attempt summary:")
    for sel_name, method_name, status in post_attempt_log:
        print(f"      [{sel_name}] x [{method_name}] -> {status}")

    if post_clicked:
        print("\n   ✅ Post button click confirmed (settings panel closed)")
    else:
        print("\n⚠️  Could not confirm Post button click — check 07_post_failed.html / screenshot")
        await dump_html(page, "07_post_failed.html")

    await save_screenshot(page, "07_after_post_attempt")

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
