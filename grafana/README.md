# Grafana

**The dashboard does not live here.** It is `monitoring/grafana/husk.json` in
[acts/monitoring](https://gitlab.cern.ch/acts/monitoring) on CERN GitLab,
alongside the Prometheus config that scrapes huskd and the kustomization that
deploys both. That repo has since grown other components beside `monitoring/`,
which is why the dashboard sits a directory deeper than the repo name suggests.

This repo carried a copy of it, and the copy went stale in the way copies do —
not evenly, but in both directions at once. The deployed dashboard grew a
`Broken` stat, a `Slots husk cannot act on` panel and the whole cloud-init
self-timing row while this one didn't; this one kept two boot-timing panels built
on `husk_boot_phase_seconds_bucket`, a histogram that stopped existing when the
console-log exfil was superseded (the metric is a *gauge* now, published by the
guest's own `husk-bootreport`). Two files, one of them deployed, and no way to
tell from either which was right. So: one file, in the repo that deploys it.

What stays here is the contract that file depends on — `../observability.md` is
the reference for what huskd exposes and why each metric has the shape it does.

## The one coupling worth knowing about

`husk_slot_state_code` is the slot's classified state as a small integer, because
a Grafana state-timeline colours a lane by a numeric field value. The **decode
table is the panel's value mappings**, over in the other repo, and the encoding
is `SlotState`'s declaration order (`src/husk/slot.py`, codes from 1).

Adding or reordering a state therefore silently relabels every lane on the
deployed dashboard. `tests/test_metrics.py` pins the encoding so the change
cannot pass unnoticed — when it fails, update the mappings in acts-monitoring and
then update the test. The panel may also map codes *above* the enum's range as
overlays that win over the classified state; `broken` (7, from
`husk_slot_failing_seconds`) is one, and huskd knows nothing about it.

## Editing

**The file is the source of truth, not the UI.** `husk.json` is provisioned — baked
into a ConfigMap and mounted into Grafana — so the live dashboard is read-only and
Save is disabled. Edit the file and `just deploy` (a pod roll, ~15s). For anything
beyond a small tweak the practical route is *Save as…* a private copy, edit that in
the UI, export it over `husk.json` keeping `uid: husk-fleet` and dropping any `id`
field, then delete the copy. The full procedure is in that repo's
`monitoring/grafana/README.md`.
