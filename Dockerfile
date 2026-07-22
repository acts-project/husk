# huskd daemon image. Two stages so the (slow, rarely-changing) dependency solve
# is cached independently of the app code: an edit under src/husk/ reuses the
# dependency layer and only re-runs the fast project install.
#
# This image includes the `libvirt` extra, so huskd can drive the libvirt/QEMU
# backend as well as OpenStack. libvirt-python ships no PyPI wheels, so it is
# compiled from source here: the builder needs libvirt-dev + a C compiler, and
# the runtime needs the matching libvirt client library (libvirt0). Both come
# from the same Debian release, so the compiled binding and the runtime .so agree.

# ---- builder: resolve deps, then build+install the husk wheel into /app/.venv ----
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

# Toolchain to compile libvirt-python: pkg-config + gcc, the libvirt headers, and
# libc6-dev for the standard C headers (assert.h &c.) — gcc alone doesn't pull it
# in under --no-install-recommends. (The slim python base carries the CPython
# dev headers already.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends pkg-config gcc libc6-dev libvirt-dev \
    && rm -rf /var/lib/apt/lists/*

# Byte-compile on install (faster cold start) and copy rather than hardlink out of
# the uv cache mount (the cache lives on a different filesystem than /app/.venv).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: install ONLY third-party deps (incl. the libvirt extra) from
# the locked graph, with no project code present. Bind-mounting the manifests
# (instead of COPY) keeps them out of this layer, so this step's cache key is
# purely pyproject.toml + uv.lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project --extra libvirt

# Project layer: now bring in the source and install husk itself as a wheel
# (--no-editable, so the venv is self-contained and we can drop /app/src at
# runtime). Only this layer is invalidated by a code change.
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra libvirt

# ---- runtime: interpreter + built venv + libvirt client, no uv, no toolchain ----
FROM python:3.13-slim-trixie

# libvirt0 provides libvirt.so.0 that the compiled binding links against;
# openssh-client is the transport huskd uses to reach libvirt hosts (qemu+ssh://)
# and to bridge each guest's metrics scrape over the SSH channel it already holds.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libvirt0 openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Run as non-root; the lock lives under /tmp and config is mounted read-only.
RUN groupadd --system husk && useradd --system --gid husk --home-dir /app husk

COPY --from=builder --chown=husk:husk /app/.venv /app/.venv

# Put the venv on PATH so `huskd` (the console script) resolves without activation.
# HOME is pinned so `~` expands to /app for both openstacksdk (~/.config/openstack/
# clouds.yaml) and the libvirt/guest-scrape SSH transport (~/.ssh).
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/app \
    HUSK_LOG_LEVEL=INFO

USER husk
WORKDIR /app

# Dashboard + /status /metrics /healthz /events. Bind 0.0.0.0 in your config
# (controller.http_addr) to reach it from outside the container.
EXPOSE 9100

# /livez, not /healthz: the latter is 503 until a pool has reconciled recently, and
# a cold start's golden-image upload can outlast any start-period — which would mark
# the container unhealthy (and restart it, under an orchestrator that acts on that)
# mid-upload, forever. Reconcile staleness is an alerting signal, not a restart one.
# Assumes the default :9100 port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/livez', timeout=3).status==200 else 1)"]

# huskd IS the ASGI host: it drives hypercorn on the main event loop AND runs the
# reconcile loop on a background thread under one process-wide lock, with
# SIGTERM/SIGINT wired to graceful shutdown. So there is no separate uvicorn/ASGI
# entrypoint to add — an external ASGI server would serve the Quart surface but
# skip the reconcile loop entirely. (huskd is a single-command Typer app, so the
# daemon is `huskd <opts>` with no subcommand.) Mount config + secrets at these paths.
ENTRYPOINT ["huskd"]
CMD ["--config", "/etc/husk/config.toml"]
