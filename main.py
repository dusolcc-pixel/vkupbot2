import json
import os
import re
import html
import requests
import asyncio

# ============================================================
# DOMAINS THAT MUST USE A REAL BROWSER (CLICK METHOD)
# ============================================================
#
# Add domains here manually whenever a site needs JavaScript,
# rendered buttons, generated mirrors, or click actions.
#
# Example:
#     "titancloud.site",   # click method
#     "anothercloud.example",  # click method
#
BROWSER_FIRST_DOMAINS = {
    "titancloud.site",  # click method
    # Add more JavaScript/click sites here manually:
    # "technocloud.site",
    # "examplecloud.com",
}

# ============================================================
# REAL BROWSER SITES THAT REQUIRE A PERSISTENT LOGIN SESSION
# ============================================================
#
# Add domains here when the site requires a one-time login (for
# example, Login with Telegram) before its real links/content are
# visible. The bot loads a Playwright storage_state JSON file for
# that domain.
#
# Example:
#     "members.example.com",
#
AUTH_BROWSER_DOMAINS = {
    # "members.example.com",
}

AUTH_STATE_DIR = os.getenv("AUTH_STATE_DIR", "auth_states").strip() or "auth_states"

# GDFlix keeps the old fast curl_cffi method.
# Add new GDFlix mirrors here if they do not already contain
# the word "gdflix" in their hostname.
GDFlIX_DOMAINS = {
    "gdflix.io",
    "gdflix.dad",
    "gdflix.net",
    # "new-gdflix-mirror.example",
}

# Also keep the old automatic marker behavior. Any hostname
# containing "gdflix" is treated as a GDFlix/curl_cffi site.
GDFlIX_DOMAIN_MARKER = "gdflix"

BROWSER_TIMEOUT_MS = 30_000
BROWSER_WAIT_MS = 6_000
BROWSER_HEADLESS = True
TELEGRAM_SAFE_LIMIT = 3900

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("[WARN] Playwright is not installed. Browser domains will not work.")

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

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

# Optional built-in routes. These survive hosts that wipe routes.json on restart.
# Add permanent routes here manually when needed.
DEFAULT_ROUTES = {
    # Example:
    # "titancloud": {
    #     "main_domain": "titancloud.site",
    #     "steps": [],
    #     "direct_targets": ["example-download-domain.com"],
    #     "final": {"type": "current_url"},
    # },
}

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
    routes = {}

    if os.path.exists(ROUTES_FILE):
        try:
            with open(
                ROUTES_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            if isinstance(data, dict):
                routes.update(data)

        except Exception as e:
            print(
                "routes.json read error:",
                e
            )

    # Built-in defaults always win when there is no matching saved route.
    for name, route in DEFAULT_ROUTES.items():
        if name not in routes and isinstance(route, dict):
            routes[name] = json.loads(json.dumps(route))

    return routes


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
# BROWSER / CLICK HELPERS
# ============================================================

from dataclasses import dataclass


@dataclass
class PageResult:
    url: str
    text: str
    status_code: int = 200
    title: str = ""


def hostname_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def domain_in_list(url, domains):
    host = hostname_of(url)
    if not host:
        return False

    for domain in domains:
        domain = clean_domain(domain)
        if host == domain or host.endswith("." + domain):
            return True

    return False


def should_use_browser(url):
    return (
        domain_in_list(url, BROWSER_FIRST_DOMAINS)
        or domain_in_list(url, AUTH_BROWSER_DOMAINS)
    )


def requires_auth_browser(url):
    return domain_in_list(url, AUTH_BROWSER_DOMAINS)


def auth_state_path(url):
    """Return the per-domain Playwright storage-state path."""
    host = hostname_of(url)
    if not host:
        return None
    os.makedirs(AUTH_STATE_DIR, exist_ok=True)
    safe_host = re.sub(r"[^a-zA-Z0-9._-]", "_", host)
    return os.path.join(AUTH_STATE_DIR, f"{safe_host}.json")


def auth_state_exists(url):
    path = auth_state_path(url)
    return bool(path and os.path.isfile(path))


def should_use_gdflix(url):
    host = hostname_of(url)
    if not host:
        return False

    if domain_in_list(url, GDFlIX_DOMAINS):
        return True

    return GDFlIX_DOMAIN_MARKER in host


def _browser_asset(url):
    path = urlparse(url).path.lower()
    bad = (
        ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif",
        ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf",
        "googletagmanager", "google-analytics", "_next/static"
    )
    return any(x in path or x in url.lower() for x in bad)


def _browser_useful_text(text):
    t = (text or "").strip().lower()
    if not t:
        return False

    markers = (
        "download", "direct", "mirror", "server", "generate",
        "mkv", "mp4", "avi", "mov", "webm",
        "stream", "get link", "file", "1080", "720", "480", "360"
    )
    blocked = (
        "sign in", "login", "logout", "register", "subscribe",
        "purchase", "delete", "cancel"
    )

    if any(x in t for x in blocked):
        return False

    return any(x in t for x in markers)


async def _collect_browser_links(page, base_url):
    links = []

    # Normal visible anchors.
    anchors = await page.locator("a[href]").evaluate_all(
        """els => els.map(a => ({
            href: a.href || "",
            text: (a.innerText || a.textContent || "").trim(),
            visible: !!(a.offsetWidth || a.offsetHeight || a.getClientRects().length)
        }))"""
    )

    for item in anchors:
        href = (item.get("href") or "").strip()
        if not href or _browser_asset(href):
            continue
        if item.get("visible") or not item.get("text"):
            if valid_url(href) and href not in links:
                links.append(href)

    # Rendered controls and their direct href/data-url values.
    controls = await page.locator(
        'button, [role="button"], input[type="button"], input[type="submit"], [onclick], [data-href], [data-url], [data-link]'
    ).evaluate_all(
        """els => els.map((el, i) => ({
            i,
            text: (el.innerText || el.textContent || el.value || "").trim(),
            href: el.href || "",
            dataHref: el.getAttribute("data-href") || "",
            dataUrl: el.getAttribute("data-url") || "",
            dataLink: el.getAttribute("data-link") || "",
            visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        }))"""
    )

    for item in controls:
        if not item.get("visible"):
            continue
        candidates = [
            item.get("href"),
            item.get("dataHref"),
            item.get("dataUrl"),
            item.get("dataLink"),
        ]
        for href in candidates:
            if not href:
                continue
            href = urljoin(base_url, href)
            if valid_url(href) and not _browser_asset(href) and href not in links:
                links.append(href)

    return links


async def _click_browser_controls(page, base_url):
    discovered = []

    # We inspect a bounded number of useful visible controls. Each click is
    # isolated by reloading the page, so one control cannot destroy the next.
    controls = await page.locator(
        'button, [role="button"], input[type="button"], input[type="submit"], a'
    ).evaluate_all(
        """els => els.map((el, i) => ({
            i,
            tag: el.tagName,
            text: (el.innerText || el.textContent || el.value || "").trim(),
            href: el.href || "",
            visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).filter(x => x.visible && x.text)"""
    )

    useful = [
        x for x in controls
        if _browser_useful_text(x.get("text", ""))
    ][:20]

    for item in useful:
        try:
            await page.goto(
                base_url,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT_MS
            )
            await page.wait_for_timeout(min(BROWSER_WAIT_MS, 4000))

            target = page.locator(
                'button, [role="button"], input[type="button"], input[type="submit"], a'
            ).filter(has_text=item.get("text", ""))

            if await target.count() == 0:
                continue

            control = target.first
            if not await control.is_visible():
                continue

            captured = []
            downloads = []

            def on_request(req):
                u = req.url
                if valid_url(u) and not _browser_asset(u):
                    if u not in captured:
                        captured.append(u)

            def on_download(download):
                try:
                    u = download.url
                    if valid_url(u):
                        downloads.append(u)
                except Exception:
                    pass

            page.on("request", on_request)
            page.on("download", on_download)

            popup_url = None
            try:
                async with page.expect_popup(timeout=2500) as popup_info:
                    await control.click(timeout=5000)
                popup = await popup_info.value
                try:
                    await popup.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                popup_url = popup.url
                if valid_url(popup_url) and popup_url not in captured:
                    captured.append(popup_url)
                for u in await _collect_browser_links(popup, popup.url):
                    if u not in discovered:
                        discovered.append(u)
                await popup.close()
            except Exception:
                try:
                    await control.click(timeout=5000)
                except Exception:
                    continue

            await page.wait_for_timeout(2500)

            if downloads:
                for u in downloads:
                    if u not in discovered:
                        discovered.append(u)

            if popup_url and popup_url not in discovered:
                discovered.append(popup_url)

            for u in captured:
                # Keep the clicked control's own href, direct results, and
                # newly generated URLs. Ignore obvious browser assets.
                if not _browser_asset(u) and u not in discovered:
                    discovered.append(u)

            for u in await _collect_browser_links(page, page.url):
                if u not in discovered:
                    discovered.append(u)

        except Exception as e:
            print("[BROWSER CLICK ERROR]", item.get("text"), e)
            continue

    return discovered


async def fetch_page_browser(url):
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright is not installed. Add it to requirements.txt."
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=BROWSER_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context_kwargs = {
            "user_agent": HEADERS["User-Agent"],
            "viewport": {"width": 1366, "height": 768},
            "locale": "en-US",
            "timezone_id": "Asia/Kolkata",
            "ignore_https_errors": True,
            "accept_downloads": True,
        }

        # Authenticated browser domains reuse the saved Playwright
        # storage state created by auth_setup.py. We deliberately do
        # not overwrite this file automatically, because an expired
        # login must not destroy a still-valid backup state.
        if requires_auth_browser(url):
            state_path = auth_state_path(url)
            if state_path and os.path.isfile(state_path):
                context_kwargs["storage_state"] = state_path
                print("[BROWSER AUTH] Using saved session:", state_path)
            else:
                print("[BROWSER AUTH] No saved session for:", hostname_of(url))

        context = await browser.new_context(**context_kwargs)

        page = await context.new_page()
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT_MS
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            await page.wait_for_timeout(BROWSER_WAIT_MS)

            # Authenticated sites may still open successfully but show their
            # login wall when no saved session is available. Keep debugging
            # usable and make the missing-session reason explicit.
            if requires_auth_browser(url) and not auth_state_exists(url):
                print(
                    "[BROWSER AUTH] Authentication state missing for ",
                    hostname_of(url),
                    " - run auth_setup.py locally first."
                )

            # Trigger lazy-rendered controls.
            try:
                await page.evaluate(
                    """async () => {
                        for (let y = 0; y < document.body.scrollHeight; y += 700) {
                            window.scrollTo(0, y);
                            await new Promise(r => setTimeout(r, 150));
                        }
                        window.scrollTo(0, 0);
                    }"""
                )
            except Exception:
                pass

            links = await _collect_browser_links(page, page.url)

            clicked = await _click_browser_controls(page, url)
            for u in clicked:
                if valid_url(u) and not _browser_asset(u) and u not in links:
                    links.append(u)

            # Preserve rendered page text so debug output is useful.
            page_text = await page.locator("body").inner_text()
            title = await page.title()

            return (
                PageResult(
                    url=page.url,
                    text=(
                        "<html><head><title>"
                        + html.escape(title or "")
                        + "</title></head><body>"
                        + html.escape(page_text or "")
                        + "</body></html>"
                    ),
                    status_code=200,
                    title=title or "",
                ),
                links,
            )
        finally:
            await context.close()
            await browser.close()



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
# EXTRACT LINKS
# ============================================================

def extract_links(
    response
):

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    links = []

    # --------------------------------------------------------
    # HTML <a href>
    # --------------------------------------------------------

    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get(
            "href",
            ""
        ).strip()

        if not href:
            continue

        href = urljoin(
            response.url,
            href
        )

        if (
            valid_url(href)
            and href not in links
        ):

            links.append(
                href
            )

    # --------------------------------------------------------
    # URLs inside JavaScript / HTML
    # --------------------------------------------------------

    regex_urls = re.findall(
        r'https?://[^\s\'"<>]+',
        response.text
    )

    for href in regex_urls:

        href = href.rstrip(
            "',);]}"
        )

        if (
            valid_url(href)
            and href not in links
        ):

            links.append(
                href
            )

    return links


# ============================================================
# FETCH PAGE
#
# Engine selection:
#   - GDFlix          -> old curl_cffi method
#   - BROWSER list    -> real browser + click method
#   - everything else -> normal requests method
# ============================================================

from curl_cffi import requests as cffi_requests


async def fetch_page(session, url):
    if should_use_browser(url):
        print("[BROWSER] Rendering/clicking:", url)
        return await fetch_page_browser(url)

    if should_use_gdflix(url):
        print("[GDFlIX] Using curl_cffi:", url)
        response = cffi_requests.get(
            url,
            impersonate="chrome",
            timeout=25,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response, extract_links(response)

    print("[HTTP] Using normal requests:", url)
    response = session.get(
        url,
        headers=HEADERS,
        timeout=25,
        allow_redirects=True,
    )
    response.raise_for_status()

    return PageResult(
        url=response.url,
        text=response.text,
        status_code=response.status_code,
        title=(
            BeautifulSoup(response.text, "html.parser").title.get_text(
                " ", strip=True
            )
            if BeautifulSoup(response.text, "html.parser").title
            else ""
        ),
    ), extract_links(response)


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
    # DIRECT-LINK ROUTE
    # --------------------------------------------------------
    # If direct_targets are configured, fetch the first page using
    # its domain's selected engine and return the first matching link.
    direct_targets = [
        clean_domain(x)
        for x in route.get("direct_targets", [])
        if str(x).strip()
    ]

    if direct_targets:
        response, links = await fetch_page(
            session,
            current_url
        )

        selected = None

        for target in direct_targets:
            if domain_matches(response.url, target):
                selected = response.url
                break

            selected = find_target_link(
                links,
                [target]
            )

            if selected:
                break

        if selected is None:
            raise RuntimeError(
                "Direct route: none of these domains were found: "
                + ", ".join(direct_targets)
            )

        return extract_final_url(
            selected,
            route.get("final")
        ), [
            {
                "type": "direct",
                "targets": direct_targets,
                "input": current_url,
                "selected": selected
            }
        ]

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

def format_debug_links(
    links
):

    if not links:

        return (
            "❌ <b>No links found.</b>"
        )

    shown = links[
        :MAX_DEBUG_LINKS
    ]

    lines = [
        f"🔗 <b>Links found: "
        f"{len(links)}</b>",
        ""
    ]

    for number, link in enumerate(
        shown,
        start=1
    ):

        display = link

        if len(display) > 700:

            display = (
                display[:697]
                + "..."
            )

        lines.append(
            f"<b>{number}.</b> "
            f"<code>{html.escape(display)}</code>"
        )

    if len(links) > MAX_DEBUG_LINKS:

        lines.extend([
            "",
            f"⚠️ Showing first "
            f"{MAX_DEBUG_LINKS} links."
        ])

    lines.extend([
        "",
        "👉 Reply with the <b>number</b> "
        "of the link you want to debug.",
        "",
        "Send /cancel to stop."
    ])

    return "\n".join(
        lines
    )


async def debug_page(
    update,
    context,
    url
):

    status = await update.message.reply_text(
        "🔎 Fetching page...",
        disable_web_page_preview=True
    )

    try:
        session = context.user_data.get(
            "session"
        )

        if session is None:
            session = requests.Session()
            context.user_data[
                "session"
            ] = session

        response, links = await fetch_page(
            session,
            url
        )

        # Save debug state
        context.user_data[
            "debug_links"
        ] = links

        context.user_data[
            "debug_current_url"
        ] = response.url

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        title = response.title or "Unknown"

        if soup.title:
            title = soup.title.get_text(
                " ",
                strip=True
            )

        message = (
            "✅ <b>Page fetched</b>\n\n"
            f"<b>Status:</b> {response.status_code}\n\n"
            f"<b>Final URL:</b>\n"
            f"<code>{html.escape(response.url)}</code>\n\n"
            f"<b>Title:</b>\n"
            f"{html.escape(title[:500])}\n\n"
        )

        message += format_debug_links(
            links
        )

        # Telegram limit workaround:
        # normal-sized debug stays directly in Telegram.
        # Only oversized debug output goes to Spacebin.
        if len(message) <= TELEGRAM_SAFE_LIMIT:
            await status.edit_text(
                message,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            return

        spacebin_url = upload_to_spacebin(
            message
        )

        if spacebin_url:
            compact = (
                "✅ <b>Debug complete</b>\n\n"
                f"<b>Status:</b> {response.status_code}\n"
                f"<b>Links found:</b> {len(links)}\n\n"
                "📄 <b>Full debug log is too large for Telegram.</b>\n"
                "Open the Spacebin log below. The same link numbers "
                "are stored here, so reply with a number to continue debugging."
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📄 Open full debug log",
                        url=spacebin_url
                    )
                ]
            ])

            await status.edit_text(
                compact,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            return

        # If Spacebin itself fails, keep Telegram usable by truncating.
        fallback = (
            message[:TELEGRAM_SAFE_LIMIT - 120]
            + "\n\n⚠️ Full debug output was too large for Telegram "
            "and Spacebin upload failed."
        )

        await status.edit_text(
            fallback,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:
        await status.edit_text(
            "❌ <b>Failed</b>\n\n"
            f"<code>{html.escape(str(e)[:1500])}</code>",
            parse_mode="HTML"
        )

# ============================================================
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

            # Every resolved result gets both the direct Telegram link
            # and a Spacebin copy of the final URL.
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
# /I<number> - COMPILE RESULT MESSAGES
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
# /AUTHSTATUS
# ============================================================

async def authstatus_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not AUTH_BROWSER_DOMAINS:
        await update.message.reply_text(
            "🔐 No authenticated browser domains are configured.\n\n"
            "Add a domain to AUTH_BROWSER_DOMAINS at the top of the code."
        )
        return

    lines = [
        "🔐 <b>Authenticated browser sessions</b>",
        ""
    ]

    for domain in sorted(AUTH_BROWSER_DOMAINS):
        state_path = auth_state_path("https://" + clean_domain(domain) + "/")
        if state_path and os.path.isfile(state_path):
            lines.append(f"✅ <code>{html.escape(domain)}</code> — session found")
        else:
            lines.append(f"❌ <code>{html.escape(domain)}</code> — no session")

    lines.extend([
        "",
        "Create a session locally with <code>auth_setup.py</code>, then place the generated JSON in:",
        f"<code>{html.escape(AUTH_STATE_DIR)}/</code>"
    ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
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
        "🎯 /adddirect — direct target while creating\n"
        "✏️ /editroute — edit route\n"
        "📂 /routes — list routes\n"
        "🔐 /authstatus — check login sessions\n"
        "🗑 /deleteroute — delete route\n"
        "❌ /cancel — cancel\n\n"
        "💡 <b>Normal mode:</b>\n"
        "Just send URL(s), one per line.\n"
        "The bot automatically resolves them.\n\n"
        "🌐 Browser/click sites are controlled by "
        "BROWSER_FIRST_DOMAINS at the top of the code.",
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
        "direct_targets": [],
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
# /ADDDIRECT
# ============================================================

async def adddirect_command(
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

    context.user_data[
        "mode"
    ] = "route_direct_target"

    await update.message.reply_text(
        "🎯 <b>Direct-link route</b>\n\n"
        "Send target domain(s) that should be "
        "found directly on the first page.\n\n"
        "Example:\n"
        "<code>cdn.example.com, mirror.example.net</code>",
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

    if not route["steps"] and not route.get("direct_targets"):

        await update.message.reply_text(
            "❌ Add at least one step or one direct target first."
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

        direct_targets = route.get(
            "direct_targets",
            []
        )

        if direct_targets:
            lines.append(
                "Direct: "
                f"<code>{html.escape(', '.join(direct_targets))}</code>"
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
        "direct_targets": route.get(
            "direct_targets",
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
        "<b>Direct targets:</b>",
        f"<code>{html.escape(', '.join(route.get('direct_targets', [])) or 'None')}</code>",
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

    direct_targets = route.get(
        "direct_targets",
        []
    )

    if direct_targets:
        lines.append(
            "Direct: "
            f"<code>{html.escape(', '.join(direct_targets))}</code>"
        )

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
            "🎯 /adddirect — desired link is already on the first page\n"
            "➕ /addstep — the link needs one or more intermediate steps\n\n"
            "Send /cancel to stop.",
            parse_mode="HTML"
        )

        return

    # ========================================================
    # ROUTE DIRECT TARGET
    # ========================================================

    if mode == "route_direct_target":

        route = context.user_data[
            "new_route"
        ]

        raw_domains = text.split(
            ","
        )

        targets = []

        for domain in raw_domains:
            domain = clean_domain(domain)

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
            "direct_targets"
        ] = targets

        context.user_data[
            "mode"
        ] = "route_menu"

        await update.message.reply_text(
            "✅ <b>Direct target(s) saved</b>\n\n"
            f"<code>{html.escape(', '.join(targets))}</code>\n\n"
            "Use /endstep when finished, or /addstep "
            "to add multi-step targets too.",
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
# KOYEB HEALTH CHECK SERVER
# ============================================================

HEALTH_PORT = int(os.getenv("PORT", "8000"))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/health", "/ping"):
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        body = b"Not Found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Keep Koyeb logs quiet unless you are debugging health requests.
        return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="health-server",
        daemon=True,
    )
    thread.start()
    print(f"Web server listening on 0.0.0.0:{HEALTH_PORT}")
    print("Health endpoints: /health and /ping")
    return server


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":

        raise RuntimeError(
            "Put your Telegram bot token "
            "in BOT_TOKEN first."
        )

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

    start_health_server()

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
            "authstatus",
            authstatus_command
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
            "adddirect",
            adddirect_command
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
