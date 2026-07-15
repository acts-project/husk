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
