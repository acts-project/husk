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
"""

from __future__ import annotations

import re
from collections.abc import Sequence

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
) -> list[str]:
    """The full label set for one pool, in a stable order.

    Order is grouped (GitHub baseline → husk taxonomy → capabilities → extras)
    purely so that the startup log and the dashboard read consistently; GitHub
    itself treats the set as unordered. Duplicates are dropped keeping the first
    occurrence, so an extra that repeats a derived label is a no-op rather than a
    double registration.

    `size` is None for accelerator pools — see the module docstring.
    """
    out: list[str] = ["self-hosted", "linux", arch]
    if alias := ARCH_ALIASES.get(arch):
        out.append(alias)

    out += ["husk", f"husk-pool-{_slug(pool_name)}", f"husk-backend-{backend_type}"]
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

    seen: set[str] = set()
    return [x for x in out if not (x.lower() in seen or seen.add(x.lower()))]


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
_DIMENSIONS = {"arch": arch_labels, "class": class_labels}


def underspecified(labels: Sequence[str]) -> list[str]:
    """Dimensions this `runs-on` selector fails to pin, in a stable order.

    Empty means the selector names exactly the hardware it means to. Exposed so
    the dashboard and any workflow linter share one definition of "specified"
    rather than each re-deriving it — and so the answer changes in one place when
    a dimension is added.
    """
    return [name for name, pick in _DIMENSIONS.items() if not pick(labels)]
