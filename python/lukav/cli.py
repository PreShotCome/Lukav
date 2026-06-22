"""Lukav launcher.

Usage:
  python -m lukav            # start server + open browser
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lukav")
    parser.add_argument("--host", default=os.environ.get("LUKAV_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("LUKAV_PORT", DEFAULT_PORT)),
    )
    parser.add_argument("--no-open", action="store_true",
                        help="Do not open a browser window.")
    parser.add_argument("--check", action="store_true",
                        help="Construct the app and exit (no server).")
    args = parser.parse_args(argv)

    from lukav.web.app import create_app

    app = create_app()
    if args.check:
        print(f"lukav app OK; {len(app.routes)} routes registered")
        return 0

    if not args.no_open:
        try:
            webbrowser.open(f"http://{args.host}:{args.port}/")
        except Exception:
            pass

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
