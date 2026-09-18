import json
import os
import re
import html
import requests
import asyncio

# Browser configuration. This is intentionally NOT domain-specific.
# Any website can be rendered in Chromium when the page needs JavaScript.
BROWSER_TIMEOUT_MS = 30_000
BROWSER_WAIT_MS = 6_000
BROWSER_HEADLESS = True
BROWSER_AUTO_INSTALL = False

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("[WARN] Playwright package is not installed.")

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
Application,
CommandHandler,
MessageHandler,
ContextTypes,
filters,
)

# ============================================================

# CONFIG

# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ROUTES_FILE = "routes.json"

MAX_URLS_PER_MESSAGE = 9999
MAX_DEBUG_LINKS = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
"Chrome/139.0 Safari/537.36"
),
"Accept": (
        "text/html,application/xhtml+xml,application/xml;"
"q=0.9,*/*;q=0.8"
),
"Accept-Language": "en-US,en;q=0.9",
}

# ============================================================

# SPACEBIN

# ============================================================

SPACEBIN_API = "https://spaceb.in/api/"

def upload_to_spacebin(content):
    """Upload text to Spacebin and return its public URL."""

    try:
        response = requests.post(
            SPACEBIN_API,
            json={"content": content},
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Content-Type": "application/json",
            },
            timeout=30,
        )

        response.raise_for_status()
        data = response.json()

        if data.get("error"):
            raise RuntimeError(str(data["error"]))

        paste_id = (data.get("payload") or {}).get("id")

        if not paste_id:
            raise RuntimeError("Spacebin did not return a paste ID.")

        return f"https://spaceb.in/{paste_id}"

    except Exception as e:
        print("[SPACEBIN ERROR]", e)
        return None

# ============================================================

# ROUTE STORAGE

# ============================================================

def load_routes():
    if not os.path.exists(ROUTES_FILE):
        return {}

    try:
        with open(
            ROUTES_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(data, dict):
            return data

        return {}

    except Exception as e:

        print(
            "routes.json read error:",
            e
        )

        return {}

def save_routes(routes):

    temp_file = ROUTES_FILE + ".tmp"

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            routes,
            f,
            indent=4,
            ensure_ascii=False
        )

    os.replace(
        temp_file,
        ROUTES_FILE
    )

# ============================================================

# RESULT MESSAGE STORE

# ============================================================

# Stores resolved final URLs by Telegram message ID.

# It is intentionally kept in memory; restart of the bot clears it.

RESULT_MESSAGES = {}

# ============================================================

# URL HELPERS

# ============================================================

def clean_domain(domain):

    domain = str(
        domain
    ).strip().lower()

    domain = re.sub(
        r"^https?://",
        "",
        domain
    )

    domain = domain.split("/")[0]

    domain = domain.split("?")[0]

    domain = domain.rstrip(".")

    return domain

def valid_url(url):

    return url.startswith(
        (
            "http://",
            "https://"
        )
    )

def domain_matches(
url,
target
):

    try:

        hostname = urlparse(
            url
        ).hostname

        if not hostname:
            return False

        hostname = hostname.lower()

        target = clean_domain(
            target
        )

        return (
            hostname == target
            or hostname.endswith(
                "." + target
            )
        )

    except Exception:

        return False

# ============================================================

# OLD DOMAIN -> CURRENT DOMAIN

# ============================================================

def normalize_start_url(url, route):
    """
    Rewrites old/rotated domains to the route's current main_domain,
    preserving path, query and fragment.

    Works for:
      - HubCloud  (hostnames starting with "hubcloud.")
      - gdflix    (hostnames containing "gdflix")
    """

    main_domain = route.get("main_domain")

    if not main_domain:
        return url

    main_domain = clean_domain(main_domain)

    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        # Already the current domain: nothing to do.
        if hostname == main_domain:
            return url

        # ----------------------------------------------------
        # Decide whether this hostname belongs to a family
        # that should be upgraded to main_domain.
        # ----------------------------------------------------
        should_rewrite = (
            hostname.startswith("hubcloud.")   # HubCloud family
            or "gdflix" in hostname            # gdflix family
        )

        if should_rewrite:
            new_url = f"https://{main_domain}{parsed.path}"

            if parsed.query:
                new_url += "?" + parsed.query

            if parsed.fragment:
                new_url += "#" + parsed.fragment

            print("[NORMALIZE]", url, "->", new_url)
            return new_url

    except Exception as e:
        print("[NORMALIZE ERROR]", e)

    return url

# ============================================================
# GENERIC LINK EXTRACTION + BROWSER RENDERING
# ============================================================

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PageResult:
    """Small response-compatible object used by the bot."""

    url: str
    text: str
    status_code: int = 200
    title: str = ""


def normalize_link(base_url, value):
    """Convert a possible link into an absolute HTTP(S) URL."""

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    lowered = value.lower()

    if lowered.startswith((
        "javascript:",
        "mailto:",
        "tel:",
        "data:",
        "blob:",
        "#",
    )):
        return None

    try:
        result = urljoin(base_url, value)

        if not result.startswith(("http://", "https://")):
            return None

        return result

    except Exception:
        return None


def add_extracted_link(links, seen, base_url, value):
    """Add a valid unique URL to the extraction result."""

    url = normalize_link(
        base_url,
        value
    )

    if url and url not in seen:
        seen.add(url)
        links.append(url)


def extract_links_from_html(base_url, html_text):
    """
    Generic HTML extractor.

    It looks beyond <a href> so dynamically generated download pages
    can expose URLs stored in data-* attributes, forms, media tags,
    inline JavaScript, onclick handlers, and other common locations.
    """

    soup = BeautifulSoup(
        html_text or "",
        "html.parser"
    )

    links = []
    seen = set()

    # --------------------------------------------------------
    # Actual navigational links.
    # Do NOT scrape every src attribute: Next.js, CSS, images, fonts,
    # analytics, etc. are page resources, not links a user can click.
    # --------------------------------------------------------

    for tag in soup.find_all(["a", "area"]):

        for attribute in (
            "href",
            "data-href",
            "data-url",
            "data-link",
            "data-target",
        ):
            value = tag.get(attribute)

            if not value:
                continue

            add_extracted_link(
                links,
                seen,
                base_url,
                value
            )

            # Some sites put several URLs inside one attribute.
            for match in re.findall(
                r'https?://[^\s\'"<>\\]+',
                str(value)
            ):
                add_extracted_link(
                    links,
                    seen,
                    base_url,
                    match.rstrip("',);]}")
                )

    # --------------------------------------------------------
    # Forms and embedded documents are navigational targets.
    # --------------------------------------------------------

    for tag in soup.find_all("form"):
        add_extracted_link(
            links,
            seen,
            base_url,
            tag.get("action")
        )

    for tag in soup.find_all("iframe"):
        add_extracted_link(
            links,
            seen,
            base_url,
            tag.get("src")
        )

    # --------------------------------------------------------
    # Buttons / interactive elements.
    # --------------------------------------------------------

    for tag in soup.find_all(["button", "input"]):
        for attribute in (
            "formaction",
            "data-href",
            "data-url",
            "data-link",
            "data-download",
            "data-target",
            "data-file",
            "data-path",
            "onclick",
        ):
            value = tag.get(attribute)
            if not value:
                continue

            add_extracted_link(
                links,
                seen,
                base_url,
                value
            )

            for match in re.findall(
                r'https?://[^\s\'"<>\\]+',
                str(value)
            ):
                add_extracted_link(
                    links,
                    seen,
                    base_url,
                    match.rstrip("',);]}")
                )

    # --------------------------------------------------------
    # Inline JavaScript handlers on any element.
    # --------------------------------------------------------

    for tag in soup.find_all(True):
        for attribute in (
            "onclick",
            "onmousedown",
            "onmouseup",
            "onchange",
            "onsubmit",
        ):
            value = tag.get(attribute)

            if not value:
                continue

            for match in re.findall(
                r'https?://[^\s\'"<>\\]+',
                str(value)
            ):
                add_extracted_link(
                    links,
                    seen,
                    base_url,
                    match.rstrip("',);]}")
                )

    # --------------------------------------------------------
    # URLs embedded in JavaScript / inline HTML
    # --------------------------------------------------------

    for match in re.findall(
        r'https?://[^\s\'"<>\\]+',
        html_text or ""
    ):
        add_extracted_link(
            links,
            seen,
            base_url,
            match.rstrip("',);]}")
        )

    return links


def page_looks_dynamic(html_text):
    """Return True when raw HTML is likely to need a real browser."""

    text = (html_text or "").lower()

    markers = (
        "<script",
        "<button",
        "__next",
        "react",
        "onclick=",
        "data-href=",
        "data-url=",
        "data-download=",
        "data-link=",
        "formaction=",
    )

    return any(marker in text for marker in markers)


async def _browser_executable_available():
    """Check whether Playwright's Chromium executable actually exists."""

    if not PLAYWRIGHT_AVAILABLE:
        return False

    try:
        async with async_playwright() as p:
            executable = p.chromium.executable_path
            return bool(
                executable
                and Path(executable).exists()
            )
    except Exception:
        return False


async def ensure_playwright_browser():
    """
    Install Chromium automatically when the Python package exists but the
    browser binary is missing, which is common on Pterodactyl/ACLClouds.
    """

    global PLAYWRIGHT_AVAILABLE

    if not PLAYWRIGHT_AVAILABLE:
        return False

    if await _browser_executable_available():
        return True

    if not BROWSER_AUTO_INSTALL:
        print("[BROWSER] Chromium must be installed by the Docker image.")
        return False

    print("[BROWSER] Chromium is missing. Installing it now...")

    try:
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "playwright",
                "install",
                "chromium",
            ],
            check=False,
            timeout=600,
        )

        if result.returncode != 0:
            print(
                "[BROWSER] Chromium installation failed with exit code",
                result.returncode,
            )
            return False

        if await _browser_executable_available():
            print("[BROWSER] Chromium installed successfully.")
            return True

        print(
            "[BROWSER] Chromium installation finished, but executable was not found."
        )
        return False

    except Exception as e:
        print("[BROWSER] Chromium installation error:", e)
        return False


def _is_obvious_asset(url):
    """Filter browser plumbing such as JS/CSS/images/fonts/analytics."""

    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        host = (parsed.hostname or "").lower()

        asset_extensions = (
            ".js", ".mjs", ".css", ".map",
            ".png", ".jpg", ".jpeg", ".gif", ".webp",
            ".svg", ".ico", ".bmp", ".avif",
            ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".mp3", ".wav", ".ogg",
        )

        if path.endswith(asset_extensions):
            return True

        if "googletagmanager.com" in host or "google-analytics.com" in host:
            return True

        if host == "www.w3.org" and path.startswith("/2000/svg"):
            return True

        if path.startswith("/_next/static/"):
            return True

        return False
    except Exception:
        return False


def _add_browser_url(links, seen, url):
    if not url:
        return

    url = str(url).strip()

    if not url.startswith(("http://", "https://")):
        return

    if _is_obvious_asset(url):
        return

    if url not in seen:
        seen.add(url)
        links.append(url)


async def _get_visible_interactive_elements(page):
    """Return visible anchors/buttons and their useful attributes."""

    try:
        return await page.locator(
            "a, button, input[type=button], input[type=submit], "
            "[role=button]"
        ).evaluate_all(
            """els => els.map((el, i) => ({
                index: i,
                tag: el.tagName.toLowerCase(),
                text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
                href: el.getAttribute('href'),
                onclick: el.getAttribute('onclick'),
                dataHref: el.getAttribute('data-href'),
                dataUrl: el.getAttribute('data-url'),
                dataLink: el.getAttribute('data-link'),
                dataDownload: el.getAttribute('data-download'),
                dataTarget: el.getAttribute('data-target'),
                formaction: el.getAttribute('formaction')
            }))"""
        )
    except Exception:
        return []


def _button_should_be_clicked(text):
    """Select buttons likely to expose a useful link, while avoiding destructive actions."""

    t = (text or "").strip().lower()

    if not t:
        return False

    # Never automatically activate account/destructive controls.
    blocked = (
        "logout", "log out", "delete", "remove", "cancel",
        "sign out", "signout", "unsubscribe", "close account",
        "checkout", "purchase", "pay now", "login", "log in",
        "sign in", "signup", "sign up",
    )

    if any(word in t for word in blocked):
        return False

    useful = (
        "download", "direct", "mkv", "mp4", "avi", "mov", "webm",
        "mirror", "server", "stream", "1080", "720", "480", "360",
        "2160", "1440", "generate", "file", "link", "get link",
    )

    return any(word in t for word in useful)


async def _click_useful_buttons(page, base_url, links, seen):
    """Inspect visible download/mirror/server controls and observe their results."""

    elements = await _get_visible_interactive_elements(page)
    items = [x for x in elements if _button_should_be_clicked(x.get("text", ""))]

    if not items:
        return

    print(f"[BROWSER] Found {len(items)} useful interactive element(s).")

    selector = "a, button, input[type=button], input[type=submit], [role=button]"

    for item in items:
        text = (item.get("text") or "").strip()
        index = item.get("index")

        try:
            await page.goto(
                base_url,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT_MS,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            await page.wait_for_timeout(1200)

            elements_now = page.locator(selector)
            count = await elements_now.count()
            element = None

            # Prefer matching visible text because hydration can change indexes.
            for j in range(count):
                candidate = elements_now.nth(j)
                try:
                    if not await candidate.is_visible():
                        continue
                    candidate_text = (
                        await candidate.inner_text()
                        if await candidate.evaluate("(el) => !!el.innerText")
                        else ""
                    )
                    candidate_text = (candidate_text or "").strip()
                    if text and candidate_text == text:
                        element = candidate
                        break
                except Exception:
                    continue

            if element is None and index is not None and index < count:
                candidate = elements_now.nth(index)
                if await candidate.is_visible():
                    element = candidate

            if element is None:
                continue

            print("[BUTTON] Inspecting:", text[:120])

            click_urls = set()
            popup = None
            download = None

            def on_request(request):
                u = request.url
                if u.startswith(("http://", "https://")):
                    click_urls.add(u)

            def on_response(response):
                try:
                    u = response.url
                    headers = {str(k).lower(): str(v).lower() for k, v in response.headers.items()}
                    ctype = headers.get("content-type", "")
                    disposition = headers.get("content-disposition", "")
                    if u.startswith(("http://", "https://")) and (
                        "attachment" in disposition
                        or any(t in ctype for t in (
                            "video/", "audio/", "application/octet-stream",
                            "application/x-matroska", "application/vnd.apple.mpegurl",
                        ))
                    ):
                        click_urls.add(u)
                except Exception:
                    pass

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                try:
                    async with page.expect_popup(timeout=3500) as popup_info:
                        try:
                            async with page.expect_download(timeout=3500) as download_info:
                                await element.click(timeout=7000, no_wait_after=True)
                                download = await download_info.value
                        except Exception:
                            await element.click(timeout=7000, no_wait_after=True)
                    popup = await popup_info.value
                except Exception:
                    try:
                        async with page.expect_download(timeout=3500) as download_info:
                            await element.click(timeout=7000, no_wait_after=True)
                            download = await download_info.value
                    except Exception:
                        await element.click(timeout=7000, no_wait_after=True)
            except Exception as e:
                print("[BUTTON] Click failed:", text[:100], e)

            await page.wait_for_timeout(2500)

            _add_browser_url(links, seen, page.url)

            if popup is not None:
                try:
                    await popup.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                _add_browser_url(links, seen, popup.url)
                try:
                    for u in extract_links_from_html(popup.url, await popup.content()):
                        _add_browser_url(links, seen, u)
                except Exception:
                    pass
                try:
                    await popup.close()
                except Exception:
                    pass

            if download is not None:
                try:
                    _add_browser_url(links, seen, download.url)
                except Exception:
                    pass

            for u in click_urls:
                _add_browser_url(links, seen, u)

            try:
                for u in extract_links_from_html(page.url, await page.content()):
                    _add_browser_url(links, seen, u)
            except Exception:
                pass

            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)

        except Exception as e:
            print("[BUTTON] Inspection error:", text[:100], e)
async def fetch_page_browser(url):
    """Render the page in Chromium and inspect the *rendered* application.

    This deliberately separates:
      * ordinary links
      * visible controls
      * API/network traffic
      * URLs produced by clicking a control

    A site's API URL is not automatically reported as a download URL.  We
    inspect the response body/headers and the browser result of the click.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright Python package is not installed.")

    if not await ensure_playwright_browser():
        raise RuntimeError(
            "Playwright Chromium is not installed. Install it in the Docker image."
        )

    captured = []
    captured_seen = set()
    response_records = []
    console_errors = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=True,
            accept_downloads=True,
            java_script_enabled=True,
        )

        # Hide the most obvious automation flag. This is not intended to bypass
        # authentication or access controls, only to make ordinary JS pages
        # behave more like a normal browser session.
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        page = await context.new_page()

        def add_network_url(u):
            if not u or not u.startswith(("http://", "https://")):
                return
            if u not in captured_seen:
                captured_seen.add(u)
                captured.append(u)

        def on_request(request):
            add_network_url(request.url)

        async def inspect_response(response):
            try:
                u = response.url
                if not u.startswith(("http://", "https://")):
                    return
                headers = await response.all_headers()
                ctype = (headers.get("content-type") or "").lower()
                # Keep metadata for API/file responses. We don't save media bodies.
                if "/api/" in u or "download" in u.lower() or "file" in u.lower():
                    response_records.append((response, headers, ctype))
            except Exception:
                pass

        def on_response(response):
            add_network_url(response.url)
            asyncio.create_task(inspect_response(response))

        def on_console(msg):
            if msg.type == "error":
                console_errors.append(msg.text[:500])

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("console", on_console)

        main_response = None
        try:
            main_response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT_MS,
            )
        except Exception as e:
            print("[BROWSER GOTO ERROR]", url, e)

        # Let the application hydrate. Some Next/React pages need substantially
        # longer than networkidle because they keep a connection open.
        for wait_ms in (2500, 3000):
            try:
                await page.wait_for_load_state("networkidle", timeout=wait_ms)
            except Exception:
                pass
            await page.wait_for_timeout(wait_ms)

        # If the page visibly says Retry, click Retry once. This is useful for
        # apps whose first client-side API request occasionally fails. We do not
        # click login/payment/delete/etc. controls automatically.
        try:
            retry = page.get_by_role("button", name=re.compile(r"^retry$", re.I))
            if await retry.count() and await retry.first.is_visible():
                print("[BROWSER] Page exposed Retry; clicking it once...")
                await retry.first.click(timeout=5000, no_wait_after=True)
                await page.wait_for_timeout(4000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
        except Exception as e:
            print("[BROWSER] Retry inspection failed:", e)

        # Scroll through the page so lazy-rendered controls have a chance to
        # appear. This does not download media.
        try:
            await page.evaluate("""
                async () => {
                    const step = Math.max(300, Math.floor(innerHeight * 0.75));
                    for (let y = 0; y < document.body.scrollHeight; y += step) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 150));
                    }
                    window.scrollTo(0, 0);
                }
            """)
        except Exception:
            pass

        await page.wait_for_timeout(1200)

        current_url = page.url
        rendered_html = await page.content()
        try:
            title = await page.title()
        except Exception:
            title = ""

        # Give pending response-body tasks a moment to finish.
        await page.wait_for_timeout(800)

        links = []
        seen = set()

        # Only ordinary rendered links initially. Do not flood the user's list
        # with JS/CSS/analytics.
        for u in extract_links_from_html(current_url, rendered_html):
            if not _is_obvious_asset(u):
                _add_browser_url(links, seen, u)

        # Extract visible controls from the real rendered page.
        interactive = await _get_interactive_elements_v2(page)
        interactive = [
            x for x in interactive
            if x.get("visible") and (x.get("text") or x.get("href"))
        ]

        # Also inspect same-origin iframes. A download button inside an iframe
        # will never be found by querying only the top-level document.
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            try:
                frame_items = await frame.locator(
                    "a, button, input[type=button], input[type=submit], "
                    "[role=button], [role=link]"
                ).evaluate_all(
                    """els => els.map((el,index) => ({
                        index,
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\\s+/g,' ').trim(),
                        href: el.getAttribute('href'),
                        dataHref: el.getAttribute('data-href'),
                        dataUrl: el.getAttribute('data-url'),
                        dataLink: el.getAttribute('data-link'),
                        dataDownload: el.getAttribute('data-download'),
                        formaction: el.getAttribute('formaction'),
                        visible: (() => { const r=el.getBoundingClientRect(); const s=getComputedStyle(el); return !!(r.width&&r.height)&&s.display!=='none'&&s.visibility!=='hidden'; })()
                    }))"""
                )
                for item in frame_items:
                    if item.get("visible") and (item.get("text") or item.get("href")):
                        item["frameUrl"] = frame.url
                        interactive.append(item)
            except Exception:
                continue

        # Build a small list of controls that actually look like file/link
        # controls. Retry has already been handled separately.
        useful_controls = [
            x for x in interactive
            if _button_score(x.get("text", ""))
        ][:30]

        for item in useful_controls:
            print("[BROWSER] Inspecting control:", item.get("text", "")[:120])
            for u in await _inspect_button_v2(page, item, current_url):
                _add_browser_url(links, seen, u)

        # API response bodies can contain the generated URL even when the API
        # request itself is not the download URL.
        for response, headers, ctype in list(response_records):
            try:
                u = response.url
                if _looks_like_download_response(u, headers):
                    _add_browser_url(links, seen, u)
                if "json" in ctype or "text" in ctype or "/api/" in u:
                    body = await response.text()
                    for found in _extract_urls_from_text(u, body):
                        _add_browser_url(links, seen, found)
            except Exception:
                continue

        # Keep the browser's final navigation URL if it is different.
        _add_browser_url(links, seen, current_url)

        # Store diagnostic details on PageResult so /debug can explain why a
        # page had no download control rather than silently falling back.
        result = PageResult(
            url=current_url,
            text=rendered_html,
            status_code=(main_response.status if main_response is not None else 200),
            title=title,
            interactive=interactive,
        )
        result.browser_diagnostics = {
            "console_errors": console_errors[:20],
            "network_count": len(captured),
            "network_urls": captured[:200],
        }

        await browser.close()
        return result, links

# ============================================================
# FETCH PAGE
# ============================================================

from curl_cffi import requests as cffi_requests


def fetch_page_http(url):
    """Fetch a page without JavaScript."""

    response = cffi_requests.get(
        url,
        impersonate="chrome",
        timeout=25,
        allow_redirects=True,
    )

    response.raise_for_status()

    links = extract_links_from_html(
        response.url,
        response.text,
    )

    return (
        PageResult(
            url=response.url,
            text=response.text,
            status_code=response.status_code,
            title="",
        ),
        links,
    )


async def fetch_page(session, url):
    """
    Generic fetcher.

    Simple pages use HTTP. Pages that look client-rendered, have too few
    useful links, or return an HTTP error are rendered with Chromium.
    No website/domain needs to be configured in advance.
    """

    try:
        response, links = fetch_page_http(url)

        # A page with only the site shell/home/login/assets is not considered
        # successfully extracted. Give the browser a chance to render it.
        useful_links = [
            link for link in links
            if not _is_obvious_asset(link)
        ]

        if (
            PLAYWRIGHT_AVAILABLE
            and (
                page_looks_dynamic(response.text)
                or len(useful_links) <= 3
            )
        ):
            print(
                "[BROWSER] Rendering dynamic/low-link page:",
                url,
            )

            try:
                return await fetch_page_browser(
                    response.url,
                )
            except Exception as browser_error:
                print(
                    "[BROWSER ERROR]",
                    browser_error,
                )

        return (
            response,
            links,
        )

    except Exception as http_error:
        print(
            "[HTTP ERROR]",
            url,
            http_error,
        )

        if not PLAYWRIGHT_AVAILABLE:
            raise

        print(
            "[BROWSER] HTTP failed, rendering:",
            url,
        )

        return await fetch_page_browser(url)

# ============================================================

# GET STEP TARGETS

# Supports both old and new JSON formats

# ============================================================

def get_step_targets(
step
):

    # --------------------------------------------------------
    # OLD FORMAT
    #
    # "gamerxyt.com"
    # --------------------------------------------------------

    if isinstance(
        step,
        str
    ):

        return [
            clean_domain(step)
        ]

    # --------------------------------------------------------
    # NEW FORMAT
    #
    # {
    #     "targets": [
    #         "gamerxyt.com",
    #         "gamerxyt.net"
    #     ]
    # }
    # --------------------------------------------------------

    if isinstance(
        step,
        dict
    ):

        targets = step.get(
            "targets",
            []
        )

        if isinstance(
            targets,
            str
        ):

            targets = [
                targets
            ]

        return [
            clean_domain(x)
            for x in targets
            if str(x).strip()
        ]

    return []

# ============================================================

# FIND TARGET LINK

# ============================================================

def find_target_link(
links,
targets
):

    for link in links:

        for target in targets:

            if domain_matches(
                link,
                target
            ):

                return link

    return None

# ============================================================

# FINAL URL EXTRACTION

# ============================================================

def extract_final_url(
url,
final_rule
):

    if not final_rule:
        return url

    if not isinstance(
        final_rule,
        dict
    ):

        return url

    rule_type = final_rule.get(
        "type"
    )

    # --------------------------------------------------------
    # Current URL
    # --------------------------------------------------------

    if rule_type == "current_url":

        return url

    # --------------------------------------------------------
    # Query parameter
    #
    # Example:
    #
    # ?link=https://...
    #
    # --------------------------------------------------------

    if rule_type == "query_parameter":

        parameter = final_rule.get(
            "parameter"
        )

        if not parameter:
            return url

        parsed = urlparse(
            url
        )

        params = parse_qs(
            parsed.query
        )

        values = params.get(
            parameter
        )

        if values:

            return values[0]

        return url

    # --------------------------------------------------------
    # Final domain
    # --------------------------------------------------------

    if rule_type == "domain":

        target = final_rule.get(
            "domain"
        )

        if target and domain_matches(
            url,
            target
        ):

            return url

        return url

    return url

# ============================================================

# RESOLVE SAVED ROUTE

# ============================================================

async def resolve_route(
session,
start_url,
route
):

    current_url = start_url

    history = []

    steps = route.get(
        "steps",
        []
    )

    # --------------------------------------------------------
    # STEP LOOP
    # --------------------------------------------------------

    for step_number, step in enumerate(
        steps,
        start=1
    ):

        targets = get_step_targets(
            step
        )

        if not targets:

            raise RuntimeError(
                f"Step {step_number} "
                f"has no target domains."
            )

        print(
            f"[STEP {step_number}] "
            f"Targets: {targets}"
        )

        # ----------------------------------------------------
        # Fetch current page
        # ----------------------------------------------------

        response, links = await fetch_page(
            session,
            current_url
        )

        print(
            f"[STEP {step_number}] "
            f"Current URL: {response.url}"
        )

        selected = None

        # ----------------------------------------------------
        # Check redirected URL first
        # ----------------------------------------------------

        for target in targets:

            if domain_matches(
                response.url,
                target
            ):

                selected = response.url

                print(
                    f"[STEP {step_number}] "
                    f"Redirect match: "
                    f"{selected}"
                )

                break

        # ----------------------------------------------------
        # Otherwise search links
        # ----------------------------------------------------

        if selected is None:

            selected = find_target_link(
                links,
                targets
            )

            if selected:

                print(
                    f"[STEP {step_number}] "
                    f"Link match: "
                    f"{selected}"
                )

        # ----------------------------------------------------
        # Nothing found
        # ----------------------------------------------------

        if selected is None:

            raise RuntimeError(
                f"Step {step_number}: "
                f"none of these domains were found: "
                f"{', '.join(targets)}"
            )

        history.append({
            "step": step_number,
            "targets": targets,
            "input": current_url,
            "selected": selected
        })

        current_url = selected

    # --------------------------------------------------------
    # FINAL PAGE
    # --------------------------------------------------------

    print(
        "[FINAL] Fetching:",
        current_url
    )

    response, links = await fetch_page(
        session,
        current_url
    )

    print(
        "[FINAL] Response URL:",
        response.url
    )

    # --------------------------------------------------------
    # FINAL EXTRACTION
    # --------------------------------------------------------

    final_url = extract_final_url(
        response.url,
        route.get("final")
    )

    return final_url, history

# ============================================================

# DEBUG PAGE

# ============================================================

def format_debug_links(links, interactive=None):
    interactive = interactive or []
    shown = links[:MAX_DEBUG_LINKS]

    lines = [
        f"🔗 <b>Useful URLs found: {len(links)}</b>",
        "",
    ]

    if interactive:
        lines.extend(["🖱 <b>Visible links / buttons</b>", ""])
        for item in interactive[:60]:
            text = (item.get("text") or "").strip()
            if not text:
                text = "(no visible text)"
            if len(text) > 120:
                text = text[:117] + "..."
            href = item.get("href") or item.get("dataHref") or item.get("dataUrl") or item.get("dataDownload")
            lines.append(f"• <b>{html.escape(text)}</b>")
            if href:
                u = normalize_link(item.get("pageUrl") or "https://example.com/", href)
                if u:
                    lines.append(f"  <code>{html.escape(u)}</code>")
                else:
                    lines.append(f"  <code>{html.escape(str(href)[:500])}</code>")
            else:
                lines.append("  <i>JavaScript button: inspected for resulting URL</i>")
        lines.extend(["", "🔗 <b>URLs you can use in rules</b>", ""])

    if not shown:
        lines.append("❌ No usable HTTP(S) URLs found.")
    else:
        for number, link in enumerate(shown, start=1):
            display = link if len(link) <= 700 else link[:697] + "..."
            lines.append(f"<b>{number}.</b> <code>{html.escape(display)}</code>")

    if len(links) > MAX_DEBUG_LINKS:
        lines.extend(["", f"⚠️ Showing first {MAX_DEBUG_LINKS} URLs."])

    lines.extend([
        "",
        "👉 Reply with the <b>number</b> of the URL you want to debug.",
        "",
        "Send /cancel to stop.",
    ])
    return "\n".join(lines)


async def debug_page(update, context, url):
    status = await update.message.reply_text(
        "🔎 Opening page in Chromium and inspecting the live page...",
        disable_web_page_preview=True,
    )

    try:
        session = context.user_data.get("session")
        if session is None:
            session = requests.Session()
            context.user_data["session"] = session

        response, links = await fetch_page_browser(url)

        context.user_data["debug_links"] = links
        context.user_data["debug_current_url"] = response.url

        interactive = response.interactive or []
        # Add the page URL to each item for safe display/normalization.
        for item in interactive:
            item["pageUrl"] = response.url
        context.user_data["debug_interactive"] = interactive

        title = response.title or "Unknown"
        message = (
            "✅ <b>Browser page inspected</b>\n\n"
            f"<b>Status:</b> {response.status_code}\n\n"
            f"<b>Final URL:</b>\n<code>{html.escape(response.url)}</code>\n\n"
            f"<b>Title:</b>\n{html.escape(title[:500])}\n\n"
        )
        message += format_debug_links(links, interactive)

        await status.edit_text(
            message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    except Exception as e:
        print("[DEBUG ERROR]", repr(e))
        await status.edit_text(
            "❌ <b>Browser debug failed</b>\n\n"
            f"<code>{html.escape(str(e)[:1500])}</code>",
            parse_mode="HTML",
        )


# AUTOMATIC RESOLVE

# ============================================================

async def automatic_resolve(
update,
context,
urls
):
    routes = load_routes()

    if not routes:
        await update.message.reply_text(
            "❌ No routes are configured.\n\nUse /addroute first."
        )
        return

    if len(urls) > MAX_URLS_PER_MESSAGE:
        await update.message.reply_text(
            f"⚠️ Maximum {MAX_URLS_PER_MESSAGE} URLs per message."
        )
        return

    # Keep the successful result message IDs in order for /i<number>.
    result_message_ids = []

    for index, start_url in enumerate(urls, start=1):
        route_name = None
        route = None

        # ----------------------------------------------------
        # Find a route for the input URL.
        # Old HubCloud domains use the current HubCloud route.
        # ----------------------------------------------------
        for name, candidate in routes.items():
            if not isinstance(candidate, dict):
                continue

            main_domain = candidate.get("main_domain")
            main_domains = candidate.get("main_domains", [])
            aliases = candidate.get("aliases", [])

            possible_domains = []

            if main_domain:
                possible_domains.append(main_domain)

            if isinstance(main_domains, str):
                main_domains = [main_domains]

            if isinstance(main_domains, list):
                possible_domains.extend(main_domains)

            if isinstance(aliases, str):
                aliases = [aliases]

            if isinstance(aliases, list):
                possible_domains.extend(aliases)

            for domain in possible_domains:
                if domain_matches(start_url, domain):
                    route_name = name
                    route = candidate
                    break

            if route:
                break

            hostname = (urlparse(start_url).hostname or "").lower()

            if (
                hostname.startswith("hubcloud.")
                and main_domain
                and clean_domain(main_domain).startswith("hubcloud.")
            ):
                route_name = name
                route = candidate
                break

            if (
                "gdflix" in hostname
                and main_domain
                and "gdflix" in clean_domain(main_domain)
            ):
                route_name = name
                route = candidate
                break

        if route is None:
            await update.message.reply_text(
                f"❌ <b>Link {index}</b>\n\n"
                f"No matching route for "
                f"<code>{html.escape(urlparse(start_url).netloc or 'unknown')}</code>",
                parse_mode="HTML"
            )
            continue

        try:
            session = requests.Session()

            normalized_url = normalize_start_url(
                start_url,
                route
            )

            final_url, history = await resolve_route(
                session,
                normalized_url,
                route
            )

            if not final_url:
                raise RuntimeError(
                    "Resolver returned an empty URL."
                )

            # ------------------------------------------------
            # Create a Spacebin paste containing ONLY this
            # final URL. Nothing else.
            # ------------------------------------------------
            individual_spacebin = upload_to_spacebin(
                final_url
            )

            buttons = [
                InlineKeyboardButton(
                    "🔗 Open Link",
                    url=final_url
                )
            ]

            if individual_spacebin:
                buttons.append(
                    InlineKeyboardButton(
                        "📄 Spacebin",
                        url=individual_spacebin
                    )
                )

            keyboard = InlineKeyboardMarkup([
                buttons
            ])

            # ------------------------------------------------
            # IMPORTANT:
            # Send every result as a separate Telegram message.
            # The long URL is never printed in the message body.
            # ------------------------------------------------
            sent = await update.message.reply_text(
                f"✅ <b>Link {index} resolved</b>",
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )

            # Save this Telegram message -> final URL mapping.
            RESULT_MESSAGES[sent.message_id] = {
                "chat_id": update.effective_chat.id,
                "final_url": final_url,
                "source_message_id": update.message.message_id,
                "created_at": __import__("time").time()
            }

            result_message_ids.append(
                sent.message_id
            )

        except Exception as e:
            await update.message.reply_text(
                f"❌ <b>Link {index} failed</b>\n\n"
                f"<code>{html.escape(str(e)[:1000])}</code>",
                parse_mode="HTML"
            )

    # Save this batch in the user's chat state as a convenience.
    context.user_data["last_result_message_ids"] = result_message_ids

# ============================================================

# /I\<number> - COMPILE RESULT MESSAGES

# ============================================================

async def compile_links_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):
    """
    Usage:

        Reply to Link 1 with /i3

    This collects the replied-to result message plus the next
    2 result messages from the same batch.

    Spacebin contains ONLY final URLs, one per line.
    """

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ Reply to a resolved-link message with /i<number>.\n\n"
            "Example: reply to Link 1 with /i3"
        )
        return

    command = update.message.text.strip()

    match = re.fullmatch(
        r"/i(\d+)(?:@\w+)?",
        command,
        re.IGNORECASE
    )

    if not match:
        await update.message.reply_text(
            "❌ Use the format /i<number>.\n\n"
            "Example: /i3"
        )
        return

    count = int(match.group(1))

    if count < 1:
        await update.message.reply_text(
            "❌ Number must be at least 1."
        )
        return

    if count > MAX_URLS_PER_MESSAGE:
        await update.message.reply_text(
            f"❌ Maximum {MAX_URLS_PER_MESSAGE} links can be compiled."
        )
        return

    replied_message_id = (
        update.message.reply_to_message.message_id
    )

    replied_result = RESULT_MESSAGES.get(
        replied_message_id
    )

    if not replied_result:
        await update.message.reply_text(
            "❌ I can't find the final URL for the message "
            "you replied to.\n\n"
            "Make sure you reply directly to a resolved-link "
            "message from the current bot session."
        )
        return

    chat_id = update.effective_chat.id

    # --------------------------------------------------------
    # Find result messages for this chat, ordered by message ID.
    # --------------------------------------------------------
    candidates = []

    for message_id, result in RESULT_MESSAGES.items():
        if result.get("chat_id") == chat_id:
            candidates.append(
                (message_id, result)
            )

    candidates.sort(
        key=lambda item: item[0]
    )

    ids = [
        message_id
        for message_id, result in candidates
    ]

    try:
        start_position = ids.index(
            replied_message_id
        )
    except ValueError:
        await update.message.reply_text(
            "❌ This result message is no longer available."
        )
        return

    selected = candidates[
        start_position:start_position + count
    ]

    if len(selected) < count:
        await update.message.reply_text(
            f"❌ Only {len(selected)} result link(s) are "
            f"available after the message you replied to.\n\n"
            f"You requested {count}."
        )
        return

    # --------------------------------------------------------
    # ONLY final URLs go into Spacebin.
    # One URL per line.
    # --------------------------------------------------------
    final_urls = []

    for message_id, result in selected:
        final_url = result.get("final_url")

        if final_url:
            final_urls.append(
                final_url
            )

    if not final_urls:
        await update.message.reply_text(
            "❌ No final URLs found."
        )
        return

    paste_content = "\n".join(
        final_urls
    )

    spacebin_url = upload_to_spacebin(
        paste_content
    )

    if not spacebin_url:
        await update.message.reply_text(
            "❌ Failed to create the Spacebin paste."
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📄 Open compiled Spacebin",
                url=spacebin_url
            )
        ]
    ])

    await update.message.reply_text(
        f"📦 <b>{len(final_urls)} final link(s) compiled</b>",
        parse_mode="HTML",
        reply_markup=keyboard
    )

# ============================================================

# /START

# ============================================================

async def start_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    context.user_data[
        "session"
    ] = requests.Session()

    await update.message.reply_text(
        "👋 <b>Multi-Step Link Bot</b>\n\n"
        "🐞 /debug — debug a link\n"
        "➕ /addroute — create route\n"
        "✏️ /editroute — edit route\n"
        "📂 /routes — list routes\n"
        "🗑 /deleteroute — delete route\n"
        "❌ /cancel — cancel\n\n"
        "💡 <b>Normal mode:</b>\n"
        "Just send URL(s), one per line.\n"
        "The bot automatically resolves them.",
        parse_mode="HTML"
    )

# ============================================================

# /CANCEL

# ============================================================

async def cancel_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    context.user_data[
        "session"
    ] = requests.Session()

    await update.message.reply_text(
        "🛑 Cancelled.\n\n"
        "You are back in normal mode.\n"
        "Send a URL to resolve it."
    )

# ============================================================

# /DEBUG

# ============================================================

async def debug_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    context.user_data[
        "session"
    ] = requests.Session()

    context.user_data[
        "mode"
    ] = "debug_url"

    await update.message.reply_text(
        "🐞 <b>Debug mode</b>\n\n"
        "Send the starting URL.",
        parse_mode="HTML"
    )

# ============================================================

# /ADDROUTE

# ============================================================

async def addroute_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    context.user_data[
        "session"
    ] = requests.Session()

    context.user_data[
        "new_route"
    ] = {
        "steps": [],
        "final": None
    }

    context.user_data[
        "mode"
    ] = "route_name"

    await update.message.reply_text(
        "➕ <b>Create route</b>\n\n"
        "Send a route name.\n\n"
        "Example:\n"
        "<code>hubcloud</code>",
        parse_mode="HTML"
    )

# ============================================================

# /ADDSTEP

# ============================================================

async def addstep_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    route = context.user_data.get(
        "new_route"
    )

    if not route:

        await update.message.reply_text(
            "❌ You are not creating a route.\n\n"
            "Use /addroute first."
        )

        return

    step_number = (
        len(route["steps"]) + 1
    )

    context.user_data[
        "mode"
    ] = "route_step"

    await update.message.reply_text(
        f"➕ <b>Step {step_number}</b>\n\n"
        "Send target domain(s).\n\n"
        "Multiple alternatives:\n"
        "<code>gamerxyt.com, gamerxyt.net</code>",
        parse_mode="HTML"
    )

# ============================================================

# /ENDSTEP

# ============================================================

async def endstep_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    route = context.user_data.get(
        "new_route"
    )

    if not route:

        await update.message.reply_text(
            "❌ No route is being created."
        )

        return

    if not route["steps"]:

        await update.message.reply_text(
            "❌ Add at least one step first."
        )

        return

    context.user_data[
        "mode"
    ] = "route_final_type"

    await update.message.reply_text(
        "🏁 <b>Final URL rule</b>\n\n"
        "<b>1</b> — Current URL\n"
        "<b>2</b> — Query parameter\n"
        "<b>3</b> — Final domain\n\n"
        "Reply with 1, 2 or 3.",
        parse_mode="HTML"
    )

# ============================================================

# /ROUTES

# ============================================================

async def routes_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    routes = load_routes()

    if not routes:

        await update.message.reply_text(
            "📂 No routes saved.\n\n"
            "Use /addroute."
        )

        return

    lines = [
        "📂 <b>Saved routes</b>",
        ""
    ]

    for name, route in routes.items():

        if not isinstance(
            route,
            dict
        ):
            continue

        lines.append(
            f"🔹 <b>{html.escape(name)}</b>"
        )

        main = route.get(
            "main_domain",
            ""
        )

        if main:

            lines.append(
                "Main: "
                f"<code>{html.escape(str(main))}</code>"
            )

        aliases = route.get(
            "aliases",
            []
        )

        if aliases:

            if isinstance(
                aliases,
                str
            ):
                aliases = [
                    aliases
                ]

            lines.append(
                "Aliases: "
                f"<code>{html.escape(', '.join(aliases))}</code>"
            )

        for number, step in enumerate(
            route.get(
                "steps",
                []
            ),
            start=1
        ):

            targets = get_step_targets(
                step
            )

            lines.append(
                f"Step {number}: "
                f"<code>"
                f"{html.escape(', '.join(targets))}"
                f"</code>"
            )

        final = route.get(
            "final"
        )

        if final:

            lines.append(
                "Final: "
                f"<code>"
                f"{html.escape(str(final.get('type')))}"
                f"</code>"
            )

        lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )

# ============================================================

# SAVE NEW ROUTE

# ============================================================

async def save_new_route(
update,
context
):

    route = context.user_data.get(
        "new_route"
    )

    if not route:
        return

    name = route.get(
        "name"
    )

    routes = load_routes()

    routes[name] = {
        "main_domain": route.get(
            "main_domain"
        ),
        "steps": route.get(
            "steps",
            []
        ),
        "final": route.get(
            "final"
        )
    }

    save_routes(
        routes
    )

    lines = [
        "✅ <b>Route saved!</b>",
        "",
        f"<b>Name:</b> "
        f"{html.escape(name)}",
        f"<b>Main:</b> "
        f"<code>{html.escape(str(route.get('main_domain')))}</code>",
        "",
        "<b>Steps:</b>"
    ]

    for number, step in enumerate(
        route["steps"],
        start=1
    ):

        targets = get_step_targets(
            step
        )

        lines.append(
            f"{number}. "
            f"<code>"
            f"{html.escape(', '.join(targets))}"
            f"</code>"
        )

    final = route.get(
        "final"
    )

    if final:

        lines.extend([
            "",
            "<b>Final rule:</b>",
            f"<code>{html.escape(json.dumps(final))}</code>"
        ])

    lines.extend([
        "",
        "🚀 You can now simply send URLs."
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )

    context.user_data.clear()

    context.user_data[
        "session"
    ] = requests.Session()

# ============================================================

# /EDITROUTE

# ============================================================

async def editroute_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    routes = load_routes()

    if not routes:

        await update.message.reply_text(
            "❌ No routes saved."
        )

        return

    names = list(
        routes.keys()
    )

    context.user_data.clear()

    context.user_data[
        "edit_routes"
    ] = names

    context.user_data[
        "mode"
    ] = "edit_route_select"

    lines = [
        "✏️ <b>Select route</b>",
        ""
    ]

    for number, name in enumerate(
        names,
        start=1
    ):

        lines.append(
            f"<b>{number}.</b> "
            f"{html.escape(name)}"
        )

    lines.extend([
        "",
        "Reply with the number."
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )

# ============================================================

# EDIT MENU

# ============================================================

async def show_edit_menu(
update,
context
):

    route_name = context.user_data.get(
        "edit_route"
    )

    routes = load_routes()

    route = routes.get(
        route_name
    )

    if not route:

        await update.message.reply_text(
            "❌ Route not found."
        )

        return

    lines = [
        f"✏️ <b>Editing:</b> "
        f"{html.escape(route_name)}",
        ""
    ]

    for number, step in enumerate(
        route.get("steps", []),
        start=1
    ):

        targets = get_step_targets(
            step
        )

        lines.append(
            f"<b>Step {number}:</b>\n"
            f"<code>"
            f"{html.escape(', '.join(targets))}"
            f"</code>"
        )

    lines.extend([
        "",
        "<b>Commands:</b>",
        "/addstep — add step",
        "/addtarget — add domain to step",
        "/deletetarget — remove domain",
        "/deletestep — remove step",
        "/editfinal — change final rule",
        "/done — finish editing"
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )

# ============================================================

# /ADDTARGET

# ============================================================

async def addtarget_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    if not context.user_data.get(
        "edit_route"
    ):

        await update.message.reply_text(
            "❌ Use /editroute first."
        )

        return

    context.user_data[
        "mode"
    ] = "edit_addtarget_step"

    await update.message.reply_text(
        "➕ Send the step number.\n\n"
        "Example: <code>2</code>",
        parse_mode="HTML"
    )

# ============================================================

# /DELETETARGET

# ============================================================

async def deletetarget_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    if not context.user_data.get(
        "edit_route"
    ):

        await update.message.reply_text(
            "❌ Use /editroute first."
        )

        return

    context.user_data[
        "mode"
    ] = "edit_delete_target_step"

    await update.message.reply_text(
        "🗑 Send the step number.",
        parse_mode="HTML"
    )

# ============================================================

# /DELETESTEP

# ============================================================

async def deletestep_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    if not context.user_data.get(
        "edit_route"
    ):

        await update.message.reply_text(
            "❌ Use /editroute first."
        )

        return

    context.user_data[
        "mode"
    ] = "edit_delete_step"

    await update.message.reply_text(
        "🗑 Send the step number to delete."
    )

# ============================================================

# /EDITFINAL

# ============================================================

async def editfinal_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    if not context.user_data.get(
        "edit_route"
    ):

        await update.message.reply_text(
            "❌ Use /editroute first."
        )

        return

    context.user_data[
        "mode"
    ] = "edit_final_type"

    await update.message.reply_text(
        "🏁 <b>Final rule</b>\n\n"
        "<b>1</b> — Current URL\n"
        "<b>2</b> — Query parameter\n"
        "<b>3</b> — Domain\n\n"
        "Reply with 1, 2 or 3.",
        parse_mode="HTML"
    )

# ============================================================

# /DONE

# ============================================================

async def done_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    if context.user_data.get(
        "edit_route"
    ):

        context.user_data.clear()

        context.user_data[
            "session"
        ] = requests.Session()

        await update.message.reply_text(
            "✅ Done editing.\n\n"
            "Back to normal mode."
        )

    else:

        await update.message.reply_text(
            "Nothing is being edited."
        )

# ============================================================

# /DELETEROUTE

# ============================================================

async def deleteroute_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    routes = load_routes()

    if not routes:

        await update.message.reply_text(
            "No routes saved."
        )

        return

    names = list(
        routes.keys()
    )

    context.user_data.clear()

    context.user_data[
        "delete_routes"
    ] = names

    context.user_data[
        "mode"
    ] = "delete_route"

    lines = [
        "🗑 <b>Delete route</b>",
        ""
    ]

    for number, name in enumerate(
        names,
        start=1
    ):

        lines.append(
            f"<b>{number}.</b> "
            f"{html.escape(name)}"
        )

    lines.extend([
        "",
        "Reply with the number."
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )

# ============================================================

# HANDLE TEXT

# ============================================================

async def handle_text(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text.strip()

    mode = context.user_data.get(
        "mode"
    )

    # ========================================================
    # NORMAL MODE
    #
    # Any plain URL(s) automatically resolve.
    # ========================================================

    if mode is None:

        urls = [
            line.strip()
            for line in text.splitlines()
            if valid_url(
                line.strip()
            )
        ]

        if urls:

            await automatic_resolve(
                update,
                context,
                urls
            )

            return

        await update.message.reply_text(
            "Send a URL to resolve it.\n\n"
            "Use /start for commands."
        )

        return

    # ========================================================
    # DEBUG URL
    # ========================================================

    if mode == "debug_url":

        if not valid_url(text):

            await update.message.reply_text(
                "❌ Send a valid URL."
            )

            return

        context.user_data[
            "mode"
        ] = "debug_select"

        await debug_page(
            update,
            context,
            text
        )

        return

    # ========================================================
    # DEBUG SELECT
    # ========================================================

    if mode == "debug_select":

        links = context.user_data.get(
            "debug_links",
            []
        )

        try:

            number = int(text)

        except ValueError:

            await update.message.reply_text(
                "❌ Reply with a link number."
            )

            return

        if number < 1 or number > len(links):

            await update.message.reply_text(
                f"❌ Choose a number from "
                f"1 to {len(links)}."
            )

            return

        selected = links[
            number - 1
        ]

        await update.message.reply_text(
            "➡️ <b>Selected:</b>\n\n"
            f"<code>{html.escape(selected)}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        await debug_page(
            update,
            context,
            selected
        )

        return

    # ========================================================
    # ROUTE NAME
    # ========================================================

    if mode == "route_name":

        name = text.lower().strip()

        if not name:

            await update.message.reply_text(
                "❌ Enter a route name."
            )

            return

        routes = load_routes()

        if name in routes:

            await update.message.reply_text(
                "❌ That route already exists."
            )

            return

        context.user_data[
            "new_route"
        ]["name"] = name

        context.user_data[
            "mode"
        ] = "route_main"

        await update.message.reply_text(
            "Send the current main domain.\n\n"
            "Example:\n"
            "<code>hubcloud.cx</code>",
            parse_mode="HTML"
        )

        return

    # ========================================================
    # ROUTE MAIN DOMAIN
    # ========================================================

    if mode == "route_main":

        route = context.user_data[
            "new_route"
        ]

        route[
            "main_domain"
        ] = clean_domain(
            text
        )

        context.user_data[
            "mode"
        ] = "route_menu"

        await update.message.reply_text(
            "✅ Main domain saved.\n\n"
            "Now use /addstep to add Step 1.",
            parse_mode="HTML"
        )

        return

    # ========================================================
    # ROUTE STEP
    # ========================================================

    if mode == "route_step":

        route = context.user_data[
            "new_route"
        ]

        raw_domains = text.split(
            ","
        )

        targets = []

        for domain in raw_domains:

            domain = clean_domain(
                domain
            )

            if (
                domain
                and domain not in targets
            ):

                targets.append(
                    domain
                )

        if not targets:

            await update.message.reply_text(
                "❌ No valid domains."
            )

            return

        route[
            "steps"
        ].append({
            "targets": targets
        })

        step_number = len(
            route["steps"]
        )

        context.user_data[
            "mode"
        ] = "route_menu"

        await update.message.reply_text(
            f"✅ <b>Step {step_number} added</b>\n\n"
            f"<code>"
            f"{html.escape(', '.join(targets))}"
            f"</code>\n\n"
            "Use /addstep for another step.\n"
            "Use /endstep when finished.",
            parse_mode="HTML"
        )

        return

    # ========================================================
    # FINAL TYPE
    # ========================================================

    if mode == "route_final_type":

        route = context.user_data[
            "new_route"
        ]

        if text == "1":

            route[
                "final"
            ] = {
                "type": "current_url"
            }

            await save_new_route(
                update,
                context
            )

            return

        if text == "2":

            context.user_data[
                "mode"
            ] = "route_parameter"

            await update.message.reply_text(
                "Send the query parameter.\n\n"
                "Example:\n"
                "<code>link</code>",
                parse_mode="HTML"
            )

            return

        if text == "3":

            context.user_data[
                "mode"
            ] = "route_final_domain"

            await update.message.reply_text(
                "Send the final domain.\n\n"
                "Example:\n"
                "<code>googleusercontent.com</code>",
                parse_mode="HTML"
            )

            return

        await update.message.reply_text(
            "Reply with 1, 2 or 3."
        )

        return

    # ========================================================
    # FINAL PARAMETER
    # ========================================================

    if mode == "route_parameter":

        route = context.user_data[
            "new_route"
        ]

        route[
            "final"
        ] = {
            "type": "query_parameter",
            "parameter": text.strip()
        }

        await save_new_route(
            update,
            context
        )

        return

    # ========================================================
    # FINAL DOMAIN
    # ========================================================

    if mode == "route_final_domain":

        route = context.user_data[
            "new_route"
        ]

        route[
            "final"
        ] = {
            "type": "domain",
            "domain": clean_domain(
                text
            )
        }

        await save_new_route(
            update,
            context
        )

        return

    # ========================================================
    # EDIT ROUTE SELECT
    # ========================================================

    if mode == "edit_route_select":

        names = context.user_data[
            "edit_routes"
        ]

        try:

            number = int(text)

        except ValueError:

            await update.message.reply_text(
                "Send a number."
            )

            return

        if number < 1 or number > len(names):

            await update.message.reply_text(
                "Invalid route number."
            )

            return

        context.user_data[
            "edit_route"
        ] = names[
            number - 1
        ]

        context.user_data[
            "mode"
        ] = "edit_menu"

        await show_edit_menu(
            update,
            context
        )

        return

    # ========================================================
    # EDIT ADD TARGET: STEP
    # ========================================================

    if mode == "edit_addtarget_step":

        try:

            step_number = int(
                text
            )

        except ValueError:

            await update.message.reply_text(
                "Send a valid step number."
            )

            return

        context.user_data[
            "edit_step"
        ] = step_number

        context.user_data[
            "mode"
        ] = "edit_addtarget_domain"

        await update.message.reply_text(
            "Send the new domain(s).\n\n"
            "Multiple domains separated by commas."
        )

        return

    # ========================================================
    # EDIT ADD TARGET: DOMAIN
    # ========================================================

    if mode == "edit_addtarget_domain":

        route_name = context.user_data[
            "edit_route"
        ]

        step_number = context.user_data[
            "edit_step"
        ]

        routes = load_routes()

        route = routes.get(
            route_name
        )

        if not route:

            await update.message.reply_text(
                "Route not found."
            )

            return

        steps = route.get(
            "steps",
            []
        )

        if (
            step_number < 1
            or step_number > len(steps)
        ):

            await update.message.reply_text(
                "❌ Invalid step number."
            )

            return

        # ----------------------------------------------------
        # Convert old string step
        # ----------------------------------------------------

        if isinstance(
            steps[step_number - 1],
            str
        ):

            old_domain = steps[
                step_number - 1
            ]

            steps[
                step_number - 1
            ] = {
                "targets": [
                    clean_domain(
                        old_domain
                    )
                ]
            }

        targets = steps[
            step_number - 1
        ].setdefault(
            "targets",
            []
        )

        domains = text.split(
            ","
        )

        added = []

        for domain in domains:

            domain = clean_domain(
                domain
            )

            if (
                domain
                and domain not in targets
            ):

                targets.append(
                    domain
                )

                added.append(
                    domain
                )

        save_routes(
            routes
        )

        if added:

            await update.message.reply_text(
                f"✅ Added to Step "
                f"{step_number}:\n\n"
                f"<code>"
                f"{html.escape(', '.join(added))}"
                f"</code>",
                parse_mode="HTML"
            )

        else:

            await update.message.reply_text(
                "ℹ️ No new domains added."
            )

        context.user_data[
            "mode"
        ] = "edit_menu"

        return

    # ========================================================
    # DELETE TARGET: STEP
    # ========================================================

    if mode == "edit_delete_target_step":

        try:

            step_number = int(
                text
            )

        except ValueError:

            await update.message.reply_text(
                "Send a valid step number."
            )

            return

        route_name = context.user_data[
            "edit_route"
        ]

        routes = load_routes()

        route = routes.get(
            route_name
        )

        if not route:

            await update.message.reply_text(
                "Route not found."
            )

            return

        steps = route.get(
            "steps",
            []
        )

        if (
            step_number < 1
            or step_number > len(steps)
        ):

            await update.message.reply_text(
                "Invalid step."
            )

            return

        # Convert old format
        if isinstance(
            steps[step_number - 1],
            str
        ):

            old_domain = steps[
                step_number - 1
            ]

            steps[
                step_number - 1
            ] = {
                "targets": [
                    clean_domain(
                        old_domain
                    )
                ]
            }

            save_routes(
                routes
            )

        targets = steps[
            step_number - 1
        ].get(
            "targets",
            []
        )

        if not targets:

            await update.message.reply_text(
                "This step has no targets."
            )

            return

        context.user_data[
            "edit_step"
        ] = step_number

        context.user_data[
            "edit_targets"
        ] = targets

        context.user_data[
            "mode"
        ] = "edit_delete_target_number"

        lines = [
            f"🗑 <b>Step {step_number}</b>",
            ""
        ]

        for number, target in enumerate(
            targets,
            start=1
        ):

            lines.append(
                f"<b>{number}.</b> "
                f"<code>"
                f"{html.escape(target)}"
                f"</code>"
            )

        lines.extend([
            "",
            "Reply with target number."
        ])

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML"
        )

        return

    # ========================================================
    # DELETE TARGET: NUMBER
    # ========================================================

    if mode == "edit_delete_target_number":

        try:

            number = int(
                text
            )

        except ValueError:

            await update.message.reply_text(
                "Send a number."
            )

            return

        route_name = context.user_data[
            "edit_route"
        ]

        step_number = context.user_data[
            "edit_step"
        ]

        routes = load_routes()

        route = routes.get(
            route_name
        )

        if not route:

            await update.message.reply_text(
                "Route not found."
            )

            return

        step = route[
            "steps"
        ][
            step_number - 1
        ]

        # Support old string format
        if isinstance(
            step,
            str
        ):

            step = {
                "targets": [
                    clean_domain(step)
                ]
            }

            route[
                "steps"
            ][
                step_number - 1
            ] = step

        targets = step.get(
            "targets",
            []
        )

        if (
            number < 1
            or number > len(targets)
        ):

            await update.message.reply_text(
                "Invalid target number."
            )

            return

        removed = targets.pop(
            number - 1
        )

        save_routes(
            routes
        )

        await update.message.reply_text(
            "🗑 Removed:\n"
            f"<code>"
            f"{html.escape(removed)}"
            f"</code>",
            parse_mode="HTML"
        )

        context.user_data[
            "mode"
        ] = "edit_menu"

        return

    # ========================================================
    # DELETE STEP
    # ========================================================

    if mode == "edit_delete_step":

        try:

            number = int(
                text
            )

        except ValueError:

            await update.message.reply_text(
                "Send a valid step number."
            )

            return

        route_name = context.user_data[
            "edit_route"
        ]

        routes = load_routes()

        route = routes.get(
            route_name
        )

        if not route:

            await update.message.reply_text(
                "Route not found."
            )

            return

        steps = route.get(
            "steps",
            []
        )

        if (
            number < 1
            or number > len(steps)
        ):

            await update.message.reply_text(
                "Invalid step number."
            )

            return

        steps.pop(
            number - 1
        )

        save_routes(
            routes
        )

        await update.message.reply_text(
            f"🗑 Step {number} deleted."
        )

        context.user_data[
            "mode"
        ] = "edit_menu"

        return

    # ========================================================
    # EDIT FINAL TYPE
    # ========================================================

    if mode == "edit_final_type":

        route_name = context.user_data[
            "edit_route"
        ]

        routes = load_routes()

        route = routes.get(
            route_name
        )

        if not route:

            await update.message.reply_text(
                "Route not found."
            )

            return

        if text == "1":

            route[
                "final"
            ] = {
                "type": "current_url"
            }

            save_routes(
                routes
            )

            await update.message.reply_text(
                "✅ Final rule changed."
            )

            context.user_data[
                "mode"
            ] = "edit_menu"

            return

        if text == "2":

            context.user_data[
                "mode"
            ] = "edit_final_parameter"

            await update.message.reply_text(
                "Send the query parameter.\n\n"
                "Example: <code>link</code>",
                parse_mode="HTML"
            )

            return

        if text == "3":

            context.user_data[
                "mode"
            ] = "edit_final_domain"

            await update.message.reply_text(
                "Send the final domain."
            )

            return

        await update.message.reply_text(
            "Reply with 1, 2 or 3."
        )

        return

    # ========================================================
    # EDIT FINAL PARAMETER
    # ========================================================

    if mode == "edit_final_parameter":

        route_name = context.user_data[
            "edit_route"
        ]

        routes = load_routes()

        routes[
            route_name
        ]["final"] = {
            "type": "query_parameter",
            "parameter": text.strip()
        }

        save_routes(
            routes
        )

        await update.message.reply_text(
            "✅ Final parameter updated."
        )

        context.user_data[
            "mode"
        ] = "edit_menu"

        return

    # ========================================================
    # EDIT FINAL DOMAIN
    # ========================================================

    if mode == "edit_final_domain":

        route_name = context.user_data[
            "edit_route"
        ]

        routes = load_routes()

        routes[
            route_name
        ]["final"] = {
            "type": "domain",
            "domain": clean_domain(
                text
            )
        }

        save_routes(
            routes
        )

        await update.message.reply_text(
            "✅ Final domain updated."
        )

        context.user_data[
            "mode"
        ] = "edit_menu"

        return

    # ========================================================
    # DELETE ROUTE
    # ========================================================

    if mode == "delete_route":

        names = context.user_data[
            "delete_routes"
        ]

        try:

            number = int(
                text
            )

        except ValueError:

            await update.message.reply_text(
                "Send a number."
            )

            return

        if (
            number < 1
            or number > len(names)
        ):

            await update.message.reply_text(
                "Invalid number."
            )

            return

        name = names[
            number - 1
        ]

        routes = load_routes()

        routes.pop(
            name,
            None
        )

        save_routes(
            routes
        )

        context.user_data.clear()

        context.user_data[
            "session"
        ] = requests.Session()

        await update.message.reply_text(
            f"🗑 Deleted route "
            f"<b>{html.escape(name)}</b>.",
            parse_mode="HTML"
        )

        return

    # ========================================================
    # UNKNOWN MODE
    # ========================================================

    await update.message.reply_text(
        "❌ Unknown state.\n\n"
        "Use /cancel and try again."
    )

# ============================================================

# EDIT MENU COMMAND

# ============================================================

async def editmenu_command(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

    if context.user_data.get(
        "edit_route"
    ):

        context.user_data[
            "mode"
        ] = "edit_menu"

        await show_edit_menu(
            update,
            context
        )

    else:

        await update.message.reply_text(
            "Use /editroute first."
        )

# ============================================================

# MAIN

# ============================================================

def start_health_server():
    """Start a tiny HTTP server for Koyeb health checks and uptime pings."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(os.getenv("PORT", "8000"))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health", "/ping"):
                body = b"OK"
                self.send_response(200)
            else:
                body = b"Not Found"
                self.send_response(404)

            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[WEB] Health server listening on port {port}")
    server.serve_forever()


def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing. "
            "Set BOT_TOKEN in Koyeb using your Secret."
        )

    import threading
    threading.Thread(target=start_health_server, daemon=True).start()

    print(
        "=" * 60
    )

    print(
        "Multi-Step Telegram Link Bot"
    )

    print(
        "=" * 60
    )

    routes = load_routes()

    print(
        f"Loaded {len(routes)} route(s)."
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # ========================================================
    # COMMANDS
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "debug",
            debug_command
        )
    )

    application.add_handler(
        CommandHandler(
            "addroute",
            addroute_command
        )
    )

    application.add_handler(
        CommandHandler(
            "addstep",
            addstep_command
        )
    )

    application.add_handler(
        CommandHandler(
            "endstep",
            endstep_command
        )
    )

    application.add_handler(
        CommandHandler(
            "routes",
            routes_command
        )
    )

    application.add_handler(
        CommandHandler(
            "editroute",
            editroute_command
        )
    )

    application.add_handler(
        CommandHandler(
            "addtarget",
            addtarget_command
        )
    )

    application.add_handler(
        CommandHandler(
            "deletetarget",
            deletetarget_command
        )
    )

    application.add_handler(
        CommandHandler(
            "deletestep",
            deletestep_command
        )
    )

    application.add_handler(
        CommandHandler(
            "editfinal",
            editfinal_command
        )
    )

    application.add_handler(
        CommandHandler(
            "done",
            done_command
        )
    )

    application.add_handler(
        CommandHandler(
            "deleteroute",
            deleteroute_command
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command
        )
    )

    application.add_handler(
        CommandHandler(
            "editmenu",
            editmenu_command
        )
    )

    # ========================================================
    # /i<number> COMPILER
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.Regex(r"^/i\d+(?:@\w+)?$"),
            compile_links_command
        )
    )

    # ========================================================
    # NORMAL TEXT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text
        )
    )

    print(
        "Bot is running..."
    )

    application.run_polling()

# ============================================================

# START

# ============================================================

if __name__ == "__main__":

    main()
