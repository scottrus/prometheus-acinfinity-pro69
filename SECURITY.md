# Security

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/scottrus/prometheus-acinfinity-pro69/security/advisories/new)
rather than a public issue.

## The credential is the whole threat model, and it cannot be scoped

This exporter is a long-running network service that holds an AC Infinity account
email and password in memory, and a session token derived from them. **The vendor
offers no read-only key, no per-device grant, and no scope of any kind.** The token
that lists a controller's readings is the same token that sets its fan speed.

That is a different situation from an exporter with a scopable key. Least privilege
is not available at the credential. It is available one level up:

**Create a second AC Infinity account, and share the controller to it from the
app's Account page.** Give the exporter that account. A leak then costs one shared
controller, not the account that owns it and every other device on it.

Two facts about a shared account, both verified live on 2026-09-01:

- A shared account sees the controller in full: every port, every reading, the
  firmware version.
- **A shared account can write.** `isShare: 1` does not mean read-only. The fan was
  set to ON at level 3 from a shared account, and the change was confirmed
  physically.

So sharing bounds the blast radius to the shared device. It does not make the
credential harmless. Network egress policy is the remaining control, and it belongs
to the deployment, not to this repository.

## What the exporter calls

Every vendor operation it performs, in full:

| Call | Purpose | Who |
|---|---|---|
| `POST /user/appUserLogin` | obtain a session token | exporter, backfill |
| `POST /user/devInfoListAll` | read every controller, port and sensor | exporter, backfill |
| `POST /log/dataPage` | read history rows for one controller | backfill only |

All three are reads. **No code path writes to a controller.** There is no fan-sync
mode, no mode setter, and no automation client.

This table is the privilege surface, and changes to it are announced:

- **A new read call** is a **minor** bump, called out under its own heading in the
  changelog, with this table updated in the same change.
- **Any call that writes** would be a **major** bump, and is not a direction this
  project intends to take. Adding one changes what the credential here can cost you.

## What the API hands back, and what the client does with it

- **The login response echoes the password**, in a field named `appPasswordl`, and
  carries a `refreshToken` and a `secretId`. The client copies the token out and
  drops the response. It is never logged, at any level, and a test asserts that the
  password does not reach the log on a failed login.
- **Every device record carries the account email.** No log line includes a response
  body. An error message carries the body `code` and `msg` only.
- **The password is truncated to 25 characters** before it is sent, because the
  server does that anyway.
- **HTTPS is fixed.** The base URL is a constant and there is no option to change the
  scheme.

## Handling the credential

- Credentials are read from the environment **only**. There is no flag, because argv
  is visible in `ps`, in a container's `spec.containers[].args`, and in crash dumps.
  The test suite asserts the parser rejects such a flag.
- In Kubernetes, use a Secret, either rendered by the chart or referenced with
  `acinfinity.existingSecret`. Create it from your password manager, never from a
  transcript.
- The exporter never calls the Kubernetes API, and the chart does not mount a
  ServiceAccount token.
- `scripts/capture-fixtures.py` reads the credential from the environment, redacts
  the account email, the Wi-Fi name, the MAC address and the vendor's opaque keys,
  and never writes the login response. Read the diff before you commit a capture.

## What the exporter exposes

`/metrics` is unauthenticated, as exporters conventionally are. It publishes readings
and states per controller and port, the controller name, the configured and vendor
port names, the firmware version, the device code and the time zone. **No email, no
credential, no Wi-Fi name, no MAC address.** Treat the name labels as you would any
label: visible to anything that can scrape the endpoint.
