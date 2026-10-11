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
import subprocess
import sys
import time
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import git_sync, permissions, projects, sdnotify, sync
from .claude_runner import run_claude
from .history_preview import get_last_assistant_message
from .state import BotState

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx logs the full request URL at INFO, which for Telegram's API includes
# the bot token (https://api.telegram.org/bot<TOKEN>/...) -- keep it out of
# the journal.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
BOT_TOKEN_GPG_PATH = Path.home() / ".credentials" / "telegram-bot-token.gpg"
TELEGRAM_MAX_CHARS = 4000

# Decrypting the token needs the GPG key unlocked in gpg-agent, which is a
# manual step (`gpg-unlock`) that can lag any (re)start by hours. Wait for
# it in-process instead of exiting: the bridge then comes up by itself the
# moment the key is unlocked, rather than hammering systemd's restart loop.
TOKEN_RETRY_INITIAL_SECONDS = 5
TOKEN_RETRY_MAX_SECONDS = 300

# Telegram rejecting the token is not something a restart can fix (it was
# revoked or rotated) -- exit with a status the systemd unit lists in
# RestartPreventExitStatus, so the service ends up visibly "failed" instead
# of looping. 78 is EX_CONFIG from sysexits.h.
EXIT_INVALID_TOKEN = 78

POLLER_CHECK_SECONDS = 30

BOT_COMMANDS = [
    BotCommand("start", "Show bridge status and quick help"),
    BotCommand("projects", "Pick the active project"),
    BotCommand("mode", "Show or set the permission mode (normal/flight)"),
    BotCommand("compact", "Compact the active project's conversation history"),
    BotCommand("clear", "Clear the active project's conversation history"),
]


def _load_bot_token(cfg: dict) -> str:
    """Resolve the bot token, preferring the centralized encrypted store.

    config.json's own "bot_token" (if set) still wins, for anyone who
    hasn't adopted the ~/.credentials/*.gpg convention. Otherwise decrypt
    it from there, which requires the user's GPG key to be unlocked
    (see the "Firma de commits" section of the global CLAUDE.md / `gpg-unlock`).
    While gpg can't decrypt it, keep retrying with backoff rather than
    failing: a locked key is the expected state after any reboot.
    """
    token = cfg.get("bot_token")
    if token:
        return token
    if not BOT_TOKEN_GPG_PATH.exists():
        raise RuntimeError(
            "No bot_token in config.json and no encrypted token at "
            f"{BOT_TOKEN_GPG_PATH}. Set one or the other."
        )
    delay = TOKEN_RETRY_INITIAL_SECONDS
    attempt = 0
    while True:
        attempt += 1
        result = subprocess.run(
            ["gpg", "--decrypt", str(BOT_TOKEN_GPG_PATH)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if not token:
                raise RuntimeError(f"{BOT_TOKEN_GPG_PATH} decrypted to an empty token.")
            return token
        # gpg's own wording for a locked key is localized and varies
        # ("No secret key", "Operation cancelled", ...), so any failure is
        # treated as "not unlocked yet" -- its last line goes to the log
        # for the cases where it's something else.
        reason = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
        logger.warning(
            "Could not decrypt the bot token (attempt %d, gpg exit %d: %s). "
            "Is the GPG key unlocked? Retrying in %ds.",
            attempt, result.returncode, reason[0], delay,
        )
        sdnotify.notify(
            f"STATUS=Waiting for the GPG key to be unlocked (attempt {attempt}, next in {delay}s)"
        )
        time.sleep(delay)
        delay = min(delay * 2, TOKEN_RETRY_MAX_SECONDS)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["projects_dir"] = os.path.expanduser(cfg["projects_dir"])
    # allowed_user_id must be an int to compare against Telegram's
    # update.effective_user.id; tolerate it being quoted as a string in
    # config.json, since that's an easy mistake to make by hand.
    cfg["allowed_user_id"] = int(cfg["allowed_user_id"])
    cfg["bot_token"] = _load_bot_token(cfg)
    # Optional two-host sync of non-git projects (see bridge/sync.py) --
    # absent sync_host means the feature is fully off.
    cfg.setdefault("sync_host", None)
    cfg["sync_remote_projects_dir"] = os.path.expanduser(
        cfg.get("sync_remote_projects_dir") or cfg["projects_dir"]
    )
    cfg.setdefault("sync_connect_timeout_seconds", 5)
    cfg.setdefault("sync_rsync_timeout_seconds", 120)
    # Hosts a remote-only git project may be cloned from (see
    # sync.is_allowed_clone_url).
    cfg.setdefault("sync_git_clone_hosts", ["github.com"])
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


def project_picker_markup(
    names: list[str],
    remote_only: list[str] | None = None,
    remote_git: list[str] | None = None,
) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(n, callback_data=f"proj:{n}")] for n in names]
    for n in remote_only or []:
        buttons.append([InlineKeyboardButton(f"{n} (remoto)", callback_data=f"projrem:{n}")])
    for n in remote_git or []:
        buttons.append([InlineKeyboardButton(f"{n} (remoto, git)", callback_data=f"projgit:{n}")])
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
    remote_only, remote_git = await sync.list_remote_only(cfg, set(names))
    if not names and not remote_only and not remote_git:
        await update.message.reply_text(f"No projects found under {cfg['projects_dir']}")
        return
    await update.message.reply_text(
        "Pick a project:", reply_markup=project_picker_markup(names, remote_only, remote_git)
    )


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


async def _run_claude_with_sync(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    cfg: dict,
    resolved: str,
    project_dir: str,
    text: str,
    extra_args: list[str],
    *,
    force_new_session: bool = False,
    skip_history_sync: bool = False,
):
    """Like _run_claude_with_typing, but also wraps the two-host content
    sync (always) and history sync (unless skip_history_sync -- used by
    the "Start new session" override, which must never pull a live
    session's transcript in from the other host), plus a plain `git
    pull --ff-only`/`git push` around git-managed projects (independent
    of the two-host feature -- runs regardless of sync_host). Returns
    (ClaudeResult, sync_warning), where sync_warning is a user-facing
    string or None."""
    state: BotState = context.bot_data["state"]
    sync_warning = None
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id))
    try:
        async with state.lock_for(resolved):
            pre = await sync.sync_project_with_remote(cfg, resolved, project_dir, "pull")
            if not pre.ok and not pre.skipped:
                sync_warning = f"(could not sync before: {pre.detail})"
            # Session history sync is best-effort in a stronger sense: "no
            # history yet" is the common case, not a failure, so it never
            # contributes to sync_warning.
            if not skip_history_sync:
                await sync.sync_history_with_remote(cfg, project_dir, "pull")
            git_pull = await git_sync.pull(project_dir)
            if not git_pull.ok and not git_pull.skipped:
                sync_warning = f"(could not git pull: {git_pull.detail})"
            try:
                result = await run_claude(project_dir, text, extra_args, force_new_session=force_new_session)
            finally:
                post = await sync.sync_project_with_remote(cfg, resolved, project_dir, "push")
                if not post.ok and not post.skipped:
                    sync_warning = f"(could not sync after: {post.detail})"
                if not skip_history_sync:
                    await sync.sync_history_with_remote(cfg, project_dir, "push")
                git_push = await git_sync.push_if_ahead(project_dir)
                if not git_push.ok and not git_push.skipped:
                    sync_warning = f"(could not git push: {git_push.detail})"
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task
    return result, sync_warning


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


# A genuine "nothing to compact" no-op returns in well under a second
# (observed: ~23ms). A real compaction invokes the model to summarize the
# conversation and can take tens of seconds to minutes -- but still prints
# no chat text on success, since the confirmation is a structured event,
# not an assistant reply. Above this threshold with empty text, assume it
# actually did the work rather than claim nothing happened.
SILENT_SUCCESS_DURATION_MS = 5000


def _compact_summary(result) -> str:
    if result.text not in ("", EMPTY_RESPONSE_MARKER):
        return result.text
    if result.duration_ms and result.duration_ms > SILENT_SUCCESS_DURATION_MS:
        seconds = result.duration_ms / 1000
        return (
            f"No summary text was printed, but it took {seconds:.0f}s of real work -- "
            "likely compacted successfully rather than finding nothing to do."
        )
    return "Nothing to compact yet."


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not is_authorized(update, cfg["allowed_user_id"]):
        return
    resolved_info = await _resolve_active_project_for_command(update, context)
    if resolved_info is None:
        return
    resolved, project_dir = resolved_info
    chat_id = update.message.chat_id
    await update.message.reply_text(
        f"[{resolved}] Compacting... this can take a couple of minutes on a large history."
    )
    result = await _run_claude_with_typing(context, chat_id, resolved, project_dir, "/compact", [])
    await send_long_message(update, f"[{resolved}] {_compact_summary(result)}")


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


def _mode_markup(current: str) -> InlineKeyboardMarkup:
    def label(name: str) -> str:
        return f"✅ {name}" if name == current else name

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label("normal"), callback_data="mode:normal"),
                InlineKeyboardButton(label("flight"), callback_data="mode:flight"),
            ]
        ]
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.bot_data["config"]
    if not is_authorized(update, cfg["allowed_user_id"]):
        return
    state: BotState = context.bot_data["state"]
    chat_id = update.message.chat_id

    if not context.args:
        current = state.get_mode(chat_id)
        await update.message.reply_text(
            f"Current mode: {current}\n\nTap to switch, or use /mode normal | /mode flight.",
            reply_markup=_mode_markup(current),
        )
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

    if data.startswith("mode:"):
        requested = data[len("mode:") :]
        state.set_mode(chat_id, requested)
        state.last_activity[chat_id] = time.time()
        await query.edit_message_text(
            f"Mode set to: {requested}", reply_markup=_mode_markup(requested)
        )
        return

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

    if data.startswith("projrem:"):
        name = data[len("projrem:") :]
        if state.is_materializing(name):
            await context.bot.send_message(chat_id, f"Already copying '{name}', hang on.")
            return
        live_pid = await sync.has_remote_live_session(cfg, name)
        if live_pid:
            await context.bot.send_message(
                chat_id,
                f"There is a live Claude Code session on '{name}' on the other host "
                f"(pid {live_pid}). Not copying it over right now.",
            )
            return
        project_dir = os.path.join(cfg["projects_dir"], name)
        state.start_materializing(name)
        await query.edit_message_text(f"Copying '{name}' from the other host...")
        try:
            result = await sync.materialize_remote_project(cfg, name, project_dir)
        finally:
            state.finish_materializing(name)
        if not result.ok:
            await context.bot.send_message(chat_id, f"Could not copy '{name}': {result.detail}")
            return
        state.set_active_project(chat_id, name)
        preview = get_last_assistant_message(project_dir)
        reply = f"Active project: {name}"
        if preview:
            reply += f"\n\nLast time you were here:\n{preview}"
        await context.bot.send_message(chat_id, reply)
        return

    if data.startswith("projgit:"):
        name = data[len("projgit:") :]
        if state.is_materializing(name):
            await context.bot.send_message(chat_id, f"Already cloning '{name}', hang on.")
            return
        # Cloning doesn't touch the other host's working tree, but it does
        # pull that project's session history, which a live session there
        # makes unsafe for the same reason as in "projrem:" above.
        live_pid = await sync.has_remote_live_session(cfg, name)
        if live_pid:
            await context.bot.send_message(
                chat_id,
                f"There is a live Claude Code session on '{name}' on the other host "
                f"(pid {live_pid}). Not cloning it right now.",
            )
            return
        project_dir = os.path.join(cfg["projects_dir"], name)
        state.start_materializing(name)
        await query.edit_message_text(f"Cloning '{name}' from its git remote...")
        try:
            result, notes = await sync.materialize_remote_git_project(cfg, name, project_dir)
        finally:
            state.finish_materializing(name)
        if not result.ok:
            await context.bot.send_message(chat_id, f"Could not clone '{name}': {result.detail}")
            return
        state.set_active_project(chat_id, name)
        reply = f"Active project: {name} (cloned from its git remote)"
        if notes:
            reply += "\n\nNot included, still only on the other host:\n" + "\n".join(
                f"- {note}" for note in notes
            )
        preview = get_last_assistant_message(project_dir)
        if preview:
            reply += f"\n\nLast time you were here:\n{preview}"
        await context.bot.send_message(chat_id, reply[:TELEGRAM_MAX_CHARS])
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

    if data == "newsession_no":
        state.pending_new_session.pop(chat_id, None)
        await query.edit_message_text("Cancelled.")
        return

    if data.startswith("newsession:"):
        name = data[len("newsession:") :]
        pending = state.pending_new_session.pop(chat_id, None)
        if pending is None or pending[0] != name:
            await query.edit_message_text("That request has expired. Send the message again.")
            return
        _, text = pending
        project_dir = os.path.join(cfg["projects_dir"], name)
        mode = state.check_and_touch(chat_id, cfg["flight_mode_idle_minutes"])
        extra_args = permissions.build_claude_args(mode)
        await query.edit_message_text(f"[{name}] Starting a new session...")
        result, sync_warning = await _run_claude_with_sync(
            context, chat_id, cfg, name, project_dir, text, extra_args,
            force_new_session=True, skip_history_sync=True,
        )
        reply = f"[{name}] {result.text}"
        if result.permission_denied and mode == "normal":
            reply += "\n\n(Looks like normal mode blocked something. Try /mode flight and resend if needed.)"
        if sync_warning:
            reply += f"\n\n{sync_warning}"
        for i in range(0, len(reply), TELEGRAM_MAX_CHARS):
            await context.bot.send_message(chat_id, reply[i : i + TELEGRAM_MAX_CHARS])
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

    if sync.sync_enabled(cfg):
        # A live session on the *other* host is not just a races risk here:
        # history sync would pull in its transcript, and --continue always
        # resumes the most-recently-modified one, so it could hijack and
        # continue that live conversation instead of starting fresh. Unlike
        # a local live session, this is offered a way around it, since the
        # user may have no other way to reach that host right now (e.g.
        # mid-flight) to close the session themselves.
        remote_pid = await sync.has_remote_live_session(cfg, resolved)
        if remote_pid:
            state.pending_new_session[chat_id] = (resolved, text)
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Start new session", callback_data=f"newsession:{resolved}"),
                        InlineKeyboardButton("Cancel", callback_data="newsession_no"),
                    ]
                ]
            )
            await update.message.reply_text(
                f"[{resolved}] There is a live Claude Code session on this project on the "
                f"other host (pid {remote_pid}). Syncing now could hijack that conversation. "
                "Start a fresh, separate session here instead?",
                reply_markup=markup,
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

    result, sync_warning = await _run_claude_with_sync(
        context, chat_id, cfg, resolved, project_dir, text, extra_args
    )

    reply = f"[{resolved}] {result.text}"
    if result.permission_denied and mode == "normal":
        reply += "\n\n(Looks like normal mode blocked something. Try /mode flight and resend if needed.)"
    if sync_warning:
        reply += f"\n\n{sync_warning}"

    await send_long_message(update, reply)


def _polling_task(application: Application):
    # python-telegram-bot exposes no public handle on (or callback for) the
    # death of its polling task, hence the name-mangled private attribute.
    # Returns None if a future version renames it, which just disables the
    # watchdog below.
    return getattr(application.updater, "_Updater__polling_task", None)


async def _watch_poller(application: Application, interval: float = POLLER_CHECK_SECONDS) -> None:
    """Stop the application if its polling task has died.

    On an invalid token (and on any unexpected exception) the library ends
    the polling task but leaves the process running: it never receives
    another update, yet systemd keeps reporting it as active. That went
    unnoticed for weeks once, after the token was rotated under a running
    instance. Stopping makes run_polling re-raise whatever killed the task,
    so main() can exit with the right status.
    """
    while True:
        await asyncio.sleep(interval)
        task = _polling_task(application)
        if task is not None and task.done() and application.running:
            logger.critical("The Telegram polling task has died; shutting down.")
            application.stop_running()
            return


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)
    application.bot_data["poller_watchdog"] = asyncio.create_task(_watch_poller(application))
    # Getting here means Telegram accepted the token (initialize() calls
    # getMe), so "active" in systemd now implies a working token.
    sdnotify.notify("READY=1\nSTATUS=Polling Telegram")


async def _post_stop(application: Application) -> None:
    watchdog = application.bot_data.pop("poller_watchdog", None)
    if watchdog is not None:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog


def main() -> None:
    cfg = load_config()
    state = BotState()

    app = (
        Application.builder()
        .token(cfg["bot_token"])
        .post_init(_post_init)
        .post_stop(_post_stop)
        .build()
    )
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
    try:
        # bootstrap_retries=-1: keep retrying while the network isn't up yet
        # at boot (an invalid token still aborts immediately).
        app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)
    except InvalidToken:
        logger.critical(
            "Telegram rejected the bot token (revoked or rotated?). Not restarting: "
            "store the current token and start the service again."
        )
        sdnotify.notify("STATUS=Telegram rejected the bot token; update it and start again")
        sys.exit(EXIT_INVALID_TOKEN)


if __name__ == "__main__":
    main()
