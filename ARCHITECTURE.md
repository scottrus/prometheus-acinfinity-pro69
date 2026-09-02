# Architecture

`prometheus-acinfinity-pro69` is two small programs in one image, and a Helm chart
that deploys them.

| Program | Shape | Reads | Writes |
|---|---|---|---|
| `acinfinity-exporter` | long-running, timer-driven | `devInfoListAll` once per poll | its own `/metrics` |
| `acinfinity-backfill` | one-shot | `devInfoListAll` once, then `log/dataPage` pages | `/api/v1/import/prometheus` |

Understanding why they are separate, and why the exporter holds so little state, is
most of this document.

---

## 1. The constraints that shape everything

### 1.1 There is no local API

The UIS Controller 69 PRO talks only to `www.acinfinityserver.com`. Every client that
has been written for it does the same, the vendor app included, even on the same LAN.
The controller's Bluetooth path exists, and the manual states that a Bluetooth link
disables Wi-Fi. So every collection is one WAN round trip to a vendor cloud, and a
WAN outage stops the data.

**Consequence:** the exporter must survive a vendor outage as a running process with a
failure metric, never as a crash. A restart loop during an outage destroys the evidence
and stops the scrape that would carry the bad news.

### 1.2 Success is in the body, not the status

The API answers HTTP 200 to almost everything. The body carries a `code` field, and 200
there is the only success. An expired session is HTTP 200 with a non-200 code. An
exporter that watches the HTTP status for expiry logs an error forever and never logs
in again.

**Consequence:** the client treats any non-success body code on a read as a possible
expiry, logs in once, and retries once. A second failure is raised.

### 1.3 The token rotates, and it is a full account session

Two logins seconds apart return two different 32-character tokens. The token is a
session, not a user id. And it can write to every controller on the account. The
vendor offers no read-only credential.

**Consequence:** the client never caches a token across a failure, and the security
posture lives outside this code: a second account with the controller shared to it.
See [SECURITY.md](SECURITY.md).

### 1.4 The login response echoes the password

The login body carries the password back in `appPasswordl`, with a `refreshToken` and
a `secretId`. The device list carries the account email on every record.

**Consequence:** no code path logs a response body at any level. Error messages carry
the body `code` and `msg` only. A test asserts the password does not reach the log on
a failed login.

### 1.5 The vendor keeps about one record per minute of history

`log/dataPage` returns one row per minute, oldest first, through a `time` cursor.
`pageNum` is ignored. The history spelling of the VPD field is `vpdNums`; the live
spelling is `vpdnums`.

**Consequence:** history is worth importing, once, at first deployment. It is not worth
holding in the exporter.

---

## 2. The exporter

```
                 timer (pollInterval)
                        |
   vendor cloud <-- Poller.collect_once() --> collect_snapshot() --> ACInfinityCollector
                                                                          |
                          vmagent  <---------- /metrics  <---- WSGI ------+
                          kubelet  <---------- /health
```

### 2.1 Collection is timer-driven, never scrape-driven

`Poller` is a thread. It polls once at start, then waits on an Event for
`pollInterval` seconds. A scrape reads the collector's last snapshot and never reaches
the vendor. If scrapes drove collection, a second vmagent replica or one manual `curl`
would double the vendor calls.

The Event, rather than `time.sleep`, is what makes SIGTERM prompt: the loop wakes at
once instead of after up to a minute.

### 2.2 One snapshot, replaced wholesale

`ACInfinityCollector` holds exactly one `Snapshot`. `update()` replaces it.
`mark_failure()` keeps it and drops `success`. Time-series history is the TSDB's job,
and a second copy of it in process memory is how a cache becomes a leak.

Two failure states are handled differently, on purpose:

| State | Reading gauges | `collection_success` |
|---|---|---|
| cold: never collected | absent | 0 |
| warm: collected, then failed | last known values retained | 0 |

A fabricated 0 C before the first success reads as healthy to a threshold rule. A
retained value after a later failure was true recently, and
`last_collection_timestamp_seconds` says how recently.

### 2.3 Identity is `controller_id` and `port`

Every measurement gauge carries `controller_id`, and per-port gauges add `port`.
Nothing editable goes on them. The vendor app lets a user rename a controller or a
port at any time, and a label that changes ends every series it sits on, with no
error. Names live on `controller_info` and `port_info` only.

`port_info` carries two names: `port_name`, from the exporter's configuration, and
`vendor_port_name`, from the app. The configured name wins. A dashboard that joins on
`port_info` for a display name therefore reads a value that only a configuration
change can move.

### 2.4 Absence over zero

A missing field yields no sample, never a 0. `port_resistance_ohms` is absent on an
empty port rather than 65535, the open-circuit sentinel. `port_connected` is absent
when the firmware does not report resistance at all, because absence must not read as
a short circuit.

Six fields are excluded outright: `insideTemp`, `insideTempF`, `outsideTemp`,
`outsideTempF`, `leafTemp` and the `thermal*` family. On a controller with no such
probe they read 0, and `insideTempF` reads 3200, which is 32.00 F, which is 0 C. A
test asserts the strings `3200` and `32.0` never reach the exposition.

### 2.5 The server starts first

`main()` starts the HTTP server, then starts the poller. A failed first login is a log
line and `collection_success 0`. The container health check and the Kubernetes probes
hit `/health`, which means only that the server is up.

### 2.6 Login backoff

A refused login blocks further login attempts for 300 s. Without that, a wrong
password is one login per poll, forever. Nothing about the vendor's rate limits on
login is documented, and one attempt per five minutes is defensible without knowing
them.

---

## 3. The backfill

```
   vendor cloud <-- iter_history() --> samples_from_row() --> VictoriaMetricsWriter --> TSDB
                    (time cursor)       (decode per row)       (batches of 10,000 lines)
```

### 3.1 A separate program

A backfill materialises tens of thousands of rows and holds a write connection to the
TSDB. The exporter holds one small snapshot and never writes anywhere. Keeping them
apart keeps the exporter's memory bound small, and its network policy at ingress only.
In the chart, the backfill is a `Job` with its own `app.kubernetes.io/component`
label, so a policy on the TSDB can admit the Job and not the exporter.

### 3.2 Paging

The cursor is `time`. Each page is sorted by `createTime`, rows at or before the last
yielded time are skipped, and the next request starts at the last yielded time plus
one. A page that yields nothing new ends the walk, so a server that repeats a boundary
row cannot spin the loop.

### 3.3 Decode

Only fields with a live counterpart are written: temperature, humidity, VPD (from
`vpdNums`), and per-port power level from the `portSpead` nibbles. The nibble loop is
bounded by the controller's `devPortCount` from the device list, because the history
rows carry null there and the bits above the real port count read 0, not 0xF. A fixed
width of 8 would publish four phantom ports at level 0.

Nibble `0xF` is skipped. On this controller it appears on ports whose live
`portResistance` is 65535, so it marks an empty port, not an "on" state.

### 3.4 Labels and duplicates

Imported samples carry `controller_id`, `port` where relevant, and whatever `--labels`
adds. They carry no `job` or `instance` unless configured. A scraped series and an
imported series for the same reading are therefore two series; read them as one with
`max by (controller_id) (...)`.

Re-running the same window writes the same samples at the same timestamps. A TSDB
with deduplication collapses them; one without it inflates `count_over_time`. The
safe window ends where the scraped series begins.

---

## 4. The chart

- `fullnameOverride` is load-bearing. The object name becomes the `job` label on every
  series, and a later rename orphans all history.
- The Secret is consumed with `envFrom`, so its keys must be exactly
  `ACINFINITY_EMAIL` and `ACINFINITY_PASSWORD`.
- `enableServiceLinks: false` on the pod. Kubernetes injects `<SVCNAME>_PORT` for every
  Service in the namespace, and a Service named `acinfinity-exporter` injects
  `ACINFINITY_EXPORTER_PORT`, which shares a prefix with every setting. The exporter
  also names its own port setting `LISTEN_PORT`, so the collision is closed twice.
- Numeric values pass through `int64` before `quote` in the ConfigMap. A values file
  yields float64 and `--set` yields int64, and a float renders as `6e+01`.
- `portNames` is rendered with `toJson`, so a name with a comma or an equals sign
  survives.
- No ServiceAccount token is mounted. The exporter never calls the Kubernetes API.
- `readOnlyRootFilesystem`, uid 65532, all capabilities dropped, a 16 Mi tmpfs at
  `/tmp`. The image is a Chainguard runtime with no shell.

---

## 5. Observability of the exporter itself

| Metric | Detects |
|---|---|
| `up` | the process is gone or unreachable |
| `acinfinity_collection_success == 0` | the vendor refused or is unreachable; `up` stays 1 |
| `time() - acinfinity_last_collection_timestamp_seconds` | the poll loop is wedged while the scrape still works |
| `absent(up{job=...})` | the scrape target itself was removed |

All four are needed. Each sees a failure the others cannot.

---

## 6. Deliberate non-goals

- **No write path.** No fan sync, no mode setter, no automation client. The credential
  could write; the code cannot.
- **No `sensors[]` decode.** This hardware returns null for it. A contribution with a
  real capture is welcome.
- **No Fahrenheit.** One unit per measurement.
- **No local transport.** There is none to build on.
