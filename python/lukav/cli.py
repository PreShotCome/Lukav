"""Lukav launcher.

Usage:
  lukav                      # start server + open browser tab
  lukav --window             # open in a native pywebview window if installed,
                             # falls back to the browser tab
  python -m lukav --no-open  # start server only (used by e2e harness)
  python -m lukav --check    # construct the app and exit (smoke check)
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lukav")
    parser.add_argument("--host", default=os.environ.get("LUKAV_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("LUKAV_PORT", DEFAULT_PORT)),
    )
    parser.add_argument("--no-open", action="store_true",
                        help="Do not open a browser window.")
    parser.add_argument("--window", action="store_true",
                        default=_env_bool("LUKAV_WINDOW"),
                        help="Open in a native pywebview window. Falls "
                             "back to the browser if pywebview is not "
                             "installed.")
    parser.add_argument("--check", action="store_true",
                        help="Construct the app and exit (no server).")
    parser.add_argument("--check-llm", action="store_true",
                        help="Print which LLM backend Lukav would use, "
                             "then exit. Useful when extraction quality "
                             "looks wrong (e.g. you forgot LUKAV_LLM_BACKEND).")
    args = parser.parse_args(argv)

    from lukav.web.app import create_app

    if args.check_llm:
        import json as _json
        from lukav.llm import describe_default_backend
        info = describe_default_backend()
        print(_json.dumps(info, indent=2))
        if info.get("LLM_BACKEND_env_present_but_ignored"):
            print(
                "\nNote: a bare LLM_BACKEND env var is set in your shell "
                "(likely from Theo). Lukav intentionally ignores it; set "
                "LUKAV_LLM_BACKEND if you actually want a non-Claude "
                "backend here."
            )
        if info.get("resolved", "").startswith("none"):
            print(
                "\nLukav will run WITHOUT an LLM. Audit, letters, and "
                "the Plaid dashboard still work. Credit-report ingest "
                "and Phase-5 letter ingest will require you to fill the "
                "extracted fields manually."
            )
        return 0

    if args.check:
        app = create_app()
        print(f"lukav app OK; {len(app.routes)} routes registered")
        return 0

    if args.window:
        # Try native-window mode first; fall through to browser on any
        # missing-dep error.
        try:
            from lukav.desktop import have_pywebview, run_in_window
            if have_pywebview():
                print(f"lukav: opening native window at http://{args.host}:{args.port}/")
                run_in_window(args.host, args.port, title="Lukav")
                return 0
            print("lukav: pywebview not installed — falling back to browser. "
                  "Install with `pip install lukav[desktop]`.")
        except Exception as e:
            print(f"lukav: native-window mode failed ({e}); using browser.")

    if not args.no_open:
        try:
            webbrowser.open(f"http://{args.host}:{args.port}/")
        except Exception:
            pass

    import uvicorn
    uvicorn.run(create_app(), host=args.host, port=args.port,
                log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
