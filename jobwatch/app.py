"""
app.py  (Phase E.1 — the thing the launcher runs)
=================================================
Double-clicking the JobWatch launcher ends up here. This file:

  1. Confirms (and if needed creates) your external data folder, in plain words.
  2. Starts the local web server on your Mac.
  3. Opens your browser to the app.
  4. Stays running until you close it (Ctrl-C in the small window, or just quit).

You never type anything here. The launcher (a small double-clickable file)
runs this for you. It's kept separate from the server so the server stays a
clean, testable module and this file stays a tiny "switch it on" script.
"""

import sys
import time
import threading
import webbrowser

from . import paths
from . import server


def _find_free_port(preferred=8765, attempts=20):
    """Use the preferred port if we can; otherwise step up until one is free.
    Returns a bound ThreadingHTTPServer, or raises if none of the ports work."""
    import socket
    last_err = None
    for offset in range(attempts):
        port = preferred + offset
        try:
            srv = server.make_server(port=port)
            return srv, port
        except OSError as e:
            last_err = e
            continue
    raise RuntimeError(
        f"Couldn't find a free port near {preferred}. Last error: {last_err}"
    )


def main():
    # 1. Data folder — create on first run, confirm in plain language.
    print("=" * 60)
    print("  JobWatch")
    print("=" * 60)
    try:
        print(paths.friendly_confirmation())
    except Exception as e:
        print(f"Couldn't set up your data folder: {e}")
        sys.exit(1)

    # 2. Start the server.
    try:
        srv, port = _find_free_port()
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)

    url = f"http://127.0.0.1:{port}/"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    print()
    print(f"JobWatch is running at:  {url}")
    print("Your browser should open automatically.")
    print("To stop JobWatch: close this window (or press Ctrl-C here).")
    print()

    # 3. Open the browser (give the server a beat to come up first).
    def _open():
        time.sleep(0.6)
        try:
            webbrowser.open(url)
        except Exception:
            pass  # if it doesn't open, the printed URL still works
    threading.Thread(target=_open, daemon=True).start()

    # 4. Stay alive until interrupted.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping JobWatch. Bye.")
        srv.shutdown()


if __name__ == "__main__":
    main()
