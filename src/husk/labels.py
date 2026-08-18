"""Runner labels, derived from pool facts.

A pool does not list its labels; it states facts (arch, size class, GPU vendor,
whether /cvmfs is mounted) and this module computes the label set from them.
Hand-written lists drift: a pool that advertises `gpu` after its GPU host is
removed still collects GPU jobs, and nothing in the config disagrees. Deriving
them makes that unrepresentable — the label exists because the fact does.

Two namespaces, split by *who owns the meaning*:

  capability (unprefixed)  A property of the machine, portable across providers.
                           `cuda` means "CUDA works against a real device here",
                           and would mean the same on hardware husk never touched.
                           A workflow author asking for one is describing the job's
                           requirement, not naming a supplier.

  husk-* (reserved)        husk's own taxonomy — pool, backend, size class. These
                           are meaningless except in reference to husk ("large"
                           is our bucketing, not a property of the box), so they
                           carry the prefix and operators may not mint their own.

The split matters for one specific reason: an accelerator label must never bake
the provider into every workflow file, because the jobs that want it are exactly
the ones most likely to move between fleets. `husk-cuda` would make a job that
says what it needs into a job that says who it buys from.

WHAT THE ACCELERATOR LABELS MEAN: a device is attached. Not "the toolkit is
installed" — the toolkit arrives in the job's container image, and the majority
of CUDA/HIP/SYCL jobs in the wild only *compile*, which any CPU slot can do.
Reading `cuda` as "toolkit present" would route every build leg onto the
scarcest hardware in the fleet. Compile-only jobs belong on a CPU pool with a
CUDA image.

WHY GPU POOLS HAVE NO SIZE LABEL: if a GPU pool carried `husk-size-large`, then
`runs-on: [..., husk-size-large]` would start matching GPU hardware, which is
the same leak the size dimension exists to prevent, one level down. Accelerator
labels *replace* the size dimension rather than stacking with it. The cost is
that "a large GPU box" is inexpressible until there is more than one GPU shape,
at which point `gpu-<model>` says it more precisely anyway.

WHAT `discoverable = false` MEANS: everything above serves *discovery* — a job
states a requirement and GitHub finds any slot that meets it. A pool can opt out
of that entirely, and then it registers as `husk-pool-<name>` plus operator
extras, nothing else. Not even `self-hosted`, because `runs-on: self-hosted` is
exactly the accidental selection the opt-out exists to prevent.

The capability labels go too, and that is the part worth explaining: keeping
`cvmfs` on an undiscoverable pool would leave `runs-on: [cvmfs]` matching it, so
every capability retained is a matching surface the operator has to keep
auditing. Dropping them costs nothing, because selection by identity settles the
capabilities implicitly — an author who names one pool has already chosen the
machine, and the name says more about it than any label could. The two modes do
not stack; a pool is in the discovery set or it is not.

Extras still apply, and that is the escape hatch: the capability namespace is not
reserved, so a pool that genuinely wants to advertise one writes it into
`extra_labels` and opts back in deliberately. Exclusivity fails closed, and every
exception to it is greppable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# Reserved: only this module may mint labels in this namespace. Operator extras
# carrying it are rejected at config load — otherwise a hand-written
# `husk-size-large` on a standard pool would contradict the derived set, and the
# whole point is that there is one source of truth.
HUSK_PREFIX = "husk-"

# GitHub's own spelling is what `runs-on` examples and the runner's self-assigned
# labels use, so it is what workflow authors will type. The uname spelling is
# emitted alongside as an alias because people reach for it out of habit and a
# missed match is an eternally-queued job, not an error.
ARCH_ALIASES = {"x64": "x86_64", "arm64": "aarch64"}

# The vendor's compute runtime — the thing a job actually links against, and a
# better selector than the vendor name for that reason.
GPU_RUNTIMES = {"nvidia": "cuda", "amd": "rocm"}

# GitHub splits label lists on commas, so one in a label silently becomes two
# labels; whitespace-only or empty entries register but can never be selected.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _slug(name: str) -> str:
    """Pool name → label-safe tag (mirrors config._slug's rules)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "pool"


def pool_label(pool_name: str) -> str:
    """The label that names one pool — its identity, and the only label an
    undiscoverable pool answers to."""
    return f"{HUSK_PREFIX}pool-{_slug(pool_name)}"


def _dedup(labels: Sequence[str]) -> list[str]:
    """First occurrence wins, case-insensitively — so an extra that repeats a
    derived label is a no-op rather than a double registration."""
    seen: set[str] = set()
    return [x for x in labels if not (x.lower() in seen or seen.add(x.lower()))]


def check_extra_label(label: str) -> str:
    """Validate one operator-supplied extra label, or raise ValueError.

    Extras are free to mint *capabilities* — a pool genuinely knowing something
    about itself that husk has no vocabulary for is a real case, and gatekeeping
    it would just push people back to hand-written lists. What they may not do is
    reach into the husk-* namespace, where they would be claiming to be derived.
    """
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"label {label!r} must be alphanumeric with . _ - (no commas — GitHub "
            "splits label lists on them, so one label would register as two)"
        )
    if label.lower().startswith(HUSK_PREFIX):
        raise ValueError(
            f"label {label!r} uses the reserved {HUSK_PREFIX}* namespace — those are "
            "derived from pool facts (backend type, size, gpu, cvmfs), not written "
            "by hand. State the fact instead and the label follows."
        )
    return label


def derive_labels(
    *,
    pool_name: str,
    backend_type: str,
    arch: str = "x64",
    size: str | None = "standard",
    gpu_vendor: str = "",
    gpu_model: str = "",
    cvmfs: bool = False,
    extra: Sequence[str] = (),
    discoverable: bool = True,
) -> list[str]:
    """The full label set for one pool, in a stable order.

    Order is grouped (GitHub baseline → husk taxonomy → capabilities → extras)
    purely so that the startup log and the dashboard read consistently; GitHub
    itself treats the set as unordered. Duplicates are dropped keeping the first
    occurrence, so an extra that repeats a derived label is a no-op rather than a
    double registration.

    `size` is None for accelerator pools — see the module docstring.

    `discoverable=False` collapses the set to identity plus extras, so the pool is
    reachable only by a selector that names it. Also in the module docstring, and
    the reason this returns early rather than filtering at the end: the derived
    labels are not *suppressed* one by one, they are never claimed.
    """
    if not discoverable:
        # Always ≥1 label, which GitHub's JIT registration requires.
        return _dedup([pool_label(pool_name), *extra])

    out: list[str] = ["self-hosted", "linux", arch]
    if alias := ARCH_ALIASES.get(arch):
        out.append(alias)

    out += ["husk", pool_label(pool_name), f"husk-backend-{backend_type}"]
    if size:
        out.append(f"husk-size-{size}")

    if gpu_vendor:
        out.append("gpu")
        out.append(f"gpu-{gpu_vendor}")
        if gpu_model:
            out.append(f"gpu-{_slug(gpu_model)}")
        if runtime := GPU_RUNTIMES.get(gpu_vendor):
            out.append(runtime)

    if cvmfs:
        out.append("cvmfs")

    out += list(extra)
    return _dedup(out)


def arch_labels(labels: Sequence[str]) -> list[str]:
    """The subset of `labels` naming a CPU architecture (either spelling)."""
    known = set(ARCH_ALIASES) | set(ARCH_ALIASES.values())
    return [x for x in labels if x in known]


def class_labels(labels: Sequence[str]) -> list[str]:
    """The subset of `labels` naming a hardware class — a size or an accelerator.

    These are alternatives, not a hierarchy: an accelerator label replaces the
    size label rather than joining it (see the module docstring), so either kind
    satisfies the dimension.
    """
    runtimes = set(GPU_RUNTIMES.values())
    return [
        x
        for x in labels
        if x.startswith("husk-size-")
        or x == "gpu"
        or x.startswith("gpu-")
        or x in runtimes
    ]


# The dimensions a `runs-on` selector must pin, and why leaving one out is not a
# style problem. GitHub matches a runner carrying *all* of a selector's labels,
# so every dimension a selector omits is a dimension it accepts ANY value of —
# and it silently acquires new values as the fleet grows.
#
#   arch   `[self-hosted, husk-size-standard]` matches an arm64 slot the day one
#          exists. Unlike the class dimension this cannot be fixed by naming:
#          size and accelerator collided because they name the same axis, so one
#          could replace the other, but arch is orthogonal (arm64-large is a real
#          combination) and folding it in would multiply the vocabulary.
#
#   class  `[self-hosted, linux, x64]` matches GPU slots, which carry all three
#          labels and more.
#
# `husk` satisfies neither: it never narrows anything, which is exactly why it is
# an enumeration label rather than a routing one.
#
# A `husk-pool-*` pin satisfies neither either, and deliberately so: it IS exact
# (a pool is one hardware shape), but it is exact by naming a supplier rather than
# by stating a requirement, and blessing it would make "specific" ambiguous
# between the two. A discoverable pool's jobs should say what they need.
_DIMENSIONS = {"arch": arch_labels, "class": class_labels}


def underspecified(
    labels: Sequence[str], *, undiscoverable_pools: Sequence[str] = ()
) -> list[str]:
    """Dimensions this `runs-on` selector fails to pin, in a stable order.

    Empty means the selector names exactly the hardware it means to. Exposed so
    the dashboard and any workflow linter share one definition of "specified"
    rather than each re-deriving it — and so the answer changes in one place when
    a dimension is added.

    `undiscoverable_pools` names the pools running with `discoverable = false`; a
    selector pinning one of those is complete by construction, because naming it
    is the only way to reach it and no requirement-shaped selector ever will. The
    caller passes the set because this is a pure function over a selector — the
    label alone cannot say which kind of pool it names.
    """
    exempt = {pool_label(p) for p in undiscoverable_pools}
    if exempt.intersection(labels):
        return []
    return [name for name, pick in _DIMENSIONS.items() if not pick(labels)]


# --------------------------------------------------------------- matching back
# Everything above answers "what labels does this pool register?". The two
# functions below answer the inverse — "which pool could serve this `runs-on`?" —
# which is the question a webhook delivery asks, since a `workflow_job` names a
# selector and never a pool.

# Sentinels for the `served_by` metric dimension. Neither is a legal pool name
# (config slugs cannot collide with them by accident in any query that matters),
# so a reader can always tell an attributed job from an unattributed one.
SERVED_BY_NONE = "none"
SERVED_BY_MULTIPLE = "multiple"


def serving_pools(
    runs_on: Sequence[str], pool_labels: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    """Names of the pools whose registered labels can serve `runs_on`, sorted.

    GitHub's own rule, mirrored: a runner matches when it carries **every** label
    the job asked for; labels it carries in addition are ignored. Subset, not
    equality — a pool advertising nine labels serves `runs-on: [self-hosted]`, and
    an equality test would attribute almost nothing.

    Case-insensitive, because GitHub compares labels that way and because
    `derive_labels` emits `linux` while workflow authors habitually write `Linux`.
    Getting this wrong fails in the quiet direction: every job looks unservable.

    An empty selector matches nothing rather than everything. A job whose labels
    husk could not read is not a job every pool can serve.
    """
    if not runs_on:
        return ()
    want = {x.lower() for x in runs_on}
    return tuple(
        sorted(
            name
            for name, have in pool_labels.items()
            if want <= {x.lower() for x in have}
        )
    )


def served_by(runs_on: Sequence[str], pool_labels: Mapping[str, Sequence[str]]) -> str:
    """`runs_on` collapsed to ONE label value drawn from a bounded vocabulary.

    This exists because of where its output goes. The job histograms and counters
    are event-time instruments, and `husk.metrics_store` persists those to disk —
    so a label value that any repo in the installation can invent would let a
    single typo (`runs-on: [self-hosted, husl-x64]`) mint a series that lives in
    that file for ever. The raw labelset is exactly such a value. Collapsing it
    against the configured pools bounds the dimension by config, which is the rule
    the whole event-time half is built on.

    The live `husk_jobs_queued` gauge keeps the full labelset instead, and is right
    to: a collector-derived series stops being emitted the moment the queue drains,
    so nothing accumulates.

    `multiple` is a real answer, not a failure — `runs-on: [self-hosted]` genuinely
    matches every discoverable pool. `none` covers the jobs bound for GitHub-hosted
    runners, which huskd is told about too and must not silently count as its own.
    """
    matches = serving_pools(runs_on, pool_labels)
    if not matches:
        return SERVED_BY_NONE
    if len(matches) > 1:
        return SERVED_BY_MULTIPLE
    return matches[0]
