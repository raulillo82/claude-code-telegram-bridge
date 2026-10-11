"""Minimal sd_notify(3) client, so the systemd unit can be Type=notify
without pulling in a dependency for one datagram.

A no-op when not started by systemd (NOTIFY_SOCKET unset), e.g. when
running `python run.py` by hand.
"""

import logging
import os
import socket

logger = logging.getLogger(__name__)


def notify(message: str) -> None:
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        # Abstract namespace socket: systemd spells the leading NUL as "@".
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), path)
    except OSError as exc:
        # Status reporting must never take the bridge down with it.
        logger.warning("Could not notify systemd (%s): %s", message, exc)
