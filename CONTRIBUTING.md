# Contributing

Contributions are welcome, particularly from people with controller shapes this has
not seen: more than one controller on an account, an external sensor probe, or an AI+
controller (`newFrameworkDevice: true`). See the "what has not been exercised" section
of the README; that list is where the real gaps are.

## The workflow

`main` is protected. All changes land through a pull request.

1. Branch from `main`.
2. Make the change.
3. **Run `make check` locally.** This is the same gate the PR faces.
4. Open a PR.

## Run the checks before you push

Every check that runs in CI is defined in the `Makefile` and nowhere else. The
workflow calls the same targets.

```bash
make setup    # one-off: create .venv and install
make check    # lint, tests, workflows, helm, docker
```

Individual gates, for a faster loop:

```bash
make lint            # ruff check, ruff format --check
make fmt             # apply formatting and autofixes
make test            # pytest
make actionlint      # workflow syntax, expressions, shellcheck on run: blocks
make actions-pinned  # every uses: is SHA-pinned with a version comment
make helm            # helm lint, template permutations, required values, kubeconform
make docker          # hadolint, image build, smoke test
make scan            # grype CVE scan (run make docker first)
```

A tool you do not have installed is reported as `SKIP` rather than a failure. CI sets
`REQUIRE_ALL=1`, which turns every skip into a failure.

Optional extras, for the full local gate:

```bash
brew install helm kubeconform hadolint grype actionlint
```

## What a good change looks like

- **Comments explain why, not what.** The code is commented at the points where a
  reader would otherwise ask why. Match that.
- **Tests assert behaviour that would silently regress.** The valuable ones here are
  the two spellings of the VPD field, the session expiry that arrives as HTTP 200, the
  placeholder temperatures that must never be published, and the nibble width that
  would otherwise publish phantom ports.
- **Fixtures come from real API output**, redacted by `scripts/capture-fixtures.py`,
  and the test module docstring says when and from what firmware. A fixture pins the
  format it was captured from and does not prove the live format still matches.

## Things to be careful with

**Never put an editable name on a measurement gauge.** A rename in the app ends every
series that carries it. Names go on the `*_info` gauges only.

**Never log a response body.** Every device record carries the account email, and the
login response carries the password.

**Renaming or removing a metric is a major version bump.** It silently breaks
dashboards and alerting rules downstream.

**Adding a vendor API call changes the privilege surface.** See
[SECURITY.md](SECURITY.md). A new read is a minor bump announced in the changelog;
anything that writes is major and is not a direction this project intends to take.
