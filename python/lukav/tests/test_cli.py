"""CLI argument parsing tests. These don't boot a real server — they
exercise the parser and the --check path."""
from __future__ import annotations

from lukav.cli import main


def test_check_mode_returns_zero(capsys):
    assert main(["--check"]) == 0
    captured = capsys.readouterr()
    assert "lukav app OK" in captured.out


def test_window_flag_falls_back_when_pywebview_missing(capsys, monkeypatch):
    # Force browser-fallback path by intercepting --no-open + --window.
    # We also block the actual uvicorn run so the test stays fast.
    import lukav.cli as cli_mod
    monkeypatch.setattr(cli_mod, "have_pywebview", lambda: False,
                        raising=False)
    # Stub webbrowser.open to a no-op and uvicorn.run to raise SystemExit
    # so we exit before binding a port.
    import webbrowser, uvicorn
    monkeypatch.setattr(webbrowser, "open", lambda *a, **kw: True)

    class _StopServer(SystemExit):
        pass

    def _fake_run(*a, **kw):
        raise _StopServer(0)

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    try:
        main(["--window", "--port", "0"])
    except _StopServer:
        pass
    captured = capsys.readouterr()
    assert "pywebview" in captured.out.lower() or "native-window" in captured.out.lower()
