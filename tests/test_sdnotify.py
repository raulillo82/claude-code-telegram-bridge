import socket

from bridge import sdnotify


def test_notify_sends_datagram_to_notify_socket(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        server.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        sdnotify.notify("READY=1")
        assert server.recv(64) == b"READY=1"


def test_notify_is_a_noop_outside_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    sdnotify.notify("READY=1")


def test_notify_never_raises_on_a_dead_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone.sock"))
    sdnotify.notify("READY=1")
