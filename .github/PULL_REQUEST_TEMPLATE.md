## What this changes

<!-- And why. If it fixes a bug, what was the wrong behaviour? -->

## Checks

- [ ] `make check` passes locally
- [ ] Tests cover the behaviour that would silently regress, not only the happy path

## Things that need a callout

- [ ] **Metric renamed or removed**: major version bump; it breaks dashboards and rules silently
- [ ] **New vendor API call**: privilege-surface change; update the table in
      [SECURITY.md](../SECURITY.md) in this same PR
- [ ] **Fixture re-captured**: note the date and the firmware version it came from
- [ ] None of the above

## Anything you could not verify

<!-- The session-expiry path and the backfill write path have not run against the live
     vendor or a live TSDB. Say so rather than imply otherwise. -->
