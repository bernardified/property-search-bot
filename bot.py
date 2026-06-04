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
from maps import get_nearby_info, format_nearby
from storage import record_search, get_recent_searches
from cache_ura import force_refresh, cache_status
from cache_rental import force_refresh_rental, rental_cache_status
from rental import get_rental_by_band, format_rental
from onemap_mrt import build_mrt_cache
from schools_cache import get_schools_cache
from mrt_data import get_line_for_exit

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ConversationHandler state
WAITING_FOR_PROPERTY_NAME = 1


# ─── Command Handlers ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 *Singapore Private Property Search*\n\n"
        "Search for any non-landed private residential development to get:\n"
        "• Latest transacted prices by unit size\n"
        "• Distance to nearest MRT\n"
        "• Primary schools within 1km\n"
        "• Distance to nearest shopping mall\n"
        "• Last 12 months of rental data\n\n"
        "Just type a development name to get started.\n"
        "Example: `Marina One Residences` or `The Garden Residences`\n\n"
        "Commands:\n"
        "/search — search a property\n"
        "/list — most searched developments\n"
        "/refresh — update property data\n"
        "/help — show this message",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /refresh — refresh all caches."""
    msg = await update.message.reply_text("🔄 Refreshing all caches...\nThis may take ~1 minute.")

    lines = []

    # 1. URA transactions
    ura_ok = force_refresh()
    ura_status = cache_status()
    if ura_ok:
        lines.append(f"✅ URA transactions — {ura_status.get('projects', '?')} projects")
    else:
        lines.append("❌ URA transactions — refresh failed")

    await msg.edit_text("🔄 Refreshing all caches...\n" + "\n".join(lines))

    # 2. MRT stations (force by clearing cache meta then rebuilding)
    try:
        from pymongo import MongoClient
        from pymongo.server_api import ServerApi
        import os
        mongo_uri = os.getenv("MONGO_URI")
        if mongo_uri:
            client = MongoClient(mongo_uri, server_api=ServerApi("1"),
                                 serverSelectionTimeoutMS=10000)
            db = client["property_bot"]
            db["mrt_cache"].delete_many({})  # clear to force rebuild
        stations = build_mrt_cache()
        lines.append(f"✅ MRT stations — {len(stations)} stations")
    except Exception as e:
        logger.error(f"MRT refresh failed: {e}")
        lines.append("❌ MRT stations — refresh failed")

    await msg.edit_text("🔄 Refreshing all caches...\n" + "\n".join(lines))

    # 3. Primary schools (force by clearing cache meta then rebuilding)
    try:
        if mongo_uri:
            db["schools_cache"].delete_many({})  # clear to force rebuild
        schools = get_schools_cache()
        lines.append(f"✅ Primary schools — {len(schools)} schools")
    except Exception as e:
        logger.error(f"Schools refresh failed: {e}")
        lines.append("❌ Primary schools — refresh failed")

    # 4. Rental data
    rental_ok = force_refresh_rental()
    r_status = rental_cache_status()
    if rental_ok:
        lines.append(f"✅ Rental data — {r_status.get('projects', '?')} projects ({', '.join(r_status.get('quarters', []))})")
    else:
        lines.append("❌ Rental data — refresh failed")

    await msg.edit_text("🔄 All caches refreshed\n\n" + "\n".join(lines))


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the leaderboard as a clean menu of buttons."""
    # 1. Fetch the data
    searches = get_recent_searches(limit=10)
    
    if not searches:
        await update.message.reply_text("No search history found.")
        return

    # 2. Build the buttons (Leaderboard)
    # We create a list of lists: each inner list is one button row
    keyboard = []
    for search in searches:
        # Button text = just the name (no count!)
        # Callback data = to be handled by your property_search function
        keyboard.append([InlineKeyboardButton(search['name'], callback_data=f"search:{search['name']}")])

    # 3. Send one single message with the buttons attached
    await update.message.reply_text(
        "🏆 *Frequently Searched Properties*\nClick a property to quickly reload it:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── /search Conversation ────────────────────────────────────────────────────

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search with no args — prompt user to type the name."""
    if context.args:
        # User typed /search <name> directly — run immediately
        await handle_property_search(update, context, " ".join(context.args))
        return ConversationHandler.END

    await update.message.reply_text(
        "🏠 Please enter the property or development name:",
    )
    return WAITING_FOR_PROPERTY_NAME


async def received_property_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive property name after /search prompt."""
    development_name = update.message.text.strip()
    await handle_property_search(update, context, development_name)
    return ConversationHandler.END


async def cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Search cancelled.")
    return ConversationHandler.END


# ─── Callback Handlers ───────────────────────────────────────────────────────

async def list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button tap from /list."""
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("search:"):
        return
    development_name = data[len("search:"):]
    await query.edit_message_reply_markup(reply_markup=None)
    await handle_property_search(update, context, development_name, message=query.message)


async def fuzzy_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Yes/No/Alternative buttons for fuzzy match confirmation."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("fuzzy_yes:"):
        # User confirmed — run the search with the matched name
        development_name = data[len("fuzzy_yes:"):]
        await query.edit_message_reply_markup(reply_markup=None)
        await handle_property_search(update, context, development_name, message=query.message)

    elif data.startswith("fuzzy_no:"):
        # User said no — show alternatives
        original_query = data[len("fuzzy_no:"):]
        # Retrieve stored alternatives from context
        alternatives = context.user_data.get(f"alt_{original_query}", [])
        if alternatives:
            keyboard = [
                [InlineKeyboardButton(name, callback_data=f"search:{name}")]
                for name in alternatives
            ]
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="fuzzy_cancel")])
            await query.edit_message_text(
                f"🔍 Here are the closest matches for *{original_query}*:\n\nTap one to search:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.edit_message_text(
                f"No close matches found for *{original_query}*.\nTry typing the full development name.",
                parse_mode="Markdown"
            )

    elif data == "fuzzy_cancel":
        await query.edit_message_text("Search cancelled.")


async def new_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Search another property' button tap."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("🏠 Please enter the property or development name:")
    context.user_data["awaiting_search"] = True


# ─── Core Search Logic ───────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages as property searches."""
    development_name = update.message.text.strip()
    if not development_name:
        return
    # Clear awaiting flag if set
    context.user_data.pop("awaiting_search", None)
    await handle_property_search(update, context, development_name)


async def handle_property_search(update: Update, context: ContextTypes.DEFAULT_TYPE, development_name: str, message=None):
    """Run the full property search and reply with results."""
    msg = message or update.message
    loading_msg = await msg.reply_text(
        f"🔍 Searching for *{development_name}*...\nThis may take a few seconds.",
        parse_mode="Markdown",
    )

    try:
        # 1. URA transaction data
        ura_result = search_property(development_name)

        # 2. Handle fuzzy match — ask user to confirm before showing results
        if "error" not in ura_result and ura_result.get("fuzzy_match"):
            matched_name = ura_result["fuzzy_match"]
            alternatives = ura_result.get("alternatives", [])

            # Store alternatives for the No callback safely
            if update.effective_user:
                context.user_data[f"alt_{development_name}"] = alternatives

            keyboard = [
                [
                    InlineKeyboardButton("✅ Yes", callback_data=f"fuzzy_yes:{matched_name}"),
                    InlineKeyboardButton("❌ No", callback_data=f"fuzzy_no:{development_name}"),
                ]
            ]
            await loading_msg.delete()
            await msg.reply_text(
                f"⚠️ Did you mean *{matched_name.title()}*?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        transaction_text = format_transactions(ura_result)

        # 3. Record search
        if "error" not in ura_result:
            record_search(
                user_id=msg.from_user.id if hasattr(msg, "from_user") and msg.from_user else 0,
                username=msg.from_user.username if hasattr(msg, "from_user") and msg.from_user else "unknown",
                query=development_name,
                resolved_name=ura_result.get("development", development_name),
            )

        # 4. Resolve address for maps (store for later use via buttons)
        address = development_name
        if "error" not in ura_result:
            street = ura_result.get("street", "")
            project = ura_result.get("development", "")
            address = f"{project} {street}".strip() if street else project or development_name

        # 5. Send prices (or the 'No transactions found' error text)
        await loading_msg.delete()
        await msg.reply_text(
            transaction_text, parse_mode="Markdown", disable_web_page_preview=True
        )

        # 6. Handle Error State vs Success State for buttons
        if "error" in ura_result:
            # Property not found: Show ONLY the "Search another" button and prompt /list
            error_keyboard = [
                [InlineKeyboardButton("🔍 Search another property", callback_data="new_search")]
            ]
            await msg.reply_text(
                "💡 Tip: You can also use /list to see popular searches.",
                reply_markup=InlineKeyboardMarkup(error_keyboard)
            )
            return

        # Success State: Show all amenity buttons
        resolved = ura_result.get("development", development_name)
        addr_key = resolved[:40] 

        keyboard = [
            [
                InlineKeyboardButton("🚇 Nearest MRT", callback_data=f"amenity:mrt:{addr_key}"),
                InlineKeyboardButton("🏫 Primary Schools", callback_data=f"amenity:schools:{addr_key}"),
            ],
            [
                InlineKeyboardButton("🛍️ Shopping Malls", callback_data=f"amenity:malls:{addr_key}"),
                InlineKeyboardButton("🏠 Rental & Yield", callback_data=f"amenity:rental:{addr_key}"),
            ],
            [
                InlineKeyboardButton("🔍 Search another property", callback_data="new_search"),
            ],
        ]
        await msg.reply_text(
            "Tap to explore nearby amenities:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.error(f"Error handling search for '{development_name}': {e}", exc_info=True)
        await loading_msg.delete()
        await msg.reply_text(
            "⚠️ Something went wrong while fetching data. Please try again.",
            parse_mode="Markdown",
        )


# ─── Main ────────────────────────────────────────────────────────────────────

async def amenity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle MRT / Schools / Malls button taps — fetch and send that section."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)  # amenity:type:addr_key
    amenity = parts[1]
    addr_key = parts[2] if len(parts) > 2 else None

    if not addr_key:
        await query.message.reply_text("⚠️ Could not identify property. Please search again.")
        return

    # Resolve full address from URA cache using the development name key
    from cache_ura import get_ura_data
    all_results, _ = get_ura_data()
    address = addr_key  # fallback
    for project in all_results:
        pname = project.get("project", "").upper()
        if addr_key.upper() in pname or pname in addr_key.upper():
            street = project.get("street", "")
            address = f"{project.get('project', '')} {street}".strip()
            break

    loading = await query.message.reply_text("🔍 Fetching...")

    try:
        maps_result = get_nearby_info(address)

        if amenity == "mrt":
            mrts = maps_result.get("mrts", [])
            if mrts:
                lines = ["🚇 *Nearest MRT Stations*", "─────────────────────"]
                for i, mrt in enumerate(mrts, 1):
                    # 2. Fetch the line data dynamically
                    line_info = get_line_for_exit(mrt['name'])
                    
                    # 3. Inject it directly next to the name, and bold the name
                    lines.append(
                        f"  {i}. *{mrt['name']}*{line_info}\n"
                        f"     🚶 {mrt['duration']} ({mrt['distance']})\n"
                        f"     [Walking directions]({mrt['maps_link']})"
                    )
                text = "\n".join(lines)
            else:
                # 4. Changed this fallback text
                text = "🚇 No MRT stations found within the search radius"

        elif amenity == "schools":
            schools = maps_result.get("schools", [])
            if schools:
                lines = ["🏫 *Nearest Primary Schools*"]
                for i, school in enumerate(schools, 1):
                    # 1. Calculate the clean MOE straight-line string
                    moe_dist = f"{int(school['dist'])}m" if school['dist'] < 1000 else f"{school['dist']/1000:.1f}km"
                
                    # 2. Display both MOE distance and Walking distance
                    lines.append(
                        f"  {i}. {school['name']} (MOE: {moe_dist})\n"
                        f"     🚶 {school['duration']} walk ({school['distance']})\n"
                        f"     [Walking directions]({school['maps_link']})"
                    )
            
                lines.append("")
                lines.append("⚠️ *Note:* MOE distances are estimates based on center-points. For borderline cases (~1km), always verify on the OneMap SchoolQuery website.")
            
                text = "\n".join(lines)
                
            else:
                text = "🏫 No primary schools found within 1km"

        elif amenity == "malls":
            malls = maps_result.get("malls", [])
            if malls:
                lines = ["🛍️ *Nearest Shopping Malls*", "─────────────────────"]
                for i, m in enumerate(malls, 1):
                    lines.append(
                        f"  {i}. {m['name']}\n"
                        f"     🚶 {m['duration']} ({m['distance']})\n"
                        f"     [Walking directions]({m['maps_link']})"
                    )
                text = "\n".join(lines)
            else:
                text = "🛍️ No shopping malls found within 2km"

        elif amenity == "rental":
            # Get sale prices from URA cache for yield calculation
            from ura import search_property
            ura_result = search_property(address)
            sale_prices = {}
            if "error" not in ura_result:
                for band_label, txn in ura_result.get("bands", {}).items():
                    sale_prices[band_label] = {"price": txn.get("price")}

            rental_result = get_rental_by_band(address, sale_prices)
            text = format_rental(rental_result)

        else:
            text = "Unknown amenity type."

        await loading.delete()
        await query.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Amenity callback failed: {e}", exc_info=True)
        await loading.delete()
        await query.message.reply_text("⚠️ Something went wrong. Please try again.")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unknown slash commands by suggesting valid ones."""
    await update.message.reply_text(
        "⚠️ Unrecognized command.\n\n"
        "Try using:\n"
        "• /search — to find a property\n"
        "• /list — to see popular searches"
    )


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env file")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # /search conversation — prompts for name if none given
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
