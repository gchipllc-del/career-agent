"""
app.py — desktop launcher for the Career Agent.

Starts the local server in a background thread and opens it in a NATIVE window
(via pywebview) so it feels like a real desktop app, not a browser tab. With
LLM_PROVIDER=ollama it runs fully offline — no account, no API key, nothing
leaves the machine.

Run:   python app.py
       (If pywebview isn't installed it falls back to opening your browser.
        For the native window:  pip install pywebview)
"""

import os
import threading
import time
import urllib.request

import localenv
localenv.load()  # apply .env (LLM_PROVIDER/keys) before server imports read it

PORT = int(os.getenv("PORT", "8000"))
URL = f"http://127.0.0.1:{PORT}"


def _serve():
    import server
    server.main()


def _wait_ready(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL + "/api/health", timeout=1).read()
            return True
        except Exception:
            time.sleep(0.4)
    return False


def main():
    threading.Thread(target=_serve, daemon=True).start()
    if not _wait_ready():
        print("Server did not come up in time — check the logs.")
        return
    print(f"Career Agent ready at {URL}")
    try:
        import webview  # pywebview -> native OS window (WebKit on macOS)
        webview.create_window("Career Agent", URL, width=1280, height=880, min_size=(900, 600))
        webview.start()
    except ImportError:
        import webbrowser
        print("pywebview not installed — opening in your browser instead.")
        print("For a native app window:  pip install pywebview")
        webbrowser.open(URL)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
