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

**Slot state** (the status plot) is derived, not read directly: huskd exposes
state as a per-pool *count* (`husk_slots`) and a per-slot cumulative
seconds-in-state *counter*, never as a per-slot categorical. The panel recovers
the current state by asking which state is accruing ~1 s/s right now. A slot
caught mid-transition splits its rate between two states and shows one interval
of "transition" — that is the derivation showing through, not a real state.

**Filesystem headroom** describes filesystems, not directories, so two `kind`s on
one disk report identical numbers. Aggregate with `min()`/`max()`, never `sum()`.

## Editing

Grafana is the editor: change it in the UI, then export via *Dashboard settings →
JSON Model* (or *Share → Export*) and overwrite `husk.json`. Keep `uid:
husk-fleet` so re-imports update in place instead of forking a copy.
