"""HTTP surface for the Pre-Trade Advisory API.

``http.server`` on a thread pool -- deliberately no framework, so the service
runs anywhere Python does with nothing installed. It is a thin adapter: every
decision belongs to :class:`~deadpool.advisory.AdvisoryService`, and this file
only parses queries and serialises answers.

Routes::

    GET /health
    GET /v1/advisory?pool=<addr>[&amount_sol=0.1|&amount_lamports=N][&overhang=0]
    GET /v1/pool/<addr>
    GET /metrics

The advisory response always carries ``snapshot_slot`` so a caller can reason
about staleness, and ``verdict`` is one of ``safe`` / ``caution`` / ``unsafe``
/ ``unknown``. An ``unsafe`` verdict is served with HTTP 200: it is a
successful answer to the question asked, and mapping it to an error status
would push callers toward treating it as a transport failure to retry past.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .advisory import DEFAULT_PROBE_LAMPORTS, AdvisoryService
from .constants import LAMPORTS_PER_SOL

log = logging.getLogger("deadpool.api")


class _Handler(BaseHTTPRequestHandler):
    server_version = "deadpool/1.0"
    service: AdvisoryService  # injected by make_server

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's naming
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/health":
                self._json(200, {"status": "ok"})
            elif path == "/metrics":
                self._text(200, self.service.telemetry.prometheus())
            elif path == "/v1/advisory":
                self._advisory(query)
            elif path.startswith("/v1/pool/"):
                self._pool(path[len("/v1/pool/") :], query)
            else:
                self._json(404, {"error": "not found", "path": path})
        except _BadRequest as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # never leak a traceback to a caller
            log.exception("unhandled error serving %s", self.path)
            self.service.telemetry.record_error(type(exc).__name__)
            self._json(500, {"error": "internal error"})

    # --- routes -----------------------------------------------------------

    def _advisory(self, query: dict[str, list[str]]) -> None:
        pool = _one(query, "pool")
        if not pool:
            raise _BadRequest("missing required parameter: pool")
        amount = _amount(query)
        with_overhang = _flag(query, "overhang", default=True)
        advisory = self.service.advise(pool, amount, with_overhang=with_overhang)
        self._json(200, advisory.to_dict())

    def _pool(self, pool: str, query: dict[str, list[str]]) -> None:
        if not pool:
            raise _BadRequest("missing pool address")
        advisory = self.service.advise(pool, _amount(query), with_overhang=False)
        body: dict[str, Any] = {
            "pool": pool,
            "snapshot": advisory.snapshot.to_dict() if advisory.snapshot else None,
            "verdict": advisory.verdict.value,
            "snapshot_slot": advisory.snapshot_slot,
        }
        self._json(200 if advisory.snapshot else 404, body)

    # --- plumbing ---------------------------------------------------------

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._write(status, "application/json", json.dumps(payload, indent=2).encode())

    def _text(self, status: int, body: str) -> None:
        self._write(status, "text/plain; version=0.0.4", body.encode())

    def _write(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


class _BadRequest(ValueError):
    pass


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0].strip() if values else None


def _flag(query: dict[str, list[str]], key: str, *, default: bool) -> bool:
    raw = _one(query, key)
    if raw is None:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _amount(query: dict[str, list[str]]) -> int:
    """Deposit size to score, in lamports.

    Both spellings are accepted because both callers exist: a wallet knows
    lamports, a person typing a URL knows SOL.
    """
    lamports = _one(query, "amount_lamports")
    sol = _one(query, "amount_sol")
    if lamports and sol:
        raise _BadRequest("pass amount_lamports or amount_sol, not both")
    try:
        if lamports:
            value = int(lamports)
        elif sol:
            value = int(round(float(sol) * LAMPORTS_PER_SOL))
        else:
            return DEFAULT_PROBE_LAMPORTS
    except ValueError:
        raise _BadRequest("amount must be a number") from None
    if value < 0:
        raise _BadRequest("amount must not be negative")
    return value


def make_server(
    service: AdvisoryService, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    """Build (but do not start) the HTTP server."""
    handler = type("_BoundHandler", (_Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


def serve(service: AdvisoryService, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = make_server(service, host, port)
    log.info("advisory API listening on http://%s:%d", host, server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
