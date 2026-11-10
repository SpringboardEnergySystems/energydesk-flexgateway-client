"""Listen for signals from the VTN: a webhook receiver, a subscription, and a poll as the safety net.

A webhook is a hint, not a guarantee (the VTN does not queue notifications for a receiver that is down), so
``listen`` also polls ``GET /events`` and ``GET /programs`` and reports anything the webhooks did not already tell it.
Without a callback URL it only polls, which is all you need to see signals from a laptop that has no public address.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime
from typing import Any, Callable, Mapping

from .client import FlexgatewayClient
from .webhooks import make_server

Emit = Callable[[str], None]
SeenKey = tuple[str, str, str]


def _key(object_type: str, obj: Mapping[str, Any]) -> SeenKey:
    return object_type, str(obj.get("id")), str(obj.get("modificationDateTime"))


def _interval_text(event: Mapping[str, Any], interval: Mapping[str, Any]) -> str:
    period = interval.get("intervalPeriod") or event.get("intervalPeriod") or {}
    values = ", ".join(
        f"{p.get('type')}={','.join(str(v) for v in p.get('values', []))}" for p in interval.get("payloads") or [])
    when = f"{period.get('start', '?')} +{period.get('duration', '?')}"
    return f"{when}  {values}".rstrip()


def describe(notification: Mapping[str, Any]) -> str:
    """One readable line (a few for an event with several intervals) for a 3.1.0 notification."""
    object_type = notification.get("objectType", "?")
    obj = notification.get("object") or {}
    name = obj.get("eventName") or obj.get("programName") or obj.get("reportName") or obj.get("resourceName") \
        or obj.get("clientName") or obj.get("id", "?")
    head = f"{object_type:<12} {notification.get('operation', '?'):<7} {name}"
    if obj.get("programID"):
        head += f"  program={obj['programID']}"
    if object_type != "EVENT":
        return head
    if obj.get("targets"):
        head += f"  targets={','.join(obj['targets'])}"           # GROUP:<ven>/<name>: a dispatch for one of your groups
    shown = [_interval_text(obj, i) for i in (obj.get("intervals") or [])[:3]]
    more = len(obj.get("intervals") or []) - len(shown)
    lines = [head] + [f"    {text}" for text in shown]
    if more > 0:
        lines.append(f"    ... and {more} more interval(s)")
    return "\n".join(lines)


class Listener:
    """Remembers what it has already shown, so a webhook and the poll that follows it do not both print."""

    def __init__(self, client: FlexgatewayClient, emit: Emit):
        self._client = client
        self._emit = emit
        self._seen: set[SeenKey] = set()
        self._lock = threading.Lock()

    def _show(self, source: str, notification: Mapping[str, Any]) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._emit(f"{stamp} [{source}] {describe(notification)}")

    def on_webhook(self, notification: dict[str, Any]) -> None:
        obj = notification.get("object") or {}
        with self._lock:
            self._seen.add(_key(str(notification.get("objectType")), obj))
        self._show("webhook", notification)

    def poll(self, *, quiet: bool = False) -> int:
        """Read events and programs and show those not seen before. ``quiet`` only remembers them (the first read)."""
        new = 0
        for object_type, items in (("PROGRAM", self._client.list_programs()), ("EVENT", self._client.list_events())):
            for obj in items:
                key = _key(object_type, obj)
                with self._lock:
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                new += 1
                if not quiet:
                    self._show("poll", {"objectType": object_type, "operation": "SEEN", "object": obj})
        return new


def listen(
    client: FlexgatewayClient,
    *,
    emit: Emit = print,
    callback_url: str | None = None,
    host: str = "127.0.0.1",
    port: int = 9000,
    bearer_token: str | None = None,
    objects: tuple[str, ...] = ("EVENT", "PROGRAM"),
    operations: tuple[str, ...] = ("CREATE", "UPDATE", "DELETE"),
    program_id: str | None = None,
    poll_seconds: float = 60.0,
    stop: threading.Event | None = None,
) -> None:
    """Run until ``stop`` is set (or Ctrl-C). With ``callback_url`` it receives webhooks on ``host:port`` and
    subscribes for them; the subscription is deleted again on the way out."""
    stop = stop or threading.Event()
    listener = Listener(client, emit)
    known = listener.poll(quiet=True)
    emit(f"{known} program(s) and event(s) visible now; showing changes from here on.")

    server = subscription_id = None
    if callback_url:
        token = bearer_token or secrets.token_urlsafe(32)  # yours to choose; only the VTN and this receiver need it
        server = make_server(host, port, token, listener.on_webhook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        subscription = client.subscribe(
            callback_url, client_name=client.ven_name or "flexclient", objects=objects, operations=operations,
            bearer_token=token, program_id=program_id)
        subscription_id = subscription.get("id")
        emit(f"receiving webhooks on http://{host}:{port}, subscribed as {callback_url}")
    else:
        emit("no --callback-url: polling only.")
    emit(f"polling every {poll_seconds:g}s as a safety net. Ctrl-C to stop.")

    try:
        while not stop.wait(poll_seconds):
            listener.poll()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if subscription_id:
            client.delete_subscription(subscription_id)
            emit("subscription deleted.")
