"""Native-window desktop launcher.

When --window is passed (or LUKAV_WINDOW=1 is set), Lukav tries to open
its UI in a chromeless pywebview window instead of the default browser
tab. If pywebview isn't installed, the launcher logs a friendly note
and falls back to the browser.

PyWebView is an optional dep — install with `pip install lukav[desktop]`.
On Linux it also needs `python3-gi` + `gir1.2-webkit2-4.1` (Debian/Ubuntu)
or equivalent. macOS and Windows ship with the underlying engines."""
from __future__ import annotations

import socket
import threading
import time
from typing import Optional


def have_pywebview() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    """Return True once `host:port` accepts a TCP connection, False on
    timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


def run_in_window(host: str, port: int, *, title: str = "Lukav") -> None:
    """Spin up a uvicorn thread, then open a pywebview window. Blocks
    until the window closes. Raises RuntimeError if pywebview missing —
    callers should catch and fall back to browser."""
    if not have_pywebview():
        raise RuntimeError("pywebview not installed")

    import uvicorn
    import webview

    from lukav.web.app import create_app

    app = create_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    def _serve() -> None:
        server.run()

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()

    if not wait_for_port(host, port, timeout=15.0):
        raise RuntimeError(f"server failed to come up on {host}:{port}")

    webview.create_window(title, f"http://{host}:{port}/",
                          width=1100, height=800, resizable=True)
    try:
        webview.start()      # blocks until the user closes the window
    finally:
        server.should_exit = True
        server_thread.join(timeout=3.0)
