import os
import re
import hashlib
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from ura import search_property, format_transactions, price_trend, format_price_trend, render_price_trend_png
from maps import get_nearby_info, resolve_postal_code, geocode_building
from storage import record_search, get_recent_searches
from cache.cache_ura import force_refresh, cache_status
from cache.cache_rental import force_refresh_rental, rental_cache_status
from rental import get_rental_by_band, format_rental
from mortgage import (
    mortgage_summary,
    format_mortgage_summary,
    DEFAULT_RATE_PCT,
    DEFAULT_TENURE_YEARS,
    MAX_TENURE_YEARS,
    MIN_DOWN_PAYMENT_PCT,
)
from liquidity import liquidity_for_project, format_liquidity_summary
from propertyguru import listing_links
from nearby import nearby_for_project
from cache.onemap_mrt import build_mrt_cache
from cache.schools_cache import get_schools_cache
from utils import get_mongo_db, clear_mongo_collection, SIZE_BANDS
from district_search import (
    get_top_developments_by_district,
    format_district_results,
    district_full_name,
    district_button_label,
    NUM_DISTRICTS,
)
import hdb

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WAITING_FOR_PROPERTY_NAME = 1
WAITING_FOR_DISTRICT = 2

# /mortgage conversation states
MORTGAGE_BAND = 9
MORTGAGE_PRICE = 10
MORTGAGE_DOWNPAYMENT = 11
MORTGAGE_RATE = 12
MORTGAGE_TENURE = 13
MORTGAGE_INCOME = 14
MORTGAGE_VARIABLE = 15


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_market_keyboard() -> InlineKeyboardMarkup:
    """Top-level market toggle shown by /start, /search, and New Search.

    One bot serves both private and HDB; the choice sets context.user_data
    ["market"], which routes subsequent free text to the right search layer.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏢 Private", callback_data="market:private")],
        [InlineKeyboardButton("🏠 HDB", callback_data="market:hdb")],
    ])


def build_search_mode_keyboard() -> InlineKeyboardMarkup:
    """The two private-market search options (after the market toggle)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search by Name", callback_data="search_mode:name")],
        [InlineKeyboardButton("📍 Browse by District", callback_data="search_mode:district")],
    ])


def build_hdb_mode_keyboard() -> InlineKeyboardMarkup:
    """The two HDB-market discovery options (after the market toggle)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗺 Browse by Town", callback_data="hdbmode:town")],
        [InlineKeyboardButton("🔍 Block / Street Lookup", callback_data="hdbmode:lookup")],
    ])


def build_hdb_town_keyboard() -> InlineKeyboardMarkup:
    """Grid of HDB towns, 2 per row. Buttons carry the 1-based town index
    (hdbtown:<idx>) — towns are derived from the data, so a new town appears
    automatically once its first resale registers."""
    towns = hdb.hdb_towns()
    keyboard = []
    for i, town in enumerate(towns, 1):
        if (i - 1) % 2 == 0:
            keyboard.append([])
        keyboard[-1].append(
            InlineKeyboardButton(town.title(), callback_data=f"hdbtown:{i}")
        )
    return InlineKeyboardMarkup(keyboard)


def build_district_keyboard() -> InlineKeyboardMarkup:
    """Grid of district buttons, 2 per row, labelled with 2 estate names.

    Buttons stay compact (2 towns) so two fit per row; the full set of towns
    is shown in the results header once a district is selected.
    """
    keyboard = []
    for i in range(1, NUM_DISTRICTS + 1):
        if (i - 1) % 2 == 0:
            keyboard.append([])
        keyboard[-1].append(
            InlineKeyboardButton(f"D{i} · {district_button_label(i)}", callback_data=f"district:{i}")
        )
    return InlineKeyboardMarkup(keyboard)


def store_addr_key(context: ContextTypes.DEFAULT_TYPE, addr_key: str) -> str:
    """Stash the full "PROJECT|STREET" addr_key and return a short token.

    Telegram caps callback_data at 64 bytes. Embedding full project + street
    names overflows for longer names (e.g. "AFFINITY AT SERANGOON"), so we map
    the addr_key to an 8-char token and carry only the token in callback_data.
    """
    token = hashlib.md5(addr_key.encode("utf-8")).hexdigest()[:8]
    context.user_data.setdefault("addr_keys", {})[token] = addr_key
    return token


def resolve_addr_key(context: ContextTypes.DEFAULT_TYPE, token: str) -> str | None:
    """Look up a stored addr_key by token. None if unknown (e.g. after restart)."""
    # Backward-compat: legacy buttons embedded the literal "PROJECT|STREET".
    if "|" in token:
        return token
    return context.user_data.get("addr_keys", {}).get(token)


def store_hdb_target(context: ContextTypes.DEFAULT_TYPE, payload: str) -> str:
    """Stash an HDB navigation target ("<STREET>" or "<BLOCK>|<STREET>") behind
    a short token, mirroring store_addr_key — HDB street names overflow
    Telegram's 64-byte callback_data limit (e.g. 'KALLANG/WHAMPOA ...')."""
    token = hashlib.md5(payload.encode("utf-8")).hexdigest()[:8]
    context.user_data.setdefault("hdb_targets", {})[token] = payload
    return token


def resolve_hdb_target(context: ContextTypes.DEFAULT_TYPE, token: str | None) -> str | None:
    """Look up a stored HDB target by token. None if unknown (e.g. after restart)."""
    return context.user_data.get("hdb_targets", {}).get(token) if token else None


def store_addr_coords(context: ContextTypes.DEFAULT_TYPE, token: str, lat: float, lng: float):
    """Stash exact origin coords (from a postal-code lookup) under an addr token.

    Lets the amenity buttons reuse the precise OneMap coordinate instead of
    re-geocoding the development name via Google — keeping both the nearest-
    amenity selection and the walk/transit times pinned to the searched address.
    """
    context.user_data.setdefault("addr_coords", {})[token] = (lat, lng)


def resolve_addr_coords(context: ContextTypes.DEFAULT_TYPE, token: str | None):
    """Look up stored origin coords by token, or None if none were stored."""
    return context.user_data.get("addr_coords", {}).get(token) if token else None


def store_addr_band_prices(context: ContextTypes.DEFAULT_TYPE, token: str, band_prices: dict):
    """Stash a {SIZE_BANDS index → avg price} map under an addr token.

    The Affordability button lets the user pick a size band; the chosen band's
    average price seeds the mortgage flow so they don't retype a figure they
    just saw in the transaction results.
    """
    context.user_data.setdefault("addr_band_prices", {})[token] = band_prices


def resolve_addr_band_prices(context: ContextTypes.DEFAULT_TYPE, token: str | None):
    """Look up the stored {band index → price} map by token, or None if none."""
    return context.user_data.get("addr_band_prices", {}).get(token) if token else None


def build_amenity_keyboard(token: str) -> InlineKeyboardMarkup:
    """Build the standard amenity button keyboard. `token` resolves to an addr_key."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚇 Nearest MRT", callback_data=f"amenity:mrt:{token}"),
            InlineKeyboardButton("🏫 Primary Schools", callback_data=f"amenity:schools:{token}"),
        ],
        [
            InlineKeyboardButton("🛍️ Shopping Malls", callback_data=f"amenity:malls:{token}"),
            InlineKeyboardButton("🛒 Supermarkets", callback_data=f"amenity:supermarkets:{token}"),
        ],
        [
            InlineKeyboardButton("🏠 Rental & Yield", callback_data=f"amenity:rental:{token}"),
            InlineKeyboardButton("📈 Price Trend", callback_data=f"amenity:trend:{token}"),
        ],
        [
            InlineKeyboardButton("🏦 Affordability", callback_data=f"mortgage:{token}"),
            InlineKeyboardButton("📊 Liquidity", callback_data=f"liquidity:{token}"),
        ],
        [
            InlineKeyboardButton("🟥 PropertyGuru (Available Listings)", callback_data=f"pg:{token}"),
        ],
        [
            # Carry the origin token so the follow-up menu can offer "Nearby".
            InlineKeyboardButton("🔍 Search another property", callback_data=f"new_search:{token}"),
        ],
    ])


def build_hdb_amenity_keyboard(token: str) -> InlineKeyboardMarkup:
    """Amenity keyboard for an HDB block. Reuses the private amenity_callback
    (same `amenity:<type>:<token>` pattern, served from stashed coords), but
    only the location amenities apply — HDB has no rental/trend/liquidity/PG
    in v1."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚇 Nearest MRT", callback_data=f"amenity:mrt:{token}"),
            InlineKeyboardButton("🏫 Primary Schools", callback_data=f"amenity:schools:{token}"),
        ],
        [
            InlineKeyboardButton("🛍️ Shopping Malls", callback_data=f"amenity:malls:{token}"),
            InlineKeyboardButton("🛒 Supermarkets", callback_data=f"amenity:supermarkets:{token}"),
        ],
        [
            # Bare new_search (no token) — HDB has no "nearby" follow-up.
            InlineKeyboardButton("🔍 Search another property", callback_data="new_search"),
        ],
    ])


def format_amenity_list(items: list, title: str, empty_msg: str, note: str = "") -> str:
    """Format a list of amenity results into a Telegram message."""
    if not items:
        return empty_msg
    lines = [title, "─────────────────────"]
    for i, item in enumerate(items, 1):
        walk_line = f"     🚶 {item['duration']} ({item['distance']})"
        if item.get("transit_duration"):
            transit_line = f"\n     🚌 ~{item['transit_duration']} ({item['transit_distance']}) by transit _(est. Tue 9am)_"
        else:
            transit_line = ""
        link_label = "Transit directions" if item.get("transit_duration") else "Walking directions"
        lines.append(
            f"  {i}. {item['name']}\n"
            f"{walk_line}{transit_line}\n"
            f"     [{link_label}]({item['maps_link']})"
        )
    if note:
        lines += ["", note]
    return "\n".join(lines)


def get_user_id(msg) -> int:
    return msg.from_user.id if hasattr(msg, "from_user") and msg.from_user else 0


def get_username(msg) -> str:
    return msg.from_user.username if hasattr(msg, "from_user") and msg.from_user else "unknown"


# ── Command Handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep link from a district list: /start d<district>r<rank> → open that property
    if context.args:
        arg = context.args[0].strip()
        m = re.fullmatch(r"d(\d+)r(\d+)", arg)
        if m:
            district, rank = int(m.group(1)), int(m.group(2))
            developments = get_top_developments_by_district(district, limit=10)
            if 1 <= rank <= len(developments):
                await handle_property_search(update, context, developments[rank - 1]["project"])
                return
        # HDB deep link: /start t<NN> → open that town's resale overview
        mt = re.fullmatch(r"t(\d+)", arg)
        if mt:
            await _send_hdb_town_overview(update.message, context, int(mt.group(1)))
            return

    await update.message.reply_text(
        "🏠 *Singapore Property Search — Private & HDB*\n\n"
        "Instant data on both private condos and HDB resale flats.\n\n"
        "🏢 *Private* — transacted prices by unit size, rental & gross yield, "
        "mortgage & affordability (TDSR), liquidity, nearby developments and "
        "PropertyGuru listings.\n"
        "🏠 *HDB* — resale prices by flat type, remaining lease & PSF, "
        "town and block/street lookup.\n\n"
        "Both markets also show: nearest MRT, primary schools within 2km, "
        "shopping malls and supermarkets.\n\n"
        "*Ways to search:*\n"
        "🔍 *By Name / Block* — a specific development or HDB block\n"
        "📍 *By District / Town* — browse the top transacted in an area\n"
        "📮 *By Postal Code* — just send a 6-digit code; it auto-detects "
        "condo vs HDB for you\n\n"
        "Pick a market below to get started.\n\n"
        "Commands:\n"
        "/search — find a property (private or HDB)\n"
        "/mortgage — affordability & monthly repayment\n"
        "/list — most searched developments\n"
        "/refresh — update property data\n"
        "/help — show this message",
        parse_mode="Markdown",
        reply_markup=build_market_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Refresh all caches and report results."""
    msg = await update.message.reply_text("🔄 Refreshing all caches...\nThis may take ~1 minute.")
    lines = []

    # 1. URA transactions
    ura_ok = force_refresh()
    ura_status = cache_status()
    lines.append(
        f"✅ URA transactions — {ura_status.get('projects', '?')} projects"
        if ura_ok else "❌ URA transactions — refresh failed"
    )
    await msg.edit_text("🔄 Refreshing all caches...\n" + "\n".join(lines))

    # 2. MRT stations
    try:
        clear_mongo_collection("mrt_cache")
        stations = build_mrt_cache()
        lines.append(f"✅ MRT stations — {len(stations)} stations")
    except Exception as e:
        logger.error(f"MRT refresh failed: {e}")
        lines.append("❌ MRT stations — refresh failed")
    await msg.edit_text("🔄 Refreshing all caches...\n" + "\n".join(lines))

    # 3. Primary schools
    try:
        clear_mongo_collection("schools_cache")
        schools = get_schools_cache()
        lines.append(f"✅ Primary schools — {len(schools)} schools")
    except Exception as e:
        logger.error(f"Schools refresh failed: {e}")
        lines.append("❌ Primary schools — refresh failed")
    await msg.edit_text("🔄 Refreshing all caches...\n" + "\n".join(lines))

    # 4. Rental data
    rental_ok = force_refresh_rental()
    r_status = rental_cache_status()
    lines.append(
        f"✅ Rental data — {r_status.get('projects', '?')} projects "
        f"({', '.join(r_status.get('quarters', []))})"
        if rental_ok else "❌ Rental data — refresh failed"
    )
    await msg.edit_text("🔄 All caches refreshed\n\n" + "\n".join(lines))


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show most searched properties as clickable buttons."""
    searches = get_recent_searches(limit=10)
    if not searches:
        await update.message.reply_text("No search history found.")
        return
    keyboard = [
        [InlineKeyboardButton(s["name"], callback_data=f"search:{s['name']}")]
        for s in searches
    ]
    keyboard.append([InlineKeyboardButton("🔍 Search a new property", callback_data="new_search")])
    await update.message.reply_text(
        "🏆 *Frequently Searched Properties*\nTap a property to reload it:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── /search Conversation ──────────────────────────────────────────────────────

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await handle_property_search(update, context, " ".join(context.args))
        return ConversationHandler.END

    await update.message.reply_text(
        "🏠 Which market?",
        reply_markup=build_market_keyboard(),
    )
    return WAITING_FOR_PROPERTY_NAME


async def received_property_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # A bare 6-digit postal code auto-detects private vs HDB regardless of the
    # toggle; other free text follows the chosen market.
    postal_match = re.fullmatch(r"\s*(\d{6})\s*", text)
    if postal_match:
        await route_postal(update, context, postal_match.group(1), update.message)
    elif context.user_data.get("market") == "hdb":
        await handle_hdb_search(update, context, text)
    else:
        await handle_property_search(update, context, text)
    return ConversationHandler.END


async def cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Search cancelled.")
    return ConversationHandler.END


# ── /mortgage Conversation ─────────────────────────────────────────────────────
#
# Two entry points: the /mortgage command (asks for everything) and the
# Affordability button on a property result (pre-fills the price, then asks for
# the rest). Each numeric step accepts "skip" (or "-") to take the default, so
# the whole flow can be a few taps when the property price is already known.

_SKIP_WORDS = {"skip", "-", "default", ""}


def _parse_money(text: str) -> float | None:
    """Parse a money/number entry: handles commas and k/m suffixes.

    e.g. "1.8m" → 1_800_000, "300k" → 300_000, "1,500,000" → 1_500_000.
    Returns None if it isn't a positive number.
    """
    t = text.strip().lower().replace(",", "").replace("$", "").replace("s$", "")
    mult = 1
    if t.endswith("m"):
        mult, t = 1_000_000, t[:-1]
    elif t.endswith("k"):
        mult, t = 1_000, t[:-1]
    try:
        val = float(t) * mult
    except ValueError:
        return None
    return val if val > 0 else None


async def mortgage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mortgage entry point — start from a blank slate and ask for the price."""
    context.user_data["mortgage"] = {}
    await update.message.reply_text(
        "🏦 *Mortgage & Affordability*\n\n"
        "What's the property price? (e.g. `1.8m`, `1800000`)\n\n"
        "_Send /cancel anytime to stop._",
        parse_mode="Markdown",
    )
    return MORTGAGE_PRICE


async def _prompt_for_price(message, context: ContextTypes.DEFAULT_TYPE):
    """Fallback when no per-band prices are available — ask the user to type one."""
    context.user_data["mortgage"] = {}
    await message.reply_text(
        "🏦 *Mortgage & Affordability*\n\n"
        "What's the property price? (e.g. `1.8m`, `1800000`)\n\n"
        "_Send /cancel anytime to stop._",
        parse_mode="Markdown",
    )
    return MORTGAGE_PRICE


async def mortgage_from_property(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affordability-button entry point — let the user pick a size band first.

    The chosen band's average transacted price seeds the rest of the flow.
    """
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1] if ":" in query.data else None
    band_prices = resolve_addr_band_prices(context, token)

    context.user_data["mortgage"] = {}
    if not band_prices:
        # Prices weren't stashed (e.g. bot restarted) — fall back to asking.
        return await _prompt_for_price(query.message, context)

    # One button per size band that has a price, in ascending size order.
    keyboard = [
        [InlineKeyboardButton(
            f"{SIZE_BANDS[idx]['label']}  ·  ~S${price:,.0f}",
            callback_data=f"mortgageband:{token}:{idx}",
        )]
        for idx, price in sorted(band_prices.items())
    ]
    await query.message.reply_text(
        "🏦 *Mortgage & Affordability*\n\n"
        "Which unit size do you want to base this on?\n"
        "_Each option uses that band's 12-month average price._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return MORTGAGE_BAND


async def mortgage_band_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Size-band picked — seed the flow with that band's average price."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    token = parts[1] if len(parts) > 1 else None
    idx = int(parts[2]) if len(parts) > 2 else None

    band_prices = resolve_addr_band_prices(context, token)
    price = band_prices.get(idx) if band_prices else None
    if not price:
        return await _prompt_for_price(query.message, context)

    context.user_data["mortgage"] = {"price": price}
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"📐 {SIZE_BANDS[idx]['label']} — avg price *S${price:,.0f}*\n\n"
        f"{_downpayment_prompt(price)}",
        parse_mode="Markdown",
    )
    return MORTGAGE_DOWNPAYMENT


def _downpayment_prompt(price: float) -> str:
    default = price * MIN_DOWN_PAYMENT_PCT
    return (
        f"How much is your *down payment*?\n"
        f"Enter a percent (e.g. `25%`) or an amount (e.g. `450k`).\n"
        f"_Send `skip` for the 25% minimum (S${default:,.0f})._"
    )


async def mortgage_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = _parse_money(update.message.text)
    if price is None:
        await update.message.reply_text(
            "⚠️ I couldn't read that. Send the price as a number, e.g. `1.8m` or `1800000`.",
            parse_mode="Markdown",
        )
        return MORTGAGE_PRICE
    context.user_data["mortgage"]["price"] = price
    await update.message.reply_text(_downpayment_prompt(price), parse_mode="Markdown")
    return MORTGAGE_DOWNPAYMENT


async def mortgage_downpayment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data["mortgage"]
    price = data["price"]
    text = update.message.text.strip().lower()

    if text in _SKIP_WORDS:
        down = price * MIN_DOWN_PAYMENT_PCT
    elif text.endswith("%"):
        try:
            pct = float(text[:-1].strip())
        except ValueError:
            pct = None
        if pct is None or pct < 0:
            await update.message.reply_text("⚠️ Enter a valid percent, e.g. `25%`.", parse_mode="Markdown")
            return MORTGAGE_DOWNPAYMENT
        down = price * pct / 100
    else:
        down = _parse_money(text)
        if down is None:
            await update.message.reply_text(
                "⚠️ Enter a percent (`25%`) or amount (`450k`), or `skip`.",
                parse_mode="Markdown",
            )
            return MORTGAGE_DOWNPAYMENT

    data["down_payment"] = min(down, price)  # can't put down more than the price
    await update.message.reply_text(
        f"Down payment: *S${data['down_payment']:,.0f}*\n\n"
        f"What *interest rate* (% p.a.)?\n"
        f"_Send `skip` for {DEFAULT_RATE_PCT}%._",
        parse_mode="Markdown",
    )
    return MORTGAGE_RATE


async def mortgage_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower().rstrip("%")
    if text in _SKIP_WORDS:
        rate = DEFAULT_RATE_PCT
    else:
        try:
            rate = float(text)
        except ValueError:
            rate = None
        if rate is None or rate < 0 or rate > 20:
            await update.message.reply_text(
                "⚠️ Enter a rate between 0 and 20, e.g. `2.6`, or `skip`.",
                parse_mode="Markdown",
            )
            return MORTGAGE_RATE
    context.user_data["mortgage"]["rate"] = rate
    await update.message.reply_text(
        f"Rate: *{rate:.2f}%*\n\n"
        f"What *loan tenure* in years?\n"
        f"_Send `skip` for {DEFAULT_TENURE_YEARS} years (max {MAX_TENURE_YEARS})._",
        parse_mode="Markdown",
    )
    return MORTGAGE_TENURE


async def mortgage_tenure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in _SKIP_WORDS:
        tenure = DEFAULT_TENURE_YEARS
    else:
        try:
            tenure = float(text)
        except ValueError:
            tenure = None
        if tenure is None or tenure <= 0:
            await update.message.reply_text(
                "⚠️ Enter a number of years, e.g. `25`, or `skip`.", parse_mode="Markdown"
            )
            return MORTGAGE_TENURE
        tenure = min(tenure, MAX_TENURE_YEARS)  # regulatory cap
    context.user_data["mortgage"]["tenure"] = tenure
    await update.message.reply_text(
        f"Tenure: *{tenure:.0f} years*\n\n"
        f"Last one — your *gross monthly income* (for the TDSR check)?\n"
        f"_Send `skip` to just see the income you'd need._",
        parse_mode="Markdown",
    )
    return MORTGAGE_INCOME


async def _send_mortgage_result(message, context: ContextTypes.DEFAULT_TYPE):
    """Compute and send the final summary from whatever is in user_data."""
    data = context.user_data["mortgage"]
    summary = mortgage_summary(
        price=data["price"],
        down_payment=data["down_payment"],
        annual_rate_pct=data["rate"],
        tenure_years=data["tenure"],
        monthly_income=data.get("income"),
        variable_income=data.get("variable_income", 0.0),
    )
    await message.reply_text(
        format_mortgage_summary(summary),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Search another property", callback_data="new_search")
        ]]),
    )
    return ConversationHandler.END


async def mortgage_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    income = None if text in _SKIP_WORDS else _parse_money(text)
    if text not in _SKIP_WORDS and income is None:
        await update.message.reply_text(
            "⚠️ Enter your monthly income, e.g. `12000`, or `skip`.", parse_mode="Markdown"
        )
        return MORTGAGE_INCOME

    context.user_data["mortgage"]["income"] = income
    # No income → no TDSR check to refine, so skip the variable-income question.
    if income is None:
        return await _send_mortgage_result(update.message, context)

    await update.message.reply_text(
        f"Income: *S${income:,.0f}/mo*\n\n"
        f"How much of that is *variable* (bonus / commission)?\n"
        f"_Variable income counts at 70% for TDSR (MAS 30% haircut). "
        f"Send `skip` if it's all fixed salary._",
        parse_mode="Markdown",
    )
    return MORTGAGE_VARIABLE


async def mortgage_variable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    variable = 0.0 if text in _SKIP_WORDS else _parse_money(text)
    if text not in _SKIP_WORDS and variable is None:
        await update.message.reply_text(
            "⚠️ Enter your variable income, e.g. `3000`, or `skip` if all fixed.",
            parse_mode="Markdown",
        )
        return MORTGAGE_VARIABLE

    income = context.user_data["mortgage"].get("income") or 0
    context.user_data["mortgage"]["variable_income"] = min(variable, income)
    return await _send_mortgage_result(update.message, context)


async def mortgage_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Mortgage calculation cancelled.")
    return ConversationHandler.END


# ── Callback Handlers ─────────────────────────────────────────────────────────

async def list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    development_name = query.data[len("search:"):]
    await query.edit_message_reply_markup(reply_markup=None)
    await handle_property_search(update, context, development_name, message=query.message)


async def fuzzy_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("fuzzy_yes:"):
        development_name = data[len("fuzzy_yes:"):]
        await query.edit_message_reply_markup(reply_markup=None)
        await handle_property_search(update, context, development_name, message=query.message)

    elif data.startswith("fuzzy_no:"):
        original_query = data[len("fuzzy_no:"):]
        alternatives = context.user_data.get(f"alt_{original_query}", [])
        if alternatives:
            keyboard = [
                [InlineKeyboardButton(name, callback_data=f"search:{name}")]
                for name in alternatives
            ]
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="fuzzy_cancel")])
            await query.edit_message_text(
                f"🔍 Closest matches for *{original_query}*:\n\nTap one to search:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.edit_message_text(
                f"No close matches found for *{original_query}*.\nTry typing the full name.",
                parse_mode="Markdown"
            )

    elif data == "fuzzy_cancel":
        await query.edit_message_text("Search cancelled.")


async def new_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    # When invoked from a (private) property result the callback carries that
    # property's token ("new_search:<token>"), letting us offer a "nearby"
    # search anchored to it alongside the market toggle. The bare "new_search"
    # (district list, /list, HDB results) has no such anchor.
    token = query.data.split(":", 1)[1] if ":" in query.data else None
    has_origin = bool(token and resolve_addr_key(context, token))

    rows = [
        [InlineKeyboardButton("🏢 Private", callback_data="market:private")],
        [InlineKeyboardButton("🏠 HDB", callback_data="market:hdb")],
    ]
    if has_origin:
        rows.append(
            [InlineKeyboardButton("📌 Search nearby (within 1km)", callback_data=f"nearby:{token}")]
        )
    await query.message.reply_text(
        "🏠 Which market?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the 🏢 Private / 🏠 HDB toggle. Sets the market for subsequent
    free-text routing and shows that market's discovery options."""
    query = update.callback_query
    await query.answer()

    market = query.data.split(":")[1]
    context.user_data["market"] = market
    await query.edit_message_reply_markup(reply_markup=None)

    if market == "hdb":
        await query.message.reply_text(
            "🏠 *HDB resale* — how would you like to search?",
            parse_mode="Markdown",
            reply_markup=build_hdb_mode_keyboard(),
        )
    else:
        await query.message.reply_text(
            "🏢 *Private property* — how would you like to search?",
            parse_mode="Markdown",
            reply_markup=build_search_mode_keyboard(),
        )


async def hdb_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle HDB discovery mode (browse-by-town vs block/street lookup)."""
    query = update.callback_query
    await query.answer()
    context.user_data["market"] = "hdb"
    mode = query.data.split(":")[1]

    if mode == "town":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "🗺 *Select a town:*",
            parse_mode="Markdown",
            reply_markup=build_hdb_town_keyboard(),
        )
    elif mode == "lookup":
        await query.edit_message_text(
            "🔍 Enter a block + street, e.g. *406 Ang Mo Kio Ave 10*\n"
            "_(or just a street to see all its blocks)_",
            parse_mode="Markdown",
        )


async def hdb_town_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle an HDB town-grid selection → that town's resale overview."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    idx = int(query.data.split(":")[1])
    await _send_hdb_town_overview(query.message, context, idx)


async def hdb_street_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a disambiguation street button → that street's summary."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    street = resolve_hdb_target(context, query.data.split(":", 1)[1])
    if not street:
        await query.message.reply_text("⚠️ Could not identify street. Please search again.")
        return
    await _send_hdb_street_summary(query.message, context, street)


async def hdb_block_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a block button (under a street summary) → that block's detail."""
    query = update.callback_query
    await query.answer()
    payload = resolve_hdb_target(context, query.data.split(":", 1)[1])
    if not payload or "|" not in payload:
        await query.message.reply_text("⚠️ Could not identify block. Please search again.")
        return
    block, street = payload.split("|", 1)
    await _send_hdb_block_detail(query.message, context, block, street)


async def nearby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 📌 Search nearby: private developments within 1km of the origin.

    Re-queries by project name at tap time (like Liquidity/PropertyGuru); the
    coordinate work lives in nearby.py (OneMap geocode + Mongo coords cache).
    """
    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1] if ":" in query.data else None
    addr_key = resolve_addr_key(context, token) if token else None
    if not addr_key:
        await query.message.reply_text("⚠️ Could not identify property. Please search again.")
        return

    project_name = addr_key.split("|", 1)[0]
    await query.edit_message_reply_markup(reply_markup=None)
    loading = await query.message.reply_text("📌 Finding properties within 1km...")
    try:
        result = nearby_for_project(project_name)
        await loading.delete()

        if "error" in result:
            await query.message.reply_text(f"⚠️ {result['error']}")
            return

        results = result["results"]
        if not results:
            await query.message.reply_text(
                f"No other private developments found within "
                f"{result['radius_m'] // 1000}km of *{project_name.title()}*.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔍 Search another property", callback_data=f"new_search:{token}")]]
                ),
            )
            return

        buttons = [
            [InlineKeyboardButton(
                f"{r['project'].title()} · {r['distance_m']}m",
                callback_data=f"search:{r['project']}",
            )]
            for r in results
        ]
        buttons.append([InlineKeyboardButton("🔍 Search another property", callback_data=f"new_search:{token}")])
        await query.message.reply_text(
            f"📌 *Within {result['radius_m'] // 1000}km of {project_name.title()}* "
            f"(District {result['district']}):\n\nTap one to see its transactions:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        logger.error(f"Nearby callback failed: {e}", exc_info=True)
        await loading.delete()
        await query.message.reply_text("⚠️ Something went wrong. Please try again.")


async def search_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search mode selection (name vs district)."""
    query = update.callback_query
    await query.answer()
    context.user_data["market"] = "private"

    mode = query.data.split(":")[1]

    if mode == "name":
        await query.edit_message_text(
            "🏠 Enter the property/development name — or a 6-digit postal code:"
        )

    elif mode == "district":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "📍 *Select an area:*",
            parse_mode="Markdown",
            reply_markup=build_district_keyboard(),
        )


async def district_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle district selection and show top 10 developments."""
    query = update.callback_query
    await query.answer()

    try:
        district = int(query.data.split(":")[1])
        await query.edit_message_reply_markup(reply_markup=None)

        loading = await query.message.reply_text(f"🔍 Loading developments in District {district}...")

        developments = get_top_developments_by_district(district, limit=10)
        text = format_district_results(district, developments, bot_username=context.bot.username)

        await loading.delete()

        reply_markup = None
        if developments:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 New Search", callback_data="new_search")]
            ])
        await query.message.reply_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    except Exception as e:
        logger.error(f"District callback failed: {e}", exc_info=True)
        await query.message.reply_text("⚠️ Something went wrong. Please try again.")


async def amenity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all amenity button taps: MRT, schools, malls, supermarkets, rental."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    amenity = parts[1]
    token = parts[2] if len(parts) > 2 else None

    addr_key = resolve_addr_key(context, token) if token else None
    if not addr_key:
        await query.message.reply_text("⚠️ Could not identify property. Please search again.")
        return

    # addr_key format: "PROJECT|STREET"
    if "|" in addr_key:
        project_name, street_address = addr_key.split("|", 1)
    else:
        project_name = addr_key
        street_address = addr_key

    display_name = project_name.title()

    # Geocode with project + street, not street alone. A long road (e.g.
    # "YIO CHU KANG ROAD") geocodes to an arbitrary midpoint far from the
    # actual development — Hundred Palms Residences (264 Yio Chu Kang Rd) was
    # resolving ~2.5km north next to Lentor MRT. The project name pins Google
    # to the building; for generic placeholder names it harmlessly falls back
    # to the street midpoint.
    if project_name and street_address and project_name != street_address:
        address = f"{project_name}, {street_address}"
    else:
        address = street_address if street_address else project_name

    loading = await query.message.reply_text("🔍 Fetching...")

    photo = None  # set by the trend branch when a PNG chart renders successfully

    try:
        if amenity == "rental":
            # Rental uses project name — indexed by development name in URA
            ura_result = search_property(project_name)
            # A still-under-construction development has no rental contracts of its
            # own — any rental match would be stale/wrong (e.g. an en-bloc'd
            # predecessor of the same name). Gate it here so the matcher never runs.
            # Completed developments — even new-sale-only ones with no resales yet —
            # are NOT gated and show their real rentals if any exist.
            if "error" not in ura_result and ura_result.get("under_construction"):
                text = (
                    f"🏠 *{project_name.title()}* is still under construction "
                    "(not yet completed) — there are no rental contracts for it yet.\n\n"
                    "_Rental & yield data will appear once the development TOPs "
                    "and units start getting leased._"
                )
            else:
                sale_prices = {}
                if "error" not in ura_result:
                    for band_label, txn in ura_result.get("bands", {}).items():
                        sale_prices[band_label] = {"price": txn.get("price")}
                rental_result = get_rental_by_band(project_name, sale_prices, street_address)
                text = format_rental(rental_result, development=project_name)
        elif amenity == "trend":
            # Price trend uses project name — indexed by development name in URA
            trend_result = price_trend(project_name)
            try:
                photo = render_price_trend_png(trend_result)
            except Exception as e:
                logger.warning(f"PSF trend chart render failed: {e}")
                photo = None
            # With a chart, the caption only needs the header/stat summary; the
            # per-period bars would just duplicate the PNG, so drop them.
            text = format_price_trend(trend_result, include_bars=photo is None)
        else:
            # Postal searches stash the exact OneMap coordinate — use it as the
            # origin so amenity selection and walk/transit times match the real
            # address; name searches fall back to geocoding `address` by name.
            coords = resolve_addr_coords(context, token)
            if coords:
                maps_result = get_nearby_info(address, coords[0], coords[1])
            else:
                maps_result = get_nearby_info(address)

            if amenity == "mrt":
                text = format_amenity_list(
                    maps_result.get("mrts", []),
                    f"🚇 *Nearest MRT Stations — {display_name}*",
                    "🚇 No MRT stations found within 2.5km"
                )
            elif amenity == "schools":
                schools = maps_result.get("schools", [])
                if schools:
                    lines = [f"🏫 *Nearest Primary Schools — {display_name}*", "─────────────────────"]
                    for i, s in enumerate(schools, 1):
                        moe_dist = (
                            f"{int(s['dist'])}m" if s["dist"] < 1000
                            else f"{s['dist']/1000:.1f}km"
                        )
                        walk_line = f"     🚶 {s['duration']} walk ({s['distance']})"
                        if s.get("transit_duration"):
                            transit_line = f"\n     🚌 ~{s['transit_duration']} ({s['transit_distance']}) by transit _(est. Tue 9am)_"
                            link_label = "Transit directions"
                        else:
                            transit_line = ""
                            link_label = "Walking directions"
                        lines.append(
                            f"  {i}. {s['name']} _(MOE: {moe_dist})_\n"
                            f"{walk_line}{transit_line}\n"
                            f"     [{link_label}]({s['maps_link']})"
                        )
                    lines += [
                        "",
                        "⚠️ _MOE distances are estimates. For borderline cases (~1km), "
                        "verify on the OneMap SchoolQuery website._"
                    ]
                    text = "\n".join(lines)
                else:
                    text = "🏫 No primary schools found within 2km"

            elif amenity == "malls":
                text = format_amenity_list(
                    maps_result.get("malls", []),
                    f"🛍️ *Nearest Shopping Malls — {display_name}*",
                    "🛍️ No shopping malls found within 2km"
                )
            elif amenity == "supermarkets":
                text = format_amenity_list(
                    maps_result.get("supermarkets", []),
                    f"🛒 *Nearest Supermarkets — {display_name}* _(within 1km)_",
                    "🛒 No major supermarkets found within 1km"
                )
            else:
                text = "Unknown amenity type."

        await loading.delete()
        # Re-attach the amenity menu to the result so the user can tap the
        # next button right here instead of scrolling back up. HDB blocks get
        # the location-only keyboard; private gets the full one.
        if token in context.user_data.get("hdb_tokens", set()):
            keyboard = build_hdb_amenity_keyboard(token)
        else:
            keyboard = build_amenity_keyboard(token)
        if photo:
            await query.message.reply_photo(
                photo=photo, caption=text, parse_mode="Markdown", reply_markup=keyboard
            )
        else:
            await query.message.reply_text(
                text, parse_mode="Markdown", disable_web_page_preview=True,
                reply_markup=keyboard,
            )

    except Exception as e:
        logger.error(f"Amenity callback failed: {e}", exc_info=True)
        await loading.delete()
        await query.message.reply_text("⚠️ Something went wrong. Please try again.")


async def liquidity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the 📊 Liquidity button: turnover / take-up rate per size band.

    Re-queries by project name at tap time (same pattern as the rental and
    trend buttons) — the computation needs the full transaction history plus
    pipeline and unit-count lookups, which is too bulky to stash per token.
    """
    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1] if ":" in query.data else None
    addr_key = resolve_addr_key(context, token) if token else None
    if not addr_key:
        await query.message.reply_text("⚠️ Could not identify property. Please search again.")
        return

    project_name = addr_key.split("|", 1)[0]
    loading = await query.message.reply_text("🔍 Crunching sales velocity...")
    try:
        result = liquidity_for_project(project_name)
        if "error" in result:
            text = f"⚠️ {result['error']}"
        else:
            text = format_liquidity_summary(result["summary"], result["development"])
        await loading.delete()
        # Same scroll-saving re-attach as the amenity buttons.
        await query.message.reply_text(
            text, parse_mode="Markdown", reply_markup=build_amenity_keyboard(token)
        )
    except Exception as e:
        logger.error(f"Liquidity callback failed: {e}", exc_info=True)
        await loading.delete()
        await query.message.reply_text("⚠️ Something went wrong. Please try again.")


async def propertyguru_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the 🟥 PropertyGuru button: per-bedroom search links.

    Re-queries the project name from the token (same pattern as Liquidity). No
    network call — we only build PropertyGuru search URLs (no API; links open
    PropertyGuru's own app/site), so the reply is a URL-button keyboard.
    """
    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1] if ":" in query.data else None
    addr_key = resolve_addr_key(context, token) if token else None
    if not addr_key:
        await query.message.reply_text("⚠️ Could not identify property. Please search again.")
        return

    project_name = addr_key.split("|", 1)[0]
    rows = [
        [
            InlineKeyboardButton(f"🛒 {label} · Sale", url=sale_url),
            InlineKeyboardButton(f"🔑 {label} · Rent", url=rent_url),
        ]
        for label, sale_url, rent_url in listing_links(project_name)
    ]
    rows.append([InlineKeyboardButton("🔍 Search another property", callback_data="new_search")])

    await query.message.reply_text(
        f"🟥 *{project_name.title()}* on PropertyGuru\n"
        "Tap a bedroom type to see live listings for sale or rent:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def route_postal(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       postal: str, msg) -> None:
    """Resolve a 6-digit postal code ONCE and auto-route to the right market,
    ignoring the toggle. A postal code is a single address: OneMap returns a
    BUILDING name for private developments and an empty building (block+road
    only) for HDB blocks — so the building name is the discriminator. This lets
    a postal code find both condo and HDB without the user picking a market."""
    looking = await msg.reply_text(
        f"🔍 Looking up postal code *{postal}*...", parse_mode="Markdown"
    )
    resolved = resolve_postal_code(postal)
    await looking.delete()
    if not resolved:
        await msg.reply_text(
            f"❌ Couldn't find any address for postal code *{postal}*.\n"
            "Please double-check the 6-digit code and try again.",
            parse_mode="Markdown",
        )
        return
    if resolved.get("building"):
        # Private development — feed the resolved building name + coord into the
        # normal private search (skips a second OneMap lookup).
        coords = None
        if resolved.get("lat") is not None and resolved.get("lng") is not None:
            coords = (resolved["lat"], resolved["lng"])
        await handle_property_search(
            update, context, resolved["building"].title(),
            message=msg, postal_coords=coords,
        )
    else:
        # No building name → HDB block (or landed/commercial, which the HDB flow
        # reports as "no resale on record").
        await _hdb_search_by_postal(msg, context, postal, resolved=resolved)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return
    # A bare 6-digit postal code is market-agnostic: auto-detect private vs HDB
    # regardless of the toggle, so a postal code finds both condo and HDB.
    postal_match = re.fullmatch(r"\s*(\d{6})\s*", text)
    if postal_match:
        await route_postal(update, context, postal_match.group(1), update.message)
        return
    # Other free text is routed by the active market toggle; private is the
    # default so existing name searches keep working unchanged.
    if context.user_data.get("market") == "hdb":
        await handle_hdb_search(update, context, text)
    else:
        await handle_property_search(update, context, text)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ Unrecognized command.\n\n"
        "Try:\n"
        "• /search — find a property\n"
        "• /list — see popular searches\n"
        "• /help — show all commands"
    )


# ── Core Search Logic ─────────────────────────────────────────────────────────

async def handle_property_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    development_name: str,
    message=None,
    postal_coords=None,
):
    msg = message or update.message

    # Postal-code search: a bare 6-digit code is resolved to its development
    # name via OneMap, then searched like any other name. Only free-text entry
    # can produce a postal code — deep links / list buttons always pass names.
    # When `postal_coords` is supplied the caller (route_postal) already resolved
    # the code and passes the building name + origin coord, so skip the lookup.
    postal_match = None if postal_coords else re.fullmatch(r"\s*(\d{6})\s*", development_name)
    if postal_match:
        postal = postal_match.group(1)
        looking = await msg.reply_text(
            f"🔍 Looking up postal code *{postal}*...", parse_mode="Markdown"
        )
        resolved = resolve_postal_code(postal)
        await looking.delete()
        if not resolved:
            await msg.reply_text(
                f"❌ Couldn't find any address for postal code *{postal}*.\n"
                "Please double-check the 6-digit code and try again.",
                parse_mode="Markdown",
            )
            return
        if not resolved.get("building"):
            road = resolved.get("road", "").title()
            where = f" ({road})" if road else ""
            await msg.reply_text(
                f"❌ Postal code *{postal}*{where} doesn't map to a private "
                "residential development — there's no building name on record "
                "(it may be a landed home, HDB block, or commercial address).\n\n"
                "Try searching by the development name instead.",
                parse_mode="Markdown",
            )
            return
        # OneMap building name becomes the search term; the loading message below
        # then shows the resolved development name.
        development_name = resolved["building"].title()
        if resolved.get("lat") is not None and resolved.get("lng") is not None:
            postal_coords = (resolved["lat"], resolved["lng"])

    loading_msg = await msg.reply_text(
        f"🔍 Searching for *{development_name}*...\nThis may take a few seconds.",
        parse_mode="Markdown",
    )

    try:
        # 1. URA transaction data
        ura_result = search_property(development_name)

        # 2a. Handle ambiguous result — multiple close matches, let user pick
        if ura_result.get("ambiguous"):
            candidates = ura_result["candidates"]
            keyboard = [
                [InlineKeyboardButton(c["project"].title(), callback_data=f"search:{c['project']}")]
                for c in candidates
            ]
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="fuzzy_cancel")])
            await loading_msg.delete()
            await msg.reply_text(
                "🔍 *Multiple matches found* — which one did you mean?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # 2b. Handle fuzzy match — ask user to confirm
        if "error" not in ura_result and ura_result.get("fuzzy_match"):
            matched_name = ura_result["fuzzy_match"]
            alternatives = ura_result.get("alternatives", [])
            if update.effective_user:
                context.user_data[f"alt_{development_name}"] = alternatives
            await loading_msg.delete()
            await msg.reply_text(
                f"⚠️ Did you mean *{matched_name.title()}*?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes", callback_data=f"fuzzy_yes:{matched_name}"),
                    InlineKeyboardButton("❌ No", callback_data=f"fuzzy_no:{development_name}"),
                ]])
            )
            return

        transaction_text = format_transactions(ura_result)

        # 3. Record search
        if "error" not in ura_result:
            record_search(
                user_id=get_user_id(msg),
                username=get_username(msg),
                query=development_name,
                resolved_name=ura_result.get("development", development_name),
            )

        # 4. Build address key for amenity buttons
        # Format: "PROJECT|STREET" — project for rental search, street for geocoding
        street = ura_result.get("street", "") if "error" not in ura_result else ""
        project = ura_result.get("development", development_name) if "error" not in ura_result else development_name
        # Stash full names behind a short token — full addr_key would overflow
        # Telegram's 64-byte callback_data limit for longer property names.
        addr_token = store_addr_key(context, f"{project}|{street}")
        # Postal searches carry the exact OneMap coordinate — stash it so the
        # amenity buttons skip Google geocoding and pin to the real address.
        if postal_coords:
            store_addr_coords(context, addr_token, *postal_coords)
        # Stash the per-band price map so the Affordability button can offer a
        # size-band picker. Prefer each band's 12-month average price; fall back
        # to its latest transacted price when there's no recent average.
        band_label_to_idx = {b["label"]: i for i, b in enumerate(SIZE_BANDS)}
        band_avg_price = ura_result.get("band_avg_price", {})
        band_prices = {}
        for label, txn in ura_result.get("bands", {}).items():
            idx = band_label_to_idx.get(label)
            if idx is None:
                continue
            price = band_avg_price.get(label, {}).get("avg_price") or txn.get("price")
            if price:
                band_prices[idx] = price
        if band_prices:
            store_addr_band_prices(context, addr_token, band_prices)

        # 5. Send transaction results
        await loading_msg.delete()
        await msg.reply_text(
            transaction_text, parse_mode="Markdown", disable_web_page_preview=True
        )

        # 6. Buttons — error state shows minimal, success shows all amenities
        if "error" in ura_result:
            await msg.reply_text(
                "💡 Tip: Use /list to see popular searches.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔍 Search another property", callback_data="new_search")
                ]])
            )
            return

        await msg.reply_text(
            "Tap to explore nearby amenities:",
            reply_markup=build_amenity_keyboard(addr_token)
        )

    except Exception as e:
        logger.error(f"Error searching '{development_name}': {e}", exc_info=True)
        await loading_msg.delete()
        await msg.reply_text(
            "⚠️ Something went wrong while fetching data. Please try again.",
            parse_mode="Markdown",
        )


# ── HDB search logic ──────────────────────────────────────────────────────────

# A New-Search button reused across HDB result messages.
_HDB_NEW_SEARCH = InlineKeyboardMarkup([[
    InlineKeyboardButton("🔍 Search another property", callback_data="new_search")
]])

# Cap block buttons under a street summary so the keyboard stays tappable.
MAX_HDB_BLOCK_BUTTONS = 12


async def _send_hdb_town_overview(message, context: ContextTypes.DEFAULT_TYPE, idx: int):
    """Render a town's per-flat-type resale overview (browse / t<NN> deep link)."""
    town = hdb.town_by_index(idx)
    if not town:
        await message.reply_text("⚠️ Unknown town. Tap New Search to start over.",
                                 reply_markup=_HDB_NEW_SEARCH)
        return
    result = hdb.town_overview(town)
    await message.reply_text(
        hdb.format_town_overview(result),
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=_HDB_NEW_SEARCH,
    )


def _geocode_hdb_block(block: str, street: str):
    """Resolve an HDB block to coordinates via OneMap. Tries the raw street
    then its abbreviation-expanded form (e.g. ST → STREET). None on miss."""
    seen = set()
    for q in (f"{block} {street}", f"{block} {hdb.expand_street(street)}"):
        if q in seen:
            continue
        seen.add(q)
        loc = geocode_building(q)
        if loc:
            return loc
    return None


async def _send_hdb_block_detail(message, context: ContextTypes.DEFAULT_TYPE,
                                 block: str, street: str, coords=None):
    """Render a block's per-flat-type detail and, when the block has a
    coordinate, attach the location-amenity keyboard (reusing the private
    amenity engine). `coords` (lat, lng) skips geocoding — supplied by the
    postal-code flow, which already has the exact OneMap coordinate."""
    result = hdb.block_detail(block, street)
    if "error" in result:
        await message.reply_text(hdb.format_block_detail(result), reply_markup=_HDB_NEW_SEARCH)
        return

    await message.reply_text(
        hdb.format_block_detail(result),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    # Resolve a coordinate, then stash it under an addr token; the amenity
    # buttons read it (skipping any geocode) exactly like the postal-code flow.
    # addr_key is "<display>|<street>" so the reused amenity_callback has a
    # sensible display name + street fallback.
    if coords:
        lat, lng = coords
    else:
        loc = _geocode_hdb_block(block, result["street"])
        if not loc:
            await message.reply_text(
                "_Amenity lookup unavailable — couldn't locate this block._",
                parse_mode="Markdown", reply_markup=_HDB_NEW_SEARCH,
            )
            return
        lat, lng = loc["lat"], loc["lng"]

    display = f"Block {block} {result['street'].title()}"
    token = store_addr_key(context, f"{display}|{result['street']}")
    store_addr_coords(context, token, lat, lng)
    # Mark the token HDB so the reused amenity_callback re-attaches the HDB
    # keyboard (location amenities only), not the private one.
    context.user_data.setdefault("hdb_tokens", set()).add(token)
    await message.reply_text(
        "Tap to explore nearby amenities:",
        reply_markup=build_hdb_amenity_keyboard(token),
    )


async def _send_hdb_street_summary(message, context: ContextTypes.DEFAULT_TYPE, street: str):
    """Render a street's per-flat-type aggregate plus a button per block."""
    result = hdb.street_summary(street)
    if "error" in result:
        await message.reply_text(hdb.format_street_summary(result), reply_markup=_HDB_NEW_SEARCH)
        return

    await message.reply_text(
        hdb.format_street_summary(result),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    # One button per block (most-transacted first), each routing to block detail.
    rows, row = [], []
    for block, count in result["blocks"][:MAX_HDB_BLOCK_BUTTONS]:
        token = store_hdb_target(context, f"{block}|{result['street']}")
        row.append(InlineKeyboardButton(f"Blk {block} ({count})", callback_data=f"hdbblk:{token}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔍 Search another property", callback_data="new_search")])
    await message.reply_text("Tap a block for its own transactions:",
                             reply_markup=InlineKeyboardMarkup(rows))


async def _hdb_search_by_postal(message, context: ContextTypes.DEFAULT_TYPE, postal: str,
                                resolved: dict | None = None):
    """Resolve a 6-digit postal code to an HDB block via OneMap (block + road +
    exact coord), then show that block's detail. The coord is reused for
    amenities, so there's no second geocode. `resolved` may be passed in by
    route_postal to skip the lookup (it already resolved the code)."""
    if resolved is None:
        looking = await message.reply_text(
            f"🔍 Looking up postal code *{postal}*...", parse_mode="Markdown"
        )
        resolved = resolve_postal_code(postal)
        await looking.delete()

    if not resolved or resolved.get("lat") is None:
        await message.reply_text(
            f"❌ Couldn't find any address for postal code *{postal}*.\n"
            "Please double-check the 6-digit code and try again.",
            parse_mode="Markdown", reply_markup=_HDB_NEW_SEARCH,
        )
        return

    block, road = resolved.get("block", ""), resolved.get("road", "")
    if not block or not road:
        await message.reply_text(
            f"❌ Postal code *{postal}* doesn't map to an HDB block.",
            parse_mode="Markdown", reply_markup=_HDB_NEW_SEARCH,
        )
        return

    # Match the OneMap block+road back to a block in the HDB resale data.
    resolution = hdb.resolve_query(f"{block} {road}")
    if resolution.get("kind") == "block":
        await _send_hdb_block_detail(
            message, context, resolution["block"], resolution["street"],
            coords=(resolved["lat"], resolved["lng"]),
        )
    else:
        await message.reply_text(
            f"❌ No HDB resale transactions on record for *Block {block} {road.title()}* "
            f"(postal *{postal}*).\nIt may be a private address — try the 🏢 Private market.",
            parse_mode="Markdown", reply_markup=_HDB_NEW_SEARCH,
        )


async def handle_hdb_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            query_text: str, message=None):
    """Route a free-text HDB block/street query via hdb.resolve_query."""
    msg = message or update.message

    # A bare 6-digit postal code resolves to a specific HDB block via OneMap
    # (same detection as the private flow), then routes to block detail.
    postal_match = re.fullmatch(r"\s*(\d{6})\s*", query_text)
    if postal_match:
        await _hdb_search_by_postal(msg, context, postal_match.group(1))
        return

    result = hdb.resolve_query(query_text)

    if "error" in result:
        await msg.reply_text(f"❌ {result['error']}", reply_markup=_HDB_NEW_SEARCH)
        return

    if result.get("ambiguous"):
        block = result.get("block")
        rows = []
        for street in result["candidates"]:
            if block:
                token = store_hdb_target(context, f"{block}|{street}")
                label, cb = f"Blk {block} · {street.title()}", f"hdbblk:{token}"
            else:
                token = store_hdb_target(context, street)
                label, cb = street.title(), f"hdbst:{token}"
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="fuzzy_cancel")])
        await msg.reply_text(
            "🔍 *Multiple matches found* — which one did you mean?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if result["kind"] == "block":
        await _send_hdb_block_detail(msg, context, result["block"], result["street"])
    else:  # street
        await _send_hdb_street_summary(msg, context, result["street"])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env file")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    search_conv = ConversationHandler(
        entry_points=[CommandHandler("search", search_command)],
        states={
            WAITING_FOR_PROPERTY_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_property_name)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_search)],
    )

    mortgage_conv = ConversationHandler(
        entry_points=[
            CommandHandler("mortgage", mortgage_command),
            CallbackQueryHandler(mortgage_from_property, pattern="^mortgage:"),
        ],
        states={
            MORTGAGE_BAND: [CallbackQueryHandler(mortgage_band_chosen, pattern="^mortgageband:")],
            MORTGAGE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, mortgage_price)],
            MORTGAGE_DOWNPAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, mortgage_downpayment)],
            MORTGAGE_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, mortgage_rate)],
            MORTGAGE_TENURE: [MessageHandler(filters.TEXT & ~filters.COMMAND, mortgage_tenure)],
            MORTGAGE_INCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, mortgage_income)],
            MORTGAGE_VARIABLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, mortgage_variable)],
        },
        fallbacks=[CommandHandler("cancel", mortgage_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(search_conv)
    app.add_handler(mortgage_conv)
    app.add_handler(CallbackQueryHandler(market_callback, pattern="^market:"))
    app.add_handler(CallbackQueryHandler(search_mode_callback, pattern="^search_mode:"))
    app.add_handler(CallbackQueryHandler(district_callback, pattern="^district:"))
    app.add_handler(CallbackQueryHandler(hdb_mode_callback, pattern="^hdbmode:"))
    app.add_handler(CallbackQueryHandler(hdb_town_callback, pattern="^hdbtown:"))
    app.add_handler(CallbackQueryHandler(hdb_street_callback, pattern="^hdbst:"))
    app.add_handler(CallbackQueryHandler(hdb_block_callback, pattern="^hdbblk:"))
    app.add_handler(CallbackQueryHandler(list_callback, pattern="^search:"))
    app.add_handler(CallbackQueryHandler(fuzzy_confirm_callback, pattern="^fuzzy_"))
    app.add_handler(CallbackQueryHandler(new_search_callback, pattern="^new_search"))
    app.add_handler(CallbackQueryHandler(nearby_callback, pattern="^nearby:"))
    app.add_handler(CallbackQueryHandler(amenity_callback, pattern="^amenity:"))
    app.add_handler(CallbackQueryHandler(liquidity_callback, pattern="^liquidity:"))
    app.add_handler(CallbackQueryHandler(propertyguru_callback, pattern="^pg:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
