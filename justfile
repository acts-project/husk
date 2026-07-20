# husk task runner — see README.md. Run `just` to list recipes.

# List available recipes.
default:
    @just --list

# ── golden image ────────────────────────────────────────────────────────────

# Rebuild a golden VM image locally (variant = base | gpu). Offline via
# libguestfs — no KVM/GPU needed. Extra flags pass through to build.sh, e.g.
#   just rebuild gpu --out /tmp/husk-gpu.qcow2 --runner-version 2.334.0
rebuild variant="base" *flags:
    images/build.sh --variant {{variant}} {{flags}}

# Rebuild both variants (base + gpu).
rebuild-all: (rebuild "base") (rebuild "gpu")

# Trigger the CI build+publish workflow, pinning a release tag (e.g. `just publish v3`).
# Publishes husk-base + husk-gpu to ghcr.io via ORAS.
publish version:
    gh workflow run build-images.yml -f version={{version}}

# ── docker ──────────────────────────────────────────────────────────────────

# Local tag for the huskd daemon image (the ghcr image is ghcr.io/acts-project/husk).
image := "husk:local"

# Build the huskd container image locally.
docker-build:
    docker build -t {{image}} .

# Mounts the config at /etc/husk/config.toml, forwards GH_TOKEN and any OS_*
# (OpenStack) vars from your shell, and mounts ~/.config/openstack so a
# `cloud = "..."` profile resolves inside the container. Ctrl-C stops it
# (SIGTERM → graceful shutdown). Examples:
#   just docker-run                     # uses ./config.toml
#   just docker-run config.libvirt.toml
# NB the config must bind `controller.http_addr = "0.0.0.0:9100"` to be reachable.
# Run huskd in Docker against a local config (rebuilds the image first).
docker-run config="config.toml": docker-build
    #!/usr/bin/env bash
    set -euo pipefail
    [ -f "{{config}}" ] || { echo "config not found: {{config}}" >&2; exit 1; }
    cfg="$(cd "$(dirname "{{config}}")" && pwd)/$(basename "{{config}}")"

    args=(--rm -it -p 9100:9100 -v "$cfg":/etc/husk/config.toml:ro)

    # GitHub PAT (github.pat_env, default GH_TOKEN) — forwarded from the environment
    # `just` runs in. Passed by NAME (-e GH_TOKEN) so the value comes from this
    # process's env, never the docker argv / ps output. Export it before running.
    if [ -n "${GH_TOKEN:-}" ]; then args+=(-e GH_TOKEN); else
        echo "warning: GH_TOKEN not set in environment — huskd will fail to auth" >&2
    fi
    # huskd log level passthrough.
    [ -n "${HUSK_LOG_LEVEL:-}" ] && args+=(-e HUSK_LOG_LEVEL)

    # OpenStack: forward every OS_* var, and mount clouds.yaml if present.
    while IFS='=' read -r name _; do
        [ -n "$name" ] && args+=(-e "$name")
    done < <(env | grep '^OS_' || true)
    [ -d "$HOME/.config/openstack" ] && args+=(-v "$HOME/.config/openstack":/app/.config/openstack:ro)

    docker run "${args[@]}" {{image}}

# ── dev ─────────────────────────────────────────────────────────────────────

# Run the test suite (extra args pass through to pytest).
test *args:
    uv run pytest {{args}}

# Lint with ruff.
lint:
    uv run ruff check .

# Format with ruff.
fmt:
    uv run ruff format .
