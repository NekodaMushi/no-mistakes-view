"""Serveur local lié à 127.0.0.1 : la page, et l'état relu à la demande.

Aucun travail sans requête : la page interroge `/api/state`, et le résultat
est gardé tant que les fichiers de no-mistakes n'ont pas bougé.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import model, state

APP_ID = "no-mistakes-view"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
MAX_CACHE_AGE_S = 10


class StateSource:
    def __init__(self, home, linger_s):
        self.home = home
        self.linger_s = linger_s
        self._lock = threading.Lock()
        self._stamp = None
        self._built_at = 0
        self._payload = None

    def _files_stamp(self):
        stamp = []
        for name in ("state.sqlite", "state.sqlite-wal", "config.yaml"):
            try:
                info = os.stat(os.path.join(self.home, name))
                stamp.append((info.st_mtime_ns, info.st_size))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def _build(self, now):
        db_path = os.path.join(self.home, "state.sqlite")
        if not os.path.exists(db_path):
            return {"now": now, "linger_s": self.linger_s, "tabs": [],
                    "error": f"Base introuvable : {db_path}"}
        try:
            snapshot = state.read_snapshot(db_path, now, self.linger_s)
        except Exception as exc:  # base verrouillée, schéma inattendu
            return {"now": now, "linger_s": self.linger_s, "tabs": [],
                    "error": f"Lecture impossible : {exc}"}
        config = state.read_config(os.path.join(self.home, "config.yaml"))
        logs_dir = os.path.join(self.home, "logs")
        return model.build_tabs(
            snapshot,
            config,
            lambda run_id, step: state.read_log_tail(logs_dir, run_id, step),
            now,
            self.linger_s,
            state.pid_alive,
        )

    def payload(self):
        with self._lock:
            now = int(time.time())
            stamp = self._files_stamp()
            fresh = self._payload is not None and stamp == self._stamp
            # Un onglet fini doit se fermer même si la base ne bouge plus.
            closing = fresh and any(
                tab["closes_at"] and tab["closes_at"] <= now for tab in self._payload["tabs"]
            )
            if not fresh or closing or now - self._built_at >= MAX_CACHE_AGE_S:
                self._payload = self._build(now)
                self._stamp = stamp
                self._built_at = now
            return dict(self._payload, now=now)


def make_handler(source, on_quit=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = APP_ID

        def log_message(self, *args):
            pass

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _local_host(self):
            # Refuse tout nom d'hôte autre que la boucle locale (rebinding DNS).
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def do_POST(self):
            # Seule la page servie ici peut arrêter la commande : même hôte et même origine.
            origin = self.headers.get("Origin")
            same_origin = origin == f"http://{self.headers.get('Host')}"
            if not self._local_host() or not same_origin:
                return self._send(403, b"forbidden", "text/plain")
            if self.path.split("?", 1)[0] == "/api/quit" and on_quit:
                self._send(200, b"{}", "application/json; charset=utf-8")
                return on_quit()
            return self._send(404, b"not found", "text/plain")

        def do_GET(self):
            if not self._local_host():
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                body = json.dumps(source.payload(), ensure_ascii=False).encode("utf-8")
                return self._send(200, body, "application/json; charset=utf-8")
            if path == "/api/ping":
                body = json.dumps({"app": APP_ID, "pid": os.getpid()}).encode("utf-8")
                return self._send(200, body, "application/json; charset=utf-8")
            if path in STATIC:
                name, content_type = STATIC[path]
                with open(os.path.join(WEB_DIR, name), "rb") as handle:
                    return self._send(200, handle.read(), content_type)
            return self._send(404, b"not found", "text/plain")

    return Handler


def make_server(port, home, linger_s, on_quit=None):
    """`on_quit` est appelé quand la page demande l'arrêt (touche q)."""
    source = StateSource(home, linger_s)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(source, on_quit))
    server.daemon_threads = True
    return server
