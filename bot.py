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
from ura import search_property, format_transactions, price_trend, format_price_trend
from maps import get_nearby_info
from storage import record_search, get_recent_searches
from cache.cache_ura import force_refresh, cache_status
from cache.cache_rental import force_refresh_rental, rental_cache_status
from rental import get_rental_by_band, format_rental
from cache.onemap_mrt import build_mrt_cache
from cache.schools_cache import get_schools_cache
from utils import get_mongo_db, clear_mongo_collection
from district_search import (
    get_top_developments_by_district,
    format_district_results,
    district_full_name,
    district_button_label,
    NUM_DISTRICTS,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WAITING_FOR_PROPERTY_NAME = 1
WAITING_FOR_DISTRICT = 2


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_search_mode_keyboard() -> InlineKeyboardMarkup:
    """The two top-level search options shown by /start and /search."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search by Name", callback_data="search_mode:name")],
        [InlineKeyboardButton("📍 Browse by District", callback_data="search_mode:district")],
    ])


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
        m = re.fullmatch(r"d(\d+)r(\d+)", context.args[0].strip())
        if m:
            district, rank = int(m.group(1)), int(m.group(2))
            developments = get_top_developments_by_district(district, limit=10)
            if 1 <= rank <= len(developments):
                await handle_property_search(update, context, developments[rank - 1]["project"])
                return

    await update.message.reply_text(
        "🏠 *Singapore Private Property Search*\n\n"
        "Get instant data on private residential developments:\n"
        "• Latest transacted prices by unit size\n"
        "• Distance to nearest MRT\n"
        "• Primary schools within 2km\n"
        "• Distance to nearest shopping mall\n"
        "• Nearest supermarkets\n"
        "• Rental prices & gross yield\n\n"
        "*Two ways to search:*\n"
        "🔍 *By Name* — Search a specific development\n"
        "📍 *By District* — Browse top developments by area\n\n"
        "Pick an option below to get started.\n\n"
        "Commands:\n"
        "/search — find a property\n"
        "/list — most searched developments\n"
        "/refresh — update property data\n"
        "/help — show this message",
        parse_mode="Markdown",
        reply_markup=build_search_mode_keyboard(),
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
        "🏠 How would you like to search?",
        reply_markup=build_search_mode_keyboard(),
    )
    return WAITING_FOR_PROPERTY_NAME


async def received_property_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_property_search(update, context, update.message.text.strip())
    return ConversationHandler.END


async def cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Search cancelled.")
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
    keyboard = [
        [InlineKeyboardButton("🔍 Search by Name", callback_data="search_mode:name")],
        [InlineKeyboardButton("📍 Browse by District", callback_data="search_mode:district")],
    ]
    await query.message.reply_text(
        "🏠 How would you like to search?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def search_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search mode selection (name vs district)."""
    query = update.callback_query
    await query.answer()

    mode = query.data.split(":")[1]

    if mode == "name":
        await query.edit_message_text("🏠 Please enter the property or development name:")

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

    # Use street for geocoding, project name for rental search
    address = street_address if street_address else project_name

    loading = await query.message.reply_text("🔍 Fetching...")

    try:
        if amenity == "rental":
            # Rental uses project name — indexed by development name in URA
            ura_result = search_property(project_name)
            # New launches (only new-sale transactions) have no rental market of
            # their own. The rental matcher would otherwise fuzzy-match a nearby
            # completed development and show its contracts mislabeled — so skip.
            if "error" not in ura_result and not ura_result.get("has_secondary_market", True):
                text = (
                    "🏠 *Rental Prices & Yield*\n"
                    "─────────────────────\n\n"
                    f"_{project_name.title()} only has new-sale (developer) "
                    "transactions — it's a new launch with no resale or rental "
                    "market yet. Rental data will appear once the project is "
                    "completed and tenanted._"
                )
            else:
                sale_prices = {}
                if "error" not in ura_result:
                    for band_label, txn in ura_result.get("bands", {}).items():
                        sale_prices[band_label] = {"price": txn.get("price")}
                rental_result = get_rental_by_band(project_name, sale_prices)
                text = format_rental(rental_result)
            sale_prices = {}
            if "error" not in ura_result:
                for band_label, txn in ura_result.get("bands", {}).items():
                    sale_prices[band_label] = {"price": txn.get("price")}
            rental_result = get_rental_by_band(project_name, sale_prices)
            text = format_rental(rental_result)
        elif amenity == "trend":
            # Price trend uses project name — indexed by development name in URA
            text = format_price_trend(price_trend(project_name))
        else:
            maps_result = get_nearby_info(address)

            if amenity == "mrt":
                text = format_amenity_list(
                    maps_result.get("mrts", []),
                    "🚇 *Nearest MRT Stations*",
                    "🚇 No MRT stations found within 2.5km"
                )
            elif amenity == "schools":
                schools = maps_result.get("schools", [])
                if schools:
                    lines = ["🏫 *Nearest Primary Schools*", "─────────────────────"]
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
                    "🛍️ *Nearest Shopping Malls*",
                    "🛍️ No shopping malls found within 2km"
                )
            elif amenity == "supermarkets":
                text = format_amenity_list(
                    maps_result.get("supermarkets", []),
                    "🛒 *Nearest Supermarkets* _(within 1km)_",
                    "🛒 No major supermarkets found within 1km"
                )
            else:
                text = "Unknown amenity type."

        await loading.delete()
        await query.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Amenity callback failed: {e}", exc_info=True)
        await loading.delete()
        await query.message.reply_text("⚠️ Something went wrong. Please try again.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    development_name = update.message.text.strip()
    if not development_name:
        return
    await handle_property_search(update, context, development_name)


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
    message=None
):
    msg = message or update.message
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(search_conv)
    app.add_handler(CallbackQueryHandler(search_mode_callback, pattern="^search_mode:"))
    app.add_handler(CallbackQueryHandler(district_callback, pattern="^district:"))
    app.add_handler(CallbackQueryHandler(list_callback, pattern="^search:"))
    app.add_handler(CallbackQueryHandler(fuzzy_confirm_callback, pattern="^fuzzy_"))
    app.add_handler(CallbackQueryHandler(new_search_callback, pattern="^new_search$"))
    app.add_handler(CallbackQueryHandler(amenity_callback, pattern="^amenity:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
