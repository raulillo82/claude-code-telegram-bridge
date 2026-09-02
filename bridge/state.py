"""In-memory per-chat state: active project, permission mode, locks.

Deliberately not persisted to disk: this bridge is meant to be used in
short, occasional bursts (mainly on flights), so a bot restart losing the
active-project/mode selection is an acceptable trade-off for staying simple.
"""

import asyncio
import time

DEFAULT_MODE = "normal"


class BotState:
    def __init__(self) -> None:
        self.active_project: dict[int, str] = {}
        self.mode: dict[int, str] = {}
        self.last_activity: dict[int, float] = {}
        self.pending_create: dict[int, str] = {}
        # chat_id -> (resolved project name, message text) for a message
        # blocked by a live session on the *other* host, kept until the
        # user picks "Start new session" or "Cancel".
        self.pending_new_session: dict[int, tuple[str, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.materializing: set[str] = set()

    def get_active_project(self, chat_id: int) -> str | None:
        return self.active_project.get(chat_id)

    def set_active_project(self, chat_id: int, name: str) -> None:
        self.active_project[chat_id] = name

    def get_mode(self, chat_id: int) -> str:
        return self.mode.get(chat_id, DEFAULT_MODE)

    def set_mode(self, chat_id: int, mode: str) -> None:
        self.mode[chat_id] = mode

    def check_and_touch(self, chat_id: int, idle_minutes: float) -> str:
        """Call once per incoming message. Reverts flight->normal if the
        chat has been idle for longer than idle_minutes, using activity
        recorded *before* this message, then records this message's time."""
        now = time.time()
        previous = self.last_activity.get(chat_id, now)
        mode = self.get_mode(chat_id)
        if mode == "flight" and (now - previous) > idle_minutes * 60:
            self.set_mode(chat_id, DEFAULT_MODE)
            mode = DEFAULT_MODE
        self.last_activity[chat_id] = now
        return mode

    def lock_for(self, project_name: str) -> asyncio.Lock:
        if project_name not in self._locks:
            self._locks[project_name] = asyncio.Lock()
        return self._locks[project_name]

    def is_materializing(self, project_name: str) -> bool:
        return project_name in self.materializing

    def start_materializing(self, project_name: str) -> None:
        self.materializing.add(project_name)

    def finish_materializing(self, project_name: str) -> None:
        self.materializing.discard(project_name)
