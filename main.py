import json
import os
import re
import html
import requests
import asyncio

# Domains that must use a real browser (Cloudflare-protected)
BROWSER_FIRST_DOMAINS = {
    "gdflix.io",
    "gdflix.dad",
    "gdflix.net",
    # add other gdflix mirrors here as they change
}

BROWSER_FALLBACK_STATUS_CODES = {403, 429, 503}
BROWSER_TIMEOUT_MS = 30_000
BROWSER_WAIT_MS = 6_000
BROWSER_HEADLESS = True

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("[WARN] Playwright not installed. gdflix will not work.")

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

BOT_TOKEN = ""

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
    """Upload text to Spacebin and return its public URL.

    Tries the documented JSON API first, then multipart/form-data as a
    compatibility fallback.  Spacebin documents both upload formats.
    """

    if content is None:
        content = ""

    content = str(content)

    headers = {
        "User-Agent": HEADERS["User-Agent"],
    }

    last_error = None

    # --------------------------------------------------------
    # Preferred: documented JSON API.
    # --------------------------------------------------------
    try:
        response = requests.post(
            SPACEBIN_API,
            json={"content": content},
            headers={
                **headers,
                "Content-Type": "application/json",
            },
            timeout=30,
        )

        response.raise_for_status()
        data = response.json()

        error_value = data.get("error")
        if error_value:
            raise RuntimeError(str(error_value))

        payload = data.get("payload") or {}
        paste_id = payload.get("id") or data.get("id")

        if paste_id:
            return f"https://spaceb.in/{paste_id}"

        raise RuntimeError(
            "Spacebin response did not contain a paste ID: "
            + response.text[:500]
        )

    except Exception as e:
        last_error = e
        print("[SPACEBIN JSON ERROR]", repr(e))

    # --------------------------------------------------------
    # Fallback: multipart/form-data.
    # Spacebin documents this upload format as well.
    # --------------------------------------------------------
    try:
        response = requests.post(
            SPACEBIN_API,
            files={"content": (None, content)},
            headers=headers,
            timeout=30,
        )

        response.raise_for_status()
        data = response.json()

        error_value = data.get("error")
        if error_value:
            raise RuntimeError(str(error_value))

        payload = data.get("payload") or {}
        paste_id = payload.get("id") or data.get("id")

        if paste_id:
            return f"https://spaceb.in/{paste_id}"

        raise RuntimeError(
            "Spacebin multipart response did not contain a paste ID: "
            + response.text[:500]
        )

    except Exception as e:
        print("[SPACEBIN MULTIPART ERROR]", repr(e))
        if last_error is not None:
            print("[SPACEBIN LAST ERROR]", repr(last_error))
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
# FETCH PAGE  (requests + Playwright fallback)
# ============================================================

from curl_cffi import requests as cffi_requests

def fetch_page(session, url):
    # impersonate a real Chrome browser's TLS fingerprint
    response = cffi_requests.get(
        url,
        impersonate="chrome",
        timeout=25,
        allow_redirects=True,
    )
    response.raise_for_status()

    # curl_cffi response is compatible enough:
    # response.text, response.url, response.status_code all work
    links = extract_links(response)
    return response, links


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

        response, links = fetch_page(
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

    response, links = fetch_page(
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

        response, links = fetch_page(
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

        title = "Unknown"

        if soup.title:

            title = soup.title.get_text(
                " ",
                strip=True
            )

        message = (
            "✅ <b>Page fetched</b>\n\n"
            f"<b>Status:</b> "
            f"{response.status_code}\n\n"
            f"<b>Final URL:</b>\n"
            f"<code>{html.escape(response.url)}</code>\n\n"
            f"<b>Title:</b>\n"
            f"{html.escape(title[:500])}\n\n"
        )

        message += format_debug_links(
            links
        )

        await status.edit_text(
            message,
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
            filters.COMMAND
            & filters.Regex(r"^/i\d+(?:@\w+)?$"),
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
