import asyncio

import pytest
from telegram.error import InvalidToken

from bridge import bot


class _FakeUpdater:
    def __init__(self, task):
        self._Updater__polling_task = task


class _FakeApp:
    def __init__(self, task, running=True):
        self.updater = _FakeUpdater(task)
        self.running = running
        self.stopped = False

    def stop_running(self):
        self.stopped = True


async def _dies():
    raise InvalidToken("Unauthorized")


async def _polls_forever():
    await asyncio.Event().wait()


def test_watchdog_stops_app_when_polling_task_died():
    """The real incident: the token was revoked under a running instance,
    the library ended its polling task, and the process stayed up (and
    "active" in systemd) for weeks without receiving anything."""

    async def scenario():
        task = asyncio.ensure_future(_dies())
        await asyncio.wait([task])
        app = _FakeApp(task)
        await asyncio.wait_for(bot._watch_poller(app, interval=0.01), timeout=2)
        return app

    assert asyncio.run(scenario()).stopped is True


def test_watchdog_leaves_a_healthy_poller_alone():
    async def scenario():
        task = asyncio.ensure_future(_polls_forever())
        app = _FakeApp(task)
        watchdog = asyncio.ensure_future(bot._watch_poller(app, interval=0.01))
        await asyncio.sleep(0.1)
        still_watching = not watchdog.done()
        watchdog.cancel()
        task.cancel()
        await asyncio.wait([watchdog, task])
        return app, still_watching

    app, still_watching = asyncio.run(scenario())
    assert app.stopped is False
    assert still_watching is True


def test_watchdog_tolerates_polling_not_started_yet():
    # post_init runs before the updater creates its polling task.
    async def scenario():
        app = _FakeApp(None)
        watchdog = asyncio.ensure_future(bot._watch_poller(app, interval=0.01))
        await asyncio.sleep(0.05)
        watchdog.cancel()
        await asyncio.wait([watchdog])
        return app

    assert asyncio.run(scenario()).stopped is False


def test_main_exits_with_no_restart_status_on_invalid_token(monkeypatch):
    monkeypatch.setattr(bot, "load_config", lambda: {"bot_token": "123:abc"})

    def rejected(self, *args, **kwargs):
        raise InvalidToken("Unauthorized")

    monkeypatch.setattr(bot.Application, "run_polling", rejected)
    with pytest.raises(SystemExit) as excinfo:
        bot.main()
    assert excinfo.value.code == bot.EXIT_INVALID_TOKEN
