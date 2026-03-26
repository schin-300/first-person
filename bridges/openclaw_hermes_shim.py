#!/usr/bin/env python3
import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

LISTEN_HOST = os.environ.get("OPENCLAW_HERMES_SHIM_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("OPENCLAW_HERMES_SHIM_PORT", "30201"))
UPSTREAM_HOST = os.environ.get("OPENCLAW_HERMES_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("OPENCLAW_HERMES_UPSTREAM_PORT", "30101"))
UPSTREAM_API_KEY = os.environ.get("OPENCLAW_HERMES_UPSTREAM_API_KEY", "openclaw-local")
SHIM_MODEL = os.environ.get("OPENCLAW_HERMES_MODEL", "qwen3.5-4b-local")
TIMEOUT_SECONDS = float(os.environ.get("OPENCLAW_HERMES_TIMEOUT_SECONDS", "600"))

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class OpenClawHermesShim(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("[openclaw-hermes-shim] " + (fmt % args) + "\n")
        sys.stdout.flush()

    def do_GET(self) -> None:
        if self.path in {"/health", "/healthz"}:
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "openclaw-hermes-shim",
                    "listen": f"http://{LISTEN_HOST}:{LISTEN_PORT}",
                    "upstream": f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}",
                    "model": SHIM_MODEL,
                },
            )
            return

        if self.path == "/v1/models":
            self._proxy_request("GET")
            return

        self._send_json(404, {"ok": False, "error": f"Unsupported GET path: {self.path}"})

    def do_POST(self) -> None:
        if self.path == "/v1/chat/completions":
            self._proxy_request("POST")
            return

        self._send_json(404, {"ok": False, "error": f"Unsupported POST path: {self.path}"})

    def _send_json(self, status: int, payload: dict) -> None:
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _proxy_request(self, method: str) -> None:
        body = self._read_body()
        request_json: Optional[dict] = None
        stream_response = False

        if body and method == "POST":
            try:
                request_json = json.loads(body.decode("utf-8"))
            except Exception:
                request_json = None

        if isinstance(request_json, dict) and self.path == "/v1/chat/completions":
            chat_template_kwargs = request_json.get("chat_template_kwargs")
            if not isinstance(chat_template_kwargs, dict):
                chat_template_kwargs = {}
            chat_template_kwargs["enable_thinking"] = False
            request_json["chat_template_kwargs"] = chat_template_kwargs

            if request_json.get("model") in {None, "", "auto"}:
                request_json["model"] = SHIM_MODEL

            stream_response = bool(request_json.get("stream"))
            body = json.dumps(request_json, ensure_ascii=False).encode("utf-8")

        headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length"}:
                continue
            headers[key] = value

        if "Authorization" not in headers and UPSTREAM_API_KEY:
            headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
        if method == "POST":
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))

        upstream = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=TIMEOUT_SECONDS)
        try:
            upstream.request(method, self.path, body=body if method == "POST" else None, headers=headers)
            response = upstream.getresponse()

            self.send_response(response.status, response.reason)
            response_headers = response.getheaders()

            content_type = None
            content_length = None
            for key, value in response_headers:
                lower = key.lower()
                if lower == "content-length":
                    content_length = value
                    continue
                if lower in HOP_BY_HOP_HEADERS:
                    continue
                if lower == "content-type":
                    content_type = value
                self.send_header(key, value)

            if not stream_response and content_length is not None:
                self.send_header("Content-Length", content_length)
            self.end_headers()

            if stream_response or (content_type and "text/event-stream" in content_type.lower()):
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                payload = response.read()
                self.wfile.write(payload)
                self.wfile.flush()
        except Exception as exc:
            self.log_message("proxy error on %s %s: %s", method, self.path, exc)
            self._send_json(502, {"ok": False, "error": str(exc)})
        finally:
            try:
                upstream.close()
            except Exception:
                pass


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), OpenClawHermesShim)
    print(
        f"[openclaw-hermes-shim] listening on http://{LISTEN_HOST}:{LISTEN_PORT} -> http://{UPSTREAM_HOST}:{UPSTREAM_PORT}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
