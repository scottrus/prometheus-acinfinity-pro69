"""CLI entry point: resolve config, start the poll loop, serve /metrics.

CONFIG PRECEDENCE IS flag > environment > default. The environment half is the
point: a Kubernetes ConfigMap becomes environment variables becomes
configuration, and a flag still wins for a local run.

CREDENTIALS ARE ENVIRONMENT-ONLY. There is no `--password` flag and there must
never be one: argv is visible in `ps`, in a container's `spec.containers[].args`
and in any crash dump. The two variable names are fixed, because a Secret
consumed with `envFrom` turns its key names into these names:

    ACINFINITY_EMAIL
    ACINFINITY_PASSWORD

THE SERVER STARTS BEFORE THE FIRST COLLECTION, AND A FAILED LOGIN DOES NOT EXIT.
A vendor outage at pod start must produce a running exporter that reports
`acinfinity_collection_success 0` on a scrape somebody can alert on, not a
CrashLoopBackOff that destroys the evidence.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import REGISTRY, make_wsgi_app

from . import __version__
from .client import DEFAULT_USER_AGENT, ACInfinityClient, ACInfinityError
from .collector import DEFAULT_NAMESPACE, ACInfinityCollector, collect_snapshot

log = logging.getLogger("acinfinity_exporter")

ENV_PREFIX = "ACINFINITY_EXPORTER_"
ENV_EMAIL = "ACINFINITY_EMAIL"
ENV_PASSWORD = "ACINFINITY_PASSWORD"

# 8000 is the port every earlier client for this API used. Nothing else in a
# typical monitoring namespace claims it.
DEFAULT_PORT = 8000
# 60 s matches the cloud's own record rate of about one per minute. Faster polls
# return the same reading; slower ones lose it.
DEFAULT_POLL_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 30


class ConfigError(ValueError):
    """Configuration is unusable. Raised rather than exiting, so it is testable."""


@dataclass(frozen=True, slots=True)
class Config:
    email: str
    password: str
    poll_seconds: int = DEFAULT_POLL_SECONDS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    metric_prefix: str = DEFAULT_NAMESPACE
    port_names: Mapping[str, str] = None  # type: ignore[assignment]
    user_agent: str = DEFAULT_USER_AGENT
    listen_address: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    log_level: str = "INFO"


def _int_or_error(raw: str | None, name: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def parse_port_names(raw: str | None) -> dict[str, str]:
    """A JSON object. Keys are `"<port>"` or `"<controller_id>/<port>"`, values are names.

    JSON rather than `1=a,2=b`, because a port name may legitimately contain a comma
    or an equals sign, and a separator that a value can contain is a bug in waiting.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"port names must be a JSON object, got {raw!r}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("port names must be a JSON object mapping port to name")
    names: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"port name for {key!r} must be a non-empty string")
        names[str(key).strip()] = value.strip()
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acinfinity-exporter",
        description="Prometheus exporter for the AC Infinity UIS Controller 69 PRO.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    # Every default is None so that "was the flag given?" stays distinct from "the
    # flag equals the default". Without that a flag could never lose to the environment.
    parser.add_argument(
        "--poll-interval",
        default=None,
        help=f"Seconds between vendor polls, default {DEFAULT_POLL_SECONDS} "
        f"[{ENV_PREFIX}POLL_INTERVAL]",
    )
    parser.add_argument(
        "--api-timeout",
        default=None,
        help=f"Seconds per vendor request, default {DEFAULT_TIMEOUT_SECONDS} "
        f"[{ENV_PREFIX}API_TIMEOUT]",
    )
    parser.add_argument(
        "--metric-prefix",
        default=None,
        help=f"Metric namespace, default {DEFAULT_NAMESPACE!r} [{ENV_PREFIX}METRIC_PREFIX]",
    )
    parser.add_argument(
        "--port-names",
        default=None,
        help='JSON object of configured port names, e.g. \'{"1": "Closet exhaust"}\' '
        f"[{ENV_PREFIX}PORT_NAMES]",
    )
    parser.add_argument(
        "--user-agent",
        default=None,
        help=f"User-Agent sent to the vendor [{ENV_PREFIX}USER_AGENT]",
    )
    parser.add_argument("--listen-address", default=None, help=f"[{ENV_PREFIX}LISTEN_ADDRESS]")
    # LISTEN_PORT, not PORT. A Service named `acinfinity-exporter` makes Kubernetes
    # inject ACINFINITY_EXPORTER_PORT=tcp://... into every pod in the namespace, and
    # a variable named PORT here would collide with it.
    parser.add_argument("--listen-port", default=None, help=f"[{ENV_PREFIX}LISTEN_PORT]")
    parser.add_argument("--log-level", default=None, help=f"[{ENV_PREFIX}LOG_LEVEL]")
    return parser


def resolve_config(argv: Sequence[str], environ: Mapping[str, str]) -> Config:
    """Pure: takes argv and an environment mapping, returns Config or raises."""
    args = build_parser().parse_args(list(argv))

    def pick(flag_value: str | None, env_suffix: str) -> str | None:
        if flag_value is not None:
            return flag_value
        return environ.get(ENV_PREFIX + env_suffix)

    email = environ.get(ENV_EMAIL)
    password = environ.get(ENV_PASSWORD)
    if not email or not password:
        raise ConfigError(
            f"credentials are required and are read from the environment ONLY: "
            f"set {ENV_EMAIL} and {ENV_PASSWORD}. There is no flag for these on purpose."
        )

    poll = _int_or_error(pick(args.poll_interval, "POLL_INTERVAL"), "poll interval")
    if poll is not None and poll <= 0:
        raise ConfigError("poll interval must be positive")
    timeout = _int_or_error(pick(args.api_timeout, "API_TIMEOUT"), "api timeout")
    if timeout is not None and timeout <= 0:
        raise ConfigError("api timeout must be positive")
    port = _int_or_error(pick(args.listen_port, "LISTEN_PORT"), "listen port")
    metric_prefix = pick(args.metric_prefix, "METRIC_PREFIX")

    return Config(
        email=email,
        password=password,
        poll_seconds=poll or DEFAULT_POLL_SECONDS,
        timeout_seconds=timeout or DEFAULT_TIMEOUT_SECONDS,
        # `is None` rather than `or`: an intentionally empty prefix is a valid choice.
        metric_prefix=DEFAULT_NAMESPACE if metric_prefix is None else metric_prefix,
        port_names=parse_port_names(pick(args.port_names, "PORT_NAMES")),
        user_agent=pick(args.user_agent, "USER_AGENT") or DEFAULT_USER_AGENT,
        listen_address=pick(args.listen_address, "LISTEN_ADDRESS") or "0.0.0.0",
        port=port or DEFAULT_PORT,
        log_level=(pick(args.log_level, "LOG_LEVEL") or "INFO").upper(),
    )


class Poller(threading.Thread):
    """Timer-driven collection. Scrapes never reach the vendor; this does.

    Collects once immediately, then waits on an Event rather than sleeping, so
    SIGTERM is honoured at once instead of after a full interval.
    """

    def __init__(
        self,
        client: ACInfinityClient,
        collector: ACInfinityCollector,
        interval_seconds: int,
    ) -> None:
        super().__init__(name="acinfinity-poll", daemon=True)
        self._client = client
        self._collector = collector
        self._interval = interval_seconds
        # Not `_stop`: threading.Thread owns a private `_stop()` on 3.11 and
        # assigning an Event over it breaks join().
        self._stopping = threading.Event()

    def collect_once(self) -> bool:
        """One collection. Returns success; never raises.

        A vendor error must not kill the thread, or the exporter would serve a
        frozen snapshot forever with nothing to say it had stopped trying.
        """
        started = time.time()
        try:
            devices = self._client.list_devices()
        except ACInfinityError as exc:
            # str(exc) carries code and msg only, never a response body.
            log.error("collection failed: %s", exc)
            self._collector.mark_failure()
            return False
        except Exception:
            log.exception("collection failed with an unexpected error")
            self._collector.mark_failure()
            return False
        snapshot = collect_snapshot(devices, started=started)
        for warning in snapshot.warnings:
            log.warning(warning)
        self._collector.update(snapshot)
        log.info(
            "collected controllers=%d ports=%d in %.2fs",
            len(snapshot.controllers),
            sum(len(c.ports) for c in snapshot.controllers),
            snapshot.duration_seconds,
        )
        return True

    def run(self) -> None:
        while True:
            self.collect_once()
            if self._stopping.wait(self._interval):
                log.info("poll loop stopping")
                return

    def stop(self) -> None:
        self._stopping.set()


def make_app(registry):
    """WSGI app: /metrics, /health, and 404 for everything else.

    prometheus_client's own app answers EVERY path with the metrics body, so a
    probe against /health would pass on a typo. Explicit dispatch makes /health
    mean one narrow thing: the HTTP server is up. It does not report collection
    state; that is what `acinfinity_collection_success` is for, and a readiness
    gate on it would stop the scrape that carries the bad news.
    """
    metrics_app = make_wsgi_app(registry)

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path == "/metrics":
            return metrics_app(environ, start_response)
        if path == "/health":
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"ok\n"]
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"not found\n"]

    return app


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        pass


def serve(registry, address: str, port: int) -> WSGIServer:
    server = make_server(address, port, make_app(registry), _ThreadingWSGIServer, _QuietHandler)
    threading.Thread(target=server.serve_forever, name="acinfinity-http", daemon=True).start()
    return server


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = resolve_config(sys.argv[1:] if argv is None else argv, os.environ)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    collector = ACInfinityCollector(namespace=config.metric_prefix, port_names=config.port_names)
    REGISTRY.register(collector)

    client = ACInfinityClient(
        config.email,
        config.password,
        timeout=config.timeout_seconds,
        user_agent=config.user_agent,
    )
    poller = Poller(client, collector, config.poll_seconds)

    # Server first, then the poll loop. See the module docstring.
    server = serve(REGISTRY, config.listen_address, config.port)
    log.info(
        "serving /metrics and /health on %s:%d; polling the vendor every %ds; prefix %r; "
        "%d configured port name(s)",
        config.listen_address,
        config.port,
        config.poll_seconds,
        config.metric_prefix,
        len(config.port_names),
    )
    poller.start()

    stopping = threading.Event()

    def _handle(signum, _frame):
        log.info("received signal %s", signum)
        poller.stop()
        stopping.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    stopping.wait()
    server.shutdown()
    poller.join(timeout=10)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
