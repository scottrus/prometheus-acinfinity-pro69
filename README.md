# prometheus-acinfinity-pro69

Prometheus exporter for the **AC Infinity UIS Controller 69 PRO**, read from the vendor
cloud API. It publishes the controller's own temperature, humidity and VPD, and the state
of each port, as gauges. A separate one-shot program backfills the vendor's history into
VictoriaMetrics with the original timestamps.

Built for one controller with a CLOUDLINE T10 on port 1, in a server closet. The code is
generic over controllers and ports, and it is tested against that one shape only.

| | |
|---|---|
| Image | `ghcr.io/scottrus/prometheus-acinfinity-pro69` |
| Chart | `oci://ghcr.io/scottrus/charts/acinfinity-exporter` |
| Metric prefix | `acinfinity_` |
| Port | 8000 |

## Why this exists

**The controller has no local API.** It talks only to `www.acinfinityserver.com`, and so does
the vendor app, even on the same LAN. Bluetooth is the one local path, and the manual states
that a Bluetooth link disables Wi-Fi. There is no configuration that gives local control and
cloud telemetry at the same time.

**The vendor gives you the phone app and nothing else.** There is no customer web view. The
app shows a chart at a resolution it chooses, for as long as it chooses. This exporter builds
the missing view: a 60-second series that outlives the vendor's retention, and that sits next
to every other sensor in the same TSDB.

The API also returns more than the app shows: per-port resistance, overcurrent and abnormal
flags, load type, and the firmware version. All of those are published.

---

## Read this before you deploy it

### This project was written with an AI coding assistant

A human reviewed and directed it, against a live controller. That is disclosed here so you can
weigh it, rather than discover it from the commit history.

**What is grounded:**

- Login, the device list and one history page were **read live on 2026-09-02** from a 69 PRO on
  firmware 3.2.56. The test fixtures are those responses, with account identifiers redacted.
- Every field decode is asserted against those fixtures, and the two spellings of the VPD field
  are asserted separately, because the reference exporter read the wrong one.
- The container builds, runs as uid 65532 with a read-only root filesystem, and scans clean.

**What has not been exercised, stated plainly:**

- **The session-expiry path has only run against a fake transport.** The API reports an expired
  session as HTTP 200 with a non-200 body code. The client re-logs-in once and retries. That
  logic is tested; a real expiry has not yet been observed through it.
- **One controller, one port in use.** Multiple controllers, external sensor probes, and the
  newer AI+ controller family (`newFrameworkDevice: true`) are handled generically and are
  untested. The `sensors[]` array is not decoded at all.
- **The backfill has not yet written to a live VictoriaMetrics.** Its decode and its paging are
  tested against a captured page; the write path has run in `--dry-run` only.

Read the source before you trust it. It is short and commented at the points where a reader
would otherwise ask why.

### The credential is a full account session, and nothing bounds it

The API has one credential: the account email and password. The token it returns can **write
to every controller on the account**, including a controller that was shared to it. There is
no read-only key, no scope, and no per-device grant.

This exporter never writes. It calls three endpoints, all reads, and the table in
[SECURITY.md](SECURITY.md) is the full list. But the credential it holds could write, and that
is the whole threat model. Two mitigations are available, and the second is the one that
matters:

1. **Force HTTPS.** Done; the base URL is fixed and the client has no option to change the
   scheme.
2. **Create a second AC Infinity account and share the controller to it.** The vendor supports
   this from the app's Account page. A leak of that account's password then costs one shared
   controller, not the account that owns it. **Note that a shared account still holds write
   access to the shared controller.** `isShare: 1` does not mean read-only; that was verified
   with a live write on 2026-09-01.

The login response echoes the password back in a field named `appPasswordl`. The client
discards that response as soon as the token is copied out, and never logs it at any level.

---

## Metrics

Identity is `controller_id` and `port`. **Editable names appear on the two `*_info` gauges
only.** A rename in the vendor app therefore ends the info series and nothing else.

### Controller

| Metric | Labels | Notes |
|---|---|---|
| `acinfinity_controller_info` | `controller_id`, `controller_name`, `device_code`, `device_type`, `firmware_version`, `hardware_version`, `zone_id` | Always 1 |
| `acinfinity_controller_online` | `controller_id` | 1 when the cloud reports the controller online |
| `acinfinity_controller_temperature_celsius` | `controller_id` | The controller's own probe. API value divided by 100 |
| `acinfinity_controller_humidity_percent` | `controller_id` | Divided by 100 |
| `acinfinity_controller_vpd_kpa` | `controller_id` | From the live field `vpdnums`, lowercase n |
| `acinfinity_controller_temperature_trend` | `controller_id` | 0 stable, 1 rising, 2 falling |
| `acinfinity_controller_humidity_trend` | `controller_id` | Same enum |

### Port

| Metric | Labels | Notes |
|---|---|---|
| `acinfinity_port_info` | `controller_id`, `port`, `port_name`, `vendor_port_name` | Always 1. `port_name` is the configured name, see below |
| `acinfinity_port_power_level` | `controller_id`, `port` | 0 to 10. The app calls it speed. API field `speak` |
| `acinfinity_port_online` | `controller_id`, `port` | |
| `acinfinity_port_connected` | `controller_id`, `port` | 1 when a load is electrically present |
| `acinfinity_port_resistance_ohms` | `controller_id`, `port` | **Absent on an empty port.** The API reports 65535 there, and that value is never published |
| `acinfinity_port_load_state` | `controller_id`, `port` | |
| `acinfinity_port_load_type` | `controller_id`, `port` | Collected because it was seen to change between reads |
| `acinfinity_port_mode` | `controller_id`, `port` | 1 OFF, 2 ON, 3 AUTO, 4 timer-to-on, 5 timer-to-off, 6 cycle, 7 schedule, 8 VPD |
| `acinfinity_port_overcurrent` | `controller_id`, `port` | |
| `acinfinity_port_abnormal` | `controller_id`, `port` | |
| `acinfinity_port_automation_active` | `controller_id`, `port` | 1 when an Advance automation program controls the port |

### Exporter

| Metric | Notes |
|---|---|
| `acinfinity_collection_success` | 1 or 0 for the most recent attempt |
| `acinfinity_last_collection_timestamp_seconds` | Last **successful** collection |
| `acinfinity_collection_duration_seconds` | |

One controller with four ports publishes about 50 series.

### Port names come from configuration, not from the app

The app lets anyone rename a port at any time, and the factory default is `Port 1`. A name
that lives in configuration cannot drift under a dashboard. Set it as a JSON object:

```bash
ACINFINITY_EXPORTER_PORT_NAMES='{"1": "Closet exhaust T10"}'
```

A key is a port number, or `<controller_id>/<port>` when two controllers share a numbering.
The specific key wins. A port with no configured name falls back to the vendor name.
`vendor_port_name` always carries what the app shows, so the two are visible side by side.

In the Helm chart this is the `portNames` value, rendered into the ConfigMap.

### What is deliberately not published

- **`insideTemp`, `insideTempF`, `outsideTemp`, `outsideTempF`, `leafTemp`, and every
  `thermal*` field.** On a controller with no such probe they read 0, and `insideTempF` reads
  3200. That decodes to 32.00 F, which is 0 C. A gauge built from it draws a plausible,
  permanent freezing line. A test asserts the strings `3200` and `32.0` never reach the
  exposition.
- **`temperatureF`.** One unit per measurement. A second unit invites a panel that reads the
  wrong one.
- **The `sensors[]` array.** This controller returns null for it. Its own temperature and
  humidity arrive in `deviceInfo` instead, which is where this exporter reads them.
- **`devName` and `portName` on measurement gauges.** See the identity rule above.

---

## Configuration

Credentials come from the environment **only**. There is no flag for them, because argv is
visible in `ps`, in a container spec and in a crash dump. The names are fixed, because a
Kubernetes Secret consumed with `envFrom` turns its key names into these names:

```
ACINFINITY_EMAIL
ACINFINITY_PASSWORD
```

Everything else follows flag > environment > default.

| Flag | Environment | Default | Notes |
|---|---|---|---|
| `--poll-interval` | `ACINFINITY_EXPORTER_POLL_INTERVAL` | `60` | Seconds between vendor polls. This, and only this, sets the vendor call rate. Scrapes never reach the vendor |
| `--api-timeout` | `ACINFINITY_EXPORTER_API_TIMEOUT` | `30` | Seconds per request |
| `--metric-prefix` | `ACINFINITY_EXPORTER_METRIC_PREFIX` | `acinfinity` | For collision avoidance only |
| `--port-names` | `ACINFINITY_EXPORTER_PORT_NAMES` | `{}` | JSON object, see above |
| `--user-agent` | `ACINFINITY_EXPORTER_USER_AGENT` | this project's name and version | An honest identity is accepted by the API |
| `--listen-address` | `ACINFINITY_EXPORTER_LISTEN_ADDRESS` | `0.0.0.0` | |
| `--listen-port` | `ACINFINITY_EXPORTER_LISTEN_PORT` | `8000` | Named `LISTEN_PORT`, because Kubernetes injects `ACINFINITY_EXPORTER_PORT` for a Service of that name |
| `--log-level` | `ACINFINITY_EXPORTER_LOG_LEVEL` | `INFO` | No level logs a response body |

The password is truncated to 25 characters before it is sent. The server does the same, and
the two reference clients that checked disagree on whether a longer password is truncated or
rejected. Truncation in the client removes the question.

### Endpoints

| Path | Response |
|---|---|
| `/metrics` | Prometheus exposition |
| `/health` | `ok`. Means the HTTP server is up, and nothing more |
| anything else | 404 |

`/health` does not report collection state on purpose. That is what
`acinfinity_collection_success` is for. A readiness gate on it would stop the scrape that
carries the bad news.

---

## Running it

### Docker

```bash
docker run --rm -p 8000:8000 \
  -e ACINFINITY_EMAIL='you@example.com' \
  -e ACINFINITY_PASSWORD='...' \
  -e ACINFINITY_EXPORTER_PORT_NAMES='{"1": "Closet exhaust T10"}' \
  ghcr.io/scottrus/prometheus-acinfinity-pro69:0.1.0
```

### Kubernetes, with the Helm chart

Create the Secret first, from your password manager. Its two keys must be exactly the names
below, because the chart consumes them with `envFrom`:

```bash
kubectl -n monitoring create secret generic acinfinity-exporter \
  --from-literal=ACINFINITY_EMAIL='you@example.com' \
  --from-literal=ACINFINITY_PASSWORD='...'
```

Then install:

```bash
helm install acinfinity-exporter \
  oci://ghcr.io/scottrus/charts/acinfinity-exporter --version 0.1.0 \
  --namespace monitoring \
  --set fullnameOverride=acinfinity-exporter \
  --set acinfinity.existingSecret=acinfinity-exporter \
  --set-json 'portNames={"1": "Closet exhaust T10"}' \
  --set vmServiceScrape.enabled=true
```

`fullnameOverride` is load-bearing. Without it the chart names every object
`<release>-acinfinity-exporter`, and that name becomes the `job` label on every series. A
later rename orphans all prior history.

The chart renders a `VMServiceScrape` for the VictoriaMetrics operator, or a `ServiceMonitor`
for the Prometheus operator. Enable one. Scrape interval and poll interval are independent:
a scrape reads process state and never reaches the vendor.

The exporter never calls the Kubernetes API, so the chart mounts no ServiceAccount token.

### Confirming it works

Before the first successful collection there are **no reading gauges at all**, only the three
exporter gauges. A fabricated 0 would read as healthy to a threshold rule, so absence is the
signal:

```promql
acinfinity_collection_success{job="acinfinity-exporter"}            # 1 after the first success
acinfinity_controller_vpd_kpa{job="acinfinity-exporter"}            # absent until then
acinfinity_port_info{job="acinfinity-exporter"}                      # shows the configured names
```

These metrics are **scraped, not pushed**. Do not wrap them in `last_over_time()`.

---

## Backfill: history before the first scrape

The vendor keeps about one record per minute of history. `acinfinity-backfill` reads a window
of it and writes each record to `/api/v1/import/prometheus` with its original timestamp.

```bash
acinfinity-backfill \
  --since 2026-08-01T00:00:00Z \
  --until 2026-09-02T14:00:00Z \
  --vm-url http://vmsingle:8428/api/v1/import/prometheus \
  --labels '{"job": "acinfinity-exporter"}'
```

Add `--dry-run` to read the window and print a summary without a write. `--controller` limits
the run to one `devId` or `devCode`.

Four things to know:

1. **It is a separate program, never a mode of the exporter.** A backfill materialises tens of
   thousands of rows. The exporter holds one small snapshot and never writes anywhere.
2. **Only fields with a live counterpart are written:** controller temperature, humidity and VPD,
   and per-port power level. Nothing else in a history row maps one-to-one onto a live gauge.
3. **Imported samples carry no `job` or `instance` label unless `--labels` adds them.** A scraped
   series and an imported series for the same reading are two series in the TSDB. Read them
   as one with `max by (controller_id) (...)`.
4. **Re-running the same window writes the same samples at the same timestamps.** A TSDB with
   deduplication enabled collapses them. Without it, the duplicates inflate `count_over_time`;
   pick a window that ends where the scraped series begins.

Two API facts the paging depends on: `pageNum` is ignored, so the cursor is `time`, set to the
last row's `createTime` plus one; and nibble `0xF` in `portSpead` marks an **empty** port on
this controller, verified against ports with resistance 65535, so it is skipped rather than
read as a level.

The Helm chart renders the backfill as a `Job` when `backfill.enabled` is true. Enable it, set
the window, upgrade, wait for completion, then disable it and upgrade again. The Job needs
egress to the TSDB; in a cluster with network policy, the TSDB's ingress policy must list it.

---

## Alerting

Four rules, on the shape used for every scraped exporter in the estate this was built for.
`up` alone is not enough: during a vendor outage the exporter stays healthy and `up` stays 1.

| Alert | Expression | For |
|---|---|---|
| `ACInfinityExporterDown` | `up{job="acinfinity-exporter"} == 0` | 10m |
| `ACInfinityExporterTargetMissing` | `absent(up{job="acinfinity-exporter"})` | 30m |
| `ACInfinityCollectionFailing` | `acinfinity_collection_success == 0` | 30m |
| `ACInfinityCollectionStale` | `time() - acinfinity_last_collection_timestamp_seconds > 900` | 15m |

`TargetMissing` is not redundant with `Down`. `up == 0` needs the target to still exist in the
scrape config. `CollectionStale` catches the third case: the process is up, the scrape works,
`collection_success` holds its last value of 1, and the poll loop is wedged. Only the timestamp
moves, so only the timestamp detects it.

---

## Relationship to `LukeEvansTech/acinfinity-exporter`

That project is the only other Prometheus exporter for this API, and it was the reference for
this one. This is a fresh implementation, not a fork. Ten findings in the upstream source
shaped it:

| Upstream | Here |
|---|---|
| Reads the field `vpd`; the API field is `vpdnums`, so VPD never populates | Reads `vpdnums`; a test asserts the spelling |
| Re-authenticates on HTTP 401 only; the API signals expiry as HTTP 200 plus a body code | Any non-success body code on a read earns one re-login and one retry |
| Calls `sys.exit(1)` when the first login fails, so a vendor outage at pod start is a CrashLoop | The server starts first; a failed login is a metric |
| Uses `http://` | HTTPS, with no option to change the scheme |
| Does not truncate the password to 25 characters | Truncates in the client |
| Reads the port field `state`; the API returns `loadState`, so the gauge is registered and never set | Reads `loadState` |
| `acinfinity_last_scrape_timestamp` has no unit, and "scrape" misnames a collection | `acinfinity_last_collection_timestamp_seconds`, `acinfinity_collection_success` |
| Collects twice at start | Once |
| Puts `devName` and `portName` on every gauge, so a rename ends every series | Names on the `*_info` gauges only, and port names come from configuration |
| Ships a fan-sync controller that writes to the API | No write path exists in this code |

---

## Development

```bash
make setup    # create .venv and install with dev extras
make check    # lint, tests, workflows, helm, docker
```

Every CI check is a `make` target, and the workflow calls the same targets. See
[CONTRIBUTING.md](CONTRIBUTING.md). **The git tag is the only version declaration**: a
local tree reports a dev version such as `0.1.0.dev3+g9e3b566`, and a release build gets
the tag through `SETUPTOOLS_SCM_PRETEND_VERSION`. The architecture, and the reasons behind each decision, is
in [ARCHITECTURE.md](ARCHITECTURE.md).

To re-capture the fixtures after a firmware or API change:

```bash
ACINFINITY_EMAIL=... ACINFINITY_PASSWORD=... .venv/bin/python scripts/capture-fixtures.py
```

The script redacts the account email, the Wi-Fi name, the MAC address and the vendor's opaque
keys before it writes anything. Read the diff before you commit a new capture.

## License

Apache-2.0. See [LICENSE](LICENSE).
