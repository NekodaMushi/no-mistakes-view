"""Commande `no-mistakes-view` : démarre le serveur local et ouvre la fenêtre."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from . import server, state

DEFAULT_PORT = 47615
DEFAULT_LINGER_S = 90
HANDOFF_S = 3
APP_BROWSERS = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "brave-browser", "microsoft-edge",
)
# Sur macOS les navigateurs vivent dans des bundles .app, hors du PATH.
MAC_APPS = ("Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge")


def _already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.5) as reply:
            return json.load(reply).get("app") == server.APP_ID
    except (OSError, ValueError):
        return False


def _mac_app_roots():
    return ("/Applications", os.path.expanduser("~/Applications"))


def _find_app_browser(wanted, platform=sys.platform, mac_roots=None):
    for name in ([wanted] if wanted else APP_BROWSERS):
        path = shutil.which(name)
        if path:
            return path
    if platform == "darwin":
        for app in ([wanted] if wanted else MAC_APPS):
            for root in mac_roots or _mac_app_roots():
                path = os.path.join(root, f"{app}.app", "Contents", "MacOS", app)
                if os.access(path, os.X_OK):
                    return path
    return None


def _cache_dir(platform=sys.platform):
    if os.environ.get("XDG_CACHE_HOME"):
        return os.environ["XDG_CACHE_HOME"]
    if platform == "darwin":
        return os.path.expanduser("~/Library/Caches")
    return os.path.expanduser("~/.cache")


def _open_window(url, browser):
    """Rend le processus de la fenêtre, ou None si on ne peut pas le suivre."""
    path = _find_app_browser(browser)
    if not path:
        webbrowser.open(url)
        return None
    profile = os.path.join(_cache_dir(), server.APP_ID, "profile")
    os.makedirs(profile, exist_ok=True)
    return subprocess.Popen(
        [
            path, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1180,640",
            "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--disable-background-networking", "--disable-component-update",
            "--disable-extensions", f"--class={server.APP_ID}",
        ],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=server.APP_ID,
        description="Vue en direct des runs no-mistakes de cette machine (lecture seule).",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--linger", type=int, default=DEFAULT_LINGER_S,
                        help="secondes pendant lesquelles un run fini garde son onglet")
    parser.add_argument("--browser", help="navigateur de la famille Chromium à utiliser")
    parser.add_argument("--no-window", action="store_true", help="serveur seul, sans fenêtre")
    args = parser.parse_args(argv)

    if os.name == "nt":
        # Sous Windows, os.kill(pid, 0) termine le processus au lieu de le tester :
        # la vue tuerait les agents qu'elle surveille.
        print("no-mistakes-view ne tourne pas sous Windows (Linux et macOS seulement).",
              file=sys.stderr)
        return 2

    if _already_running(args.port):
        url = f"http://127.0.0.1:{args.port}/"
        print(f"Déjà lancé sur {url} : ouverture d'une fenêtre sur l'instance en place.")
        if not args.no_window:
            _open_window(url, args.browser)
        return 0

    stop = threading.Event()
    try:
        httpd = server.make_server(args.port, state.home_dir(), args.linger, stop.set)
    except OSError:
        httpd = server.make_server(0, state.home_dir(), args.linger, stop.set)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"{server.APP_ID} : {url}  (q dans la fenêtre ou Ctrl-C pour arrêter)", flush=True)

    window = None if args.no_window else _open_window(url, args.browser)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    opened_at = time.monotonic()
    while not stop.is_set():
        if window is not None and window.poll() is not None:
            if time.monotonic() - opened_at > HANDOFF_S:
                break  # fenêtre fermée
            # Sortie immédiate : un navigateur déjà ouvert sur ce profil a pris
            # la fenêtre. On sert jusqu'à Ctrl-C.
            window = None
        stop.wait(0.5)

    if window is not None and window.poll() is None:
        window.terminate()
        try:
            window.wait(timeout=3)
        except subprocess.TimeoutExpired:
            window.kill()
    httpd.shutdown()
    httpd.server_close()
    print("Arrêté.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
