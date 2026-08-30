"""Telegram <-> Claude Code bridge.

Meant as a fallback channel for flights (or any Wi-Fi restricted to a
messaging allowlist) where the native Claude Code app can't reach
Anthropic's servers, but Telegram traffic can. Long-polling, so it needs no
inbound port on the machine running it.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import permissions, projects
from .claude_runner import run_claude
from .history_preview import get_last_assistant_message
from .state import BotState

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
TELEGRAM_MAX_CHARS = 4000

BOT_COMMANDS = [
    BotCommand("start", "Show bridge status and quick help"),
    BotCommand("projects", "Pick the active project"),
    BotCommand("mode", "Show or set the permission mode (normal/flight)"),
    BotCommand("compact", "Compact the active project's conversation history"),
    BotCommand("clear", "Clear the active project's conversation history"),
]


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["projects_dir"] = os.path.expanduser(cfg["projects_dir"])
    # allowed_user_id must be an int to compare against Telegram's
    # update.effective_user.id; tolerate it being quoted as a string in
    # config.json, since that's an easy mistake to make by hand.
    cfg["allowed_user_id"] = int(cfg["allowed_user_id"])
    return cfg


def is_authorized(update: Update, allowed_user_id: int) -> bool:
    return update.effective_user is not None and update.effective_user.id == allowed_user_id


async def _keep_typing(bot, chat_id: int) -> None:
    """Refresh Telegram's "typing..." indicator every few seconds.

    Telegram clears it after ~5s on its own, so a single send_chat_action
    before a long-running `claude -p` call (which can easily take minutes)
    leaves the chat looking idle well before the reply actually arrives.
    """
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def send_long_message(update: Update, text: str) -> None:
    if not text:
        text = "(empty response)"
    for i in range(0, len(text), TELEGRAM_MAX_CHARS):
        await update.message.reply_text(text[i : i + TELEGRAM_MAX_CHARS])


async def download_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE, project_dir: str) -> list[str]:
    message = update.message
    file_obj = None
    filename = None

    if message.document:
        file_obj = await context.bot.get_file(message.document.file_id)
        filename = message.document.file_name or f"file_{int(time.time())}"
    elif message.photo:
        largest = message.photo[-1]
        file_obj = await context.bot.get_file(largest.file_id)
        filename = f"photo_{int(time.time())}.jpg"

    if not file_obj:
        return []

    uploads_dir = os.path.join(project_dir, ".telegram_uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    dest = os.path.join(uploads_dir, filename)
    await file_obj.download_to_drive(dest)
    return [dest]


def project_picker_markup(names: list[str]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(n, callback_data=f"proj:{n}")] for n in names]
    return InlineKeyboardMarkup(buttons)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not is_authorized(update, cfg["allowed_user_id"]):
        return
    await update.message.reply_text(
        "Claude Code bridge ready.\n"
        "/projects - pick the active project\n"
        "/mode [normal|flight] - show or set the permission mode\n"
        "/compact - compact the active project's history\n"
        "/clear - wipe the active project's history (asks to confirm)\n"
        "Send text (or a photo/document) to talk to the active project.\n"
        "Prefix a message with @project to send it elsewhere just once."
    )


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not is_authorized(update, cfg["allowed_user_id"]):
        return
    names = projects.list_projects(cfg["projects_dir"])
    if not names:
        await update.message.reply_text(f"No projects found under {cfg['projects_dir']}")
        return
    await update.message.reply_text("Pick a project:", reply_markup=project_picker_markup(names))


async def _run_claude_with_typing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    project_name: str,
    project_dir: str,
    prompt: str,
    extra_args: list[str],
):
    state: BotState = context.bot_data["state"]
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id))
    try:
        async with state.lock_for(project_name):
            return await run_claude(project_dir, prompt, extra_args)
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task


async def _resolve_active_project_for_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve the chat's active project for /compact and /clear.

    Unlike a regular message, there's no "create it?" flow here -- clearing
    or compacting a project that doesn't exist yet makes no sense. Returns
    (resolved_name, project_dir) or None after sending an explanatory
    reply.
    """
    cfg = context.bot_data["config"]
    state: BotState = context.bot_data["state"]
    chat_id = update.message.chat_id

    project_name = state.get_active_project(chat_id)
    if not project_name:
        await update.message.reply_text("No active project. Use /projects first.")
        return None

    resolved, info = projects.resolve_project(project_name, cfg["projects_dir"])
    if resolved is None:
        if info:
            await update.message.reply_text(
                f"Active project '{project_name}' is ambiguous, matches: {', '.join(info)}."
            )
        else:
            await update.message.reply_text(f"Active project '{project_name}' no longer exists.")
        return None

    project_dir = os.path.join(cfg["projects_dir"], resolved)
    live_pid = projects.has_live_session(project_dir)
    if live_pid:
        await update.message.reply_text(
            f"[{resolved}] There is a live Claude Code session on this project right now "
            f"(pid {live_pid}). Not touching it, to avoid two processes racing on the same history."
        )
        return None

    return resolved, project_dir


EMPTY_RESPONSE_MARKER = "(empty response)"


def _or_fallback(text: str, fallback: str) -> str:
    """`claude -p` prints nothing for /compact or /clear when there was no
    history to act on -- that's a normal outcome, not a failure, so swap in
    a message that says so instead of the generic empty-response marker."""
    return fallback if text in ("", EMPTY_RESPONSE_MARKER) else text


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not is_authorized(update, cfg["allowed_user_id"]):
        return
    resolved_info = await _resolve_active_project_for_command(update, context)
    if resolved_info is None:
        return
    resolved, project_dir = resolved_info
    chat_id = update.message.chat_id
    result = await _run_claude_with_typing(context, chat_id, resolved, project_dir, "/compact", [])
    text = _or_fallback(result.text, "Nothing to compact yet.")
    await send_long_message(update, f"[{resolved}] {text}")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not is_authorized(update, cfg["allowed_user_id"]):
        return
    resolved_info = await _resolve_active_project_for_command(update, context)
    if resolved_info is None:
        return
    resolved, _ = resolved_info
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Clear it", callback_data=f"clear_yes:{resolved}"),
                InlineKeyboardButton("Cancel", callback_data="clear_no"),
            ]
        ]
    )
    await update.message.reply_text(
        f"Clear conversation history for '{resolved}'? This can't be undone.", reply_markup=markup
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not is_authorized(update, cfg["allowed_user_id"]):
        return
    state: BotState = context.bot_data["state"]
    chat_id = update.message.chat_id

    if not context.args:
        await update.message.reply_text(f"Current mode: {state.get_mode(chat_id)}")
        return

    requested = context.args[0].lower()
    if requested not in ("normal", "flight"):
        await update.message.reply_text("Usage: /mode normal | /mode flight")
        return

    state.set_mode(chat_id, requested)
    state.last_activity[chat_id] = time.time()
    await update.message.reply_text(f"Mode set to: {requested}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    query = update.callback_query
    if not is_authorized(update, cfg["allowed_user_id"]):
        await query.answer()
        return
    await query.answer()

    state: BotState = context.bot_data["state"]
    chat_id = query.message.chat_id
    data = query.data

    if data.startswith("proj:"):
        name = data[len("proj:") :]
        state.set_active_project(chat_id, name)
        project_dir = os.path.join(cfg["projects_dir"], name)
        preview = get_last_assistant_message(project_dir)
        reply = f"Active project: {name}"
        if preview:
            reply += f"\n\nLast time you were here:\n{preview}"
        await query.edit_message_text(reply)
        return

    if data == "create_no":
        state.pending_create.pop(chat_id, None)
        await query.edit_message_text("Cancelled.")
        return

    if data.startswith("create_yes:"):
        name = data[len("create_yes:") :]
        state.pending_create.pop(chat_id, None)
        project_dir = os.path.join(cfg["projects_dir"], name)
        os.makedirs(project_dir, exist_ok=True)
        state.set_active_project(chat_id, name)
        await query.edit_message_text(
            f"Created '{name}' and set it active. Send your message again."
        )
        return

    if data == "clear_no":
        await query.edit_message_text("Cancelled.")
        return

    if data.startswith("clear_yes:"):
        name = data[len("clear_yes:") :]
        project_dir = os.path.join(cfg["projects_dir"], name)
        live_pid = projects.has_live_session(project_dir)
        if live_pid:
            await query.edit_message_text(
                f"[{name}] There is a live Claude Code session on this project now "
                f"(pid {live_pid}). Not clearing."
            )
            return
        await query.edit_message_text(f"[{name}] Clearing...")
        result = await _run_claude_with_typing(context, chat_id, name, project_dir, "/clear", [])
        text = _or_fallback(result.text, "Cleared.")
        await context.bot.send_message(chat_id, f"[{name}] {text}")
        return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not is_authorized(update, cfg["allowed_user_id"]):
        return

    message = update.message
    if message is None:
        return

    state: BotState = context.bot_data["state"]
    chat_id = message.chat_id

    text = message.text or message.caption or ""
    if not text and not message.photo and not message.document:
        await update.message.reply_text("Unsupported message type (text, photos, and documents only).")
        return

    override_project = None
    if text.startswith("@"):
        rest = text[1:]
        if " " in rest:
            tag, rest = rest.split(" ", 1)
        else:
            tag, rest = rest, ""
        override_project = tag
        text = rest

    project_name = override_project or state.get_active_project(chat_id)
    if not project_name:
        await update.message.reply_text("No active project. Use /projects first.")
        return

    resolved, info = projects.resolve_project(project_name, cfg["projects_dir"])
    if resolved is None:
        if info:
            await update.message.reply_text(
                f"'{project_name}' is ambiguous, matches: {', '.join(info)}. Be more specific."
            )
        else:
            state.pending_create[chat_id] = project_name
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Create it", callback_data=f"create_yes:{project_name}"),
                        InlineKeyboardButton("Cancel", callback_data="create_no"),
                    ]
                ]
            )
            await update.message.reply_text(
                f"Project '{project_name}' does not exist. Create it?", reply_markup=markup
            )
        return

    project_dir = os.path.join(cfg["projects_dir"], resolved)

    live_pid = projects.has_live_session(project_dir)
    if live_pid:
        await update.message.reply_text(
            f"[{resolved}] There is already a live Claude Code session on this project "
            f"(pid {live_pid}, likely an interactive/rc session). Not sending, to avoid "
            "two processes writing to the same project at once."
        )
        return

    attachment_paths = await download_attachments(update, context, project_dir)
    if attachment_paths:
        text = (text + "\n\nAttached files:\n" + "\n".join(attachment_paths)).strip()

    if not text:
        await update.message.reply_text(f"[{resolved}] Attachment saved, but no instruction was given.")
        return

    mode = state.check_and_touch(chat_id, cfg["flight_mode_idle_minutes"])
    extra_args = permissions.build_claude_args(mode)

    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id))
    try:
        async with state.lock_for(resolved):
            result = await run_claude(project_dir, text, extra_args)
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task

    reply = f"[{resolved}] {result.text}"
    if result.permission_denied and mode == "normal":
        reply += "\n\n(Looks like normal mode blocked something. Try /mode flight and resend if needed.)"

    await send_long_message(update, reply)


async def _register_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def main() -> None:
    cfg = load_config()
    state = BotState()

    app = Application.builder().token(cfg["bot_token"]).post_init(_register_commands).build()
    app.bot_data["config"] = cfg
    app.bot_data["state"] = state

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    logger.info("Starting Claude Code Telegram bridge (long polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
