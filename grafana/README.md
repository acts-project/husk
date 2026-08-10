# Grafana

`husk.json` — the fleet dashboard. Import it (Dashboards → New → Import → upload
the file) and pick your Prometheus datasource; it is referenced through a
`datasource` variable, so nothing is pinned to a datasource UID.

It reads the two layers `observability.md` describes, and joins them on the
`backend` + `slot` labels that huskd's `/sd/targets` feed attaches to every guest
scrape:

- **control-plane facts** from huskd's own `/metrics` (slot states, recycle and
  boot timing, reconcile health, image storage), and
- **in-guest resource metrics** from each runner VM's node_exporter (CPU, memory,
  filesystem fill, disk, network).

Panels that mix the two — anything in the *Runner VMs* row — need both scrape
jobs present, and only show slots whose runner is online, since those are the
only ones huskd publishes as targets.

## Variables

| variable | what it does |
|---|---|
| `datasource` | which Prometheus to query |
| `backend` | filter to one husk pool (`[[pool]]` name) |
| `slot` | filter to individual slots |
| `window` | averaging window for rates and histogram quantiles |

`window` exists because recycles are rare events: at a 5m window the percentiles
on the bring-up row are mostly noise. Widen it until the "Recycles in window"
count is a number you'd trust.

## Notes on two panels

**Slot state** (the status plot) reads `husk_slot_state_code` — the classified
state as a small integer, decoded back into a name by the panel's value mappings.
It used to *derive* the state instead, by asking which state was accruing ~1 s/s
in the seconds-in-state counter, and that quietly invented states: `rate()`
extrapolation let two states clear the "more than half the interval" test at
once, and the ordinals were summed, so an ordinary busy→poweroff recycle rendered
as `error`. See the `husk_slot_state_code` section in `../observability.md`. The
codes come from `SlotState` declaration order, so if you add a state, add its
mapping here too — `tests/test_metrics.py` fails until you do.

**Filesystem headroom** describes filesystems, not directories, so two `kind`s on
one disk report identical numbers. Aggregate with `min()`/`max()`, never `sum()`.

## Editing

Grafana is the editor: change it in the UI, then export via *Dashboard settings →
JSON Model* (or *Share → Export*) and overwrite `husk.json`. Keep `uid:
husk-fleet` so re-imports update in place instead of forking a copy.
