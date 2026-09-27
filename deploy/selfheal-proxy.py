#!/usr/bin/env python3
"""vektor-selfheal-proxy — injecte X-Vektor-Selfheal vers vektor-api.

Pourquoi : Grafana v12 (nouveau provisioner) n'envoie les valeurs de headers
de contact point QUE depuis secureSettings chiffrés, qu'il ne chiffre pas
lui-même au provisioning. Plutôt que stocker le secret dans Grafana, ce proxy
local (127.0.0.1:8010 -> 127.0.0.1:8011) signe chaque POST avant retransmission.
Le secret vit uniquement dans /etc/vektor/selfheal-secret (600) sur le VPS.
"""
import http.server
import os
import urllib.request
import urllib.error

SECRET_FILE = "/etc/vektor/selfheal-secret"
UPSTREAM = "http://127.0.0.1:8011"
LISTEN_PORT = 8010
HEADER = "X-Vektor-Selfheal"


def read_secret() -> str:
    try:
        with open(SECRET_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


SECRET = read_secret()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(UPSTREAM + self.path, data=body, method=method)
        for key, value in self.headers.items():
            if key.lower() in ("host", "content-length", "connection", HEADER.lower()):
                continue
            req.add_header(key, value)
        if SECRET:
            req.add_header(HEADER, SECRET)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                out_body = resp.read()
                self.send_response(resp.status)
                out_headers = {k: v for k, v in resp.getheaders() if k.lower() not in ("transfer-encoding", "connection")}
                for k, v in out_headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out_body)))
                self.end_headers()
                self.wfile.write(out_body)
        except urllib.error.HTTPError as e:
            out_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(out_body)))
            self.end_headers()
            self.wfile.write(out_body)
        except Exception:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):  # noqa: N802
        self._proxy("POST")

    def do_GET(self):  # noqa: N802
        self._proxy("GET")

    def log_message(self, fmt, *args):  # silence access log systemd
        pass


if __name__ == "__main__":
    if not SECRET:
        raise SystemExit(f"secret manquant : {SECRET_FILE}")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    server.serve_forever()
