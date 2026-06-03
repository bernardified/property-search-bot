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
        "• Walking distance to nearest MRT\n"
        "• Walking distance to nearest shopping mall\n\n"
        "Just type a development name to get started.\n"
        "Example: `The Sail` or `Pinnacle Duxton`\n\n"
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
    """Handle /refresh — manually refresh URA data cache."""
    status = cache_status()
    msg = await update.message.reply_text(
        f"🔄 Refreshing URA data cache...\n"
        f"Current cache: {status.get('status', 'unknown')} "
        f"({status.get('age_hours', '?')}h old, {status.get('projects', '?')} projects)\n"
        f"This takes ~15 seconds...",
    )
    success = force_refresh()
    new_status = cache_status()
    if success:
        await msg.edit_text(
            f"✅ Cache refreshed successfully\n"
            f"{new_status.get('projects', '?')} projects loaded "
            f"({new_status.get('size_mb', '?')}MB)"
        )
    else:
        await msg.edit_text("❌ Cache refresh failed. URA API may be unavailable.")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list — show most searched developments with clickable buttons."""
    results = get_recent_searches(limit=10)
    if not results:
        await update.message.reply_text(
            "📋 No searches recorded yet. Try searching for a development first!"
        )
        return

    lines = ["📋 *Most Searched Developments*", "_Tap any to search again_", "─────────────────────"]
    for i, item in enumerate(results, 1):
        count_str = f"{item['count']} search" if item['count'] == 1 else f"{item['count']} searches"
        lines.append(f"{i}. *{item['name']}*\n   {count_str} · last {item['last_searched']}")

    keyboard = [
        [InlineKeyboardButton(f"{i}. {item['name']}", callback_data=f"search:{item['name']}")]
        for i, item in enumerate(results, 1)
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── /search Conversation ────────────────────────────────────────────────────

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search with no args — prompt user to type the name."""
    if context.args:
        # User typed /search <name> directly — run immediately
        await handle_property_search(update, " ".join(context.args))
        return ConversationHandler.END

    await update.message.reply_text(
        "🏠 Please enter the property or development name:",
    )
    return WAITING_FOR_PROPERTY_NAME


async def received_property_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive property name after /search prompt."""
    development_name = update.message.text.strip()
    await handle_property_search(update, development_name)
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
    await handle_property_search(update, development_name, message=query.message)


async def fuzzy_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Yes/No/Alternative buttons for fuzzy match confirmation."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("fuzzy_yes:"):
        # User confirmed — run the search with the matched name
        development_name = data[len("fuzzy_yes:"):]
        await query.edit_message_reply_markup(reply_markup=None)
        await handle_property_search(update, development_name, message=query.message)

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


# ─── Core Search Logic ───────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages as property searches."""
    development_name = update.message.text.strip()
    if not development_name:
        return
    await handle_property_search(update, development_name)


async def handle_property_search(update: Update, development_name: str, message=None):
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

            # Store alternatives for the No callback
            update.effective_user and context.user_data.update({
                f"alt_{development_name}": alternatives
            }) if hasattr(update, 'effective_user') and update.effective_user else None

            # Store in a way accessible from callback
            if update.effective_user:
                from telegram.ext import ContextTypes as CT
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

        # 4. Maps lookup
        address = development_name
        if "error" not in ura_result:
            street = ura_result.get("street", "")
            project = ura_result.get("development", "")
            address = f"{project} {street}".strip() if street else project or development_name

        maps_result = get_nearby_info(address)
        nearby_text = format_nearby(maps_result)

        # 5. Send
        full_message = f"{transaction_text}\n\n{nearby_text}"
        await loading_msg.delete()
        await msg.reply_text(
            full_message, parse_mode="Markdown", disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Error handling search for '{development_name}': {e}", exc_info=True)
        await loading_msg.delete()
        await msg.reply_text(
            "⚠️ Something went wrong while fetching data. Please try again.",
            parse_mode="Markdown",
        )


# ─── Main ────────────────────────────────────────────────────────────────────

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
