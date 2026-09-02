"""Config resolution, the poll loop, and the HTTP dispatch."""

from __future__ import annotations

import pytest

from acinfinity_exporter.__main__ import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_PORT,
    ConfigError,
    Poller,
    resolve_config,
    serve,
)
from acinfinity_exporter.client import ACInfinityError
from acinfinity_exporter.collector import DEFAULT_NAMESPACE, ACInfinityCollector

CREDS = {"ACINFINITY_EMAIL": "u@example.com", "ACINFINITY_PASSWORD": "p"}
DEVICES = [
    {"devId": "1", "devName": "x", "deviceInfo": {"temperature": 2104, "ports": [{"port": 1}]}}
]


def env(**extra: str) -> dict[str, str]:
    return {**CREDS, **extra}


# ------------------------------------------------------------------ precedence


def test_flag_beats_environment():
    cfg = resolve_config(["--poll-interval", "30"], env(ACINFINITY_EXPORTER_POLL_INTERVAL="90"))
    assert cfg.poll_seconds == 30


def test_environment_beats_default():
    cfg = resolve_config([], env(ACINFINITY_EXPORTER_METRIC_PREFIX="acme"))
    assert cfg.metric_prefix == "acme"


def test_defaults():
    cfg = resolve_config([], env())
    assert cfg.poll_seconds == DEFAULT_POLL_SECONDS
    assert cfg.port == DEFAULT_PORT
    assert cfg.metric_prefix == DEFAULT_NAMESPACE
    assert cfg.port_names == {}


def test_empty_metric_prefix_is_honoured():
    cfg = resolve_config([], env(ACINFINITY_EXPORTER_METRIC_PREFIX=""))
    assert cfg.metric_prefix == ""


# ----------------------------------------------------------------- credentials


def test_credentials_are_required():
    with pytest.raises(ConfigError, match="credentials are required"):
        resolve_config([], {})


def test_there_is_no_credential_flag():
    with pytest.raises(SystemExit):
        resolve_config(["--password", "x"], env())


# ------------------------------------------------------------------ port names


def test_port_names_from_json_env():
    cfg = resolve_config(
        [], env(ACINFINITY_EXPORTER_PORT_NAMES='{"1": "Closet exhaust", "9/2": "x"}')
    )
    assert cfg.port_names == {"1": "Closet exhaust", "9/2": "x"}


def test_port_names_may_contain_commas_and_equals():
    cfg = resolve_config(["--port-names", '{"1": "Closet, exhaust = T10"}'], env())
    assert cfg.port_names["1"] == "Closet, exhaust = T10"


def test_port_names_reject_non_object_and_empty_names():
    with pytest.raises(ConfigError, match="JSON object"):
        resolve_config(["--port-names", "[1,2]"], env())
    with pytest.raises(ConfigError, match="non-empty"):
        resolve_config(["--port-names", '{"1": ""}'], env())
    with pytest.raises(ConfigError, match="JSON object"):
        resolve_config(["--port-names", "1=foo"], env())


# --------------------------------------------------------------------- parsing


def test_listen_port_env_name_is_not_a_service_link_collision():
    """ACINFINITY_EXPORTER_PORT is what Kubernetes injects for a Service of that name."""
    cfg = resolve_config([], env(ACINFINITY_EXPORTER_PORT="tcp://10.0.0.1:8000"))
    assert cfg.port == DEFAULT_PORT
    cfg = resolve_config([], env(ACINFINITY_EXPORTER_LISTEN_PORT="9000"))
    assert cfg.port == 9000


def test_zero_poll_interval_is_rejected():
    with pytest.raises(ConfigError, match="positive"):
        resolve_config([], env(ACINFINITY_EXPORTER_POLL_INTERVAL="0"))


# ------------------------------------------------------------------- poll loop


class FakeClient:
    def __init__(self, devices, fail=False):
        self._devices = devices
        self.fail = fail
        self.calls = 0

    def list_devices(self):
        self.calls += 1
        if self.fail:
            raise ACInfinityError("vendor unreachable")
        return self._devices


def test_successful_collection_updates_the_collector():
    collector = ACInfinityCollector()
    assert Poller(FakeClient(DEVICES), collector, 60).collect_once() is True
    fam = {f.name: f for f in collector.collect()}
    assert fam["acinfinity_collection_success"].samples[0].value == 1
    assert "acinfinity_controller_temperature_celsius" in fam


def test_failure_never_raises_and_publishes_no_readings_when_cold():
    collector = ACInfinityCollector()
    assert Poller(FakeClient(DEVICES, fail=True), collector, 60).collect_once() is False
    names = {f.name for f in collector.collect()}
    assert "acinfinity_collection_success" in names
    assert not any("controller_temperature" in n for n in names)


def test_unexpected_exception_is_also_contained():
    class Boom:
        def list_devices(self):
            raise KeyError("shape changed")

    collector = ACInfinityCollector()
    assert Poller(Boom(), collector, 60).collect_once() is False


def test_failure_after_success_retains_values_and_drops_success():
    collector = ACInfinityCollector()
    client = FakeClient(DEVICES)
    poller = Poller(client, collector, 60)
    poller.collect_once()
    client.fail = True
    poller.collect_once()
    fam = {f.name: f for f in collector.collect()}
    assert fam["acinfinity_collection_success"].samples[0].value == 0
    assert fam["acinfinity_controller_temperature_celsius"].samples[0].value == 21.04


def test_stop_is_honoured_promptly():
    collector = ACInfinityCollector()
    client = FakeClient(DEVICES)
    poller = Poller(client, collector, 3600)
    poller.start()
    poller.stop()
    poller.join(timeout=5)
    assert not poller.is_alive()
    assert client.calls == 1


def test_poller_does_not_shadow_threading_internals():
    import threading

    poller = Poller(FakeClient(DEVICES), ACInfinityCollector(), 60)
    own = set(vars(poller)) - set(vars(threading.Thread()))
    assert own & set(dir(threading.Thread)) == set()


# ------------------------------------------------------------------------ http


def test_health_and_metrics_are_dispatched_and_unknown_paths_404():
    import urllib.error
    import urllib.request

    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    collector = ACInfinityCollector()
    registry.register(collector)
    Poller(FakeClient(DEVICES), collector, 60).collect_once()
    server = serve(registry, "127.0.0.1", 0)
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            assert resp.status == 200
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            assert b"acinfinity_controller_temperature_celsius" in resp.read()
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/hplth", timeout=5)
        assert caught.value.code == 404
    finally:
        server.shutdown()
