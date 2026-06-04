import os
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
from ura import search_property, format_transactions
from maps import get_nearby_info
from storage import record_search, get_recent_searches
from cache_ura import force_refresh, cache_status
from cache_rental import force_refresh_rental, rental_cache_status
from rental import get_rental_by_band, format_rental
from onemap_mrt import build_mrt_cache
from schools_cache import get_schools_cache
from utils import get_mongo_db, clear_mongo_collection

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WAITING_FOR_PROPERTY_NAME = 1


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_amenity_keyboard(addr_key: str) -> InlineKeyboardMarkup:
    """Build the standard amenity button keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚇 Nearest MRT", callback_data=f"amenity:mrt:{addr_key}"),
            InlineKeyboardButton("🏫 Primary Schools", callback_data=f"amenity:schools:{addr_key}"),
        ],
        [
            InlineKeyboardButton("🛍️ Shopping Malls", callback_data=f"amenity:malls:{addr_key}"),
            InlineKeyboardButton("🛒 Supermarkets", callback_data=f"amenity:supermarkets:{addr_key}"),
        ],
        [
            InlineKeyboardButton("🏠 Rental & Yield", callback_data=f"amenity:rental:{addr_key}"),
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


def resolve_street_from_ura(addr_key: str) -> str:
    """
    Resolve street address from URA cache using development name key.
    Returns street if found, falls back to addr_key.
    Uses street only — project+street combination confuses Google geocoder.
    """
    from cache_ura import get_ura_data
    all_results, _ = get_ura_data()
    for project in all_results:
        pname = project.get("project", "").upper()
        if addr_key.upper() in pname or pname in addr_key.upper():
            street = project.get("street", "")
            return street if street else project.get("project", addr_key)
    return addr_key


def get_user_id(msg) -> int:
    return msg.from_user.id if hasattr(msg, "from_user") and msg.from_user else 0


def get_username(msg) -> str:
    return msg.from_user.username if hasattr(msg, "from_user") and msg.from_user else "unknown"


# ── Command Handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 *Singapore Private Property Search*\n\n"
        "Search for any non-landed private residential development to get:\n"
        "• Latest transacted prices by unit size\n"
        "• Distance to nearest MRT\n"
        "• Primary schools within 2km\n"
        "• Distance to nearest shopping mall\n"
        "• Nearest supermarkets\n"
        "• Rental prices & gross yield\n\n"
        "Just type a development name to get started.\n"
        "Example: `Marina One Residences` or `The Garden Residences`\n\n"
        "Commands:\n"
        "/list — most searched developments\n"
        "/refresh — update property data\n"
        "/help — show this message",
        parse_mode="Markdown",
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
    await update.message.reply_text("🏠 Please enter the property or development name:")
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
    await query.message.reply_text("🏠 Please enter the property or development name:")


async def amenity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all amenity button taps: MRT, schools, malls, supermarkets, rental."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    amenity = parts[1]
    addr_key = parts[2] if len(parts) > 2 else None

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
            sale_prices = {}
            if "error" not in ura_result:
                for band_label, txn in ura_result.get("bands", {}).items():
                    sale_prices[band_label] = {"price": txn.get("price")}
            rental_result = get_rental_by_band(project_name, sale_prices)
            text = format_rental(rental_result)
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

        # 2. Handle fuzzy match — ask user to confirm
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
        # Truncate each part to fit within Telegram's 64-char callback limit
        addr_key = f"{project[:28]}|{street[:28]}"

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
            reply_markup=build_amenity_keyboard(addr_key)
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
