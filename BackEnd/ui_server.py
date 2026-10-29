"""Serve the UI and resume-upload API without scraping."""

from __future__ import annotations

from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ensure_ui_server, open_in_chrome, ui_server_url

if __name__ == "__main__":
    server, url, started = ensure_ui_server()
    print(f"Live UI: {url}")
    if started:
        open_in_chrome(url + "#/resumes")
        print("Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            if server is not None:
                server.shutdown()
    else:
        print("UI server was already running.")
        open_in_chrome(url + "#/resumes")
