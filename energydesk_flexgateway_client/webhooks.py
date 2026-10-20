"""A minimal webhook receiver, to see what the VTN sends and how to check it came from the VTN.

The VTN ``POST``s a notification to your ``callbackUrl`` with ``Authorization: Bearer <the bearerToken you subscribed
with>``. A real receiver sits behind https on a public address (the VTN refuses private addresses and plain http), checks
the bearer token with a constant-time comparison, answers ``2xx`` quickly (the VTN waits 5 seconds), and does its work
afterwards. This one prints the notification; it is meant for development and examples.
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], None]


def make_server(host: str, port: int, bearer_token: str, on_notification: Handler) -> ThreadingHTTPServer:
    """An HTTP server that accepts VTN notifications. ``bearer_token`` is the one given to ``subscribe``."""
    expected = f"Bearer {bearer_token}".encode()

    class VtnNotificationHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
            given = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                self.send_response(401)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                notification = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(204)  # answer first, act afterwards
            self.end_headers()
            on_notification(notification)

        def log_message(self, *args: object) -> None:  # never log headers: they carry the bearer token
            pass

    return ThreadingHTTPServer((host, port), VtnNotificationHandler)
