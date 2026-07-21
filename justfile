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

# ── kubernetes ──────────────────────────────────────────────────────────────

k8s_namespace := "husk"
k8s_profile   := "husk"                        # colima profile for the local cluster
# Must match what .github/workflows/build-app-image.yml publishes: it pushes to
# ghcr.io/${{ github.repository }} (the REPO name, husk — not huskd, and distinct
# from the husk-base/husk-gpu golden VM images), tagged by `type=sha`, whose
# default format is sha-<7 chars>. --short=7 pins that width so the tag we look
# for is the tag CI wrote.
oc_image      := "ghcr.io/acts-project/husk"
oc_sha        := "sha-" + `git rev-parse --short=7 HEAD`

# NB `just --list` shows only the LAST comment line of a block as the summary,
# so each recipe below keeps its one-line description last.

# Uses a profile separate from `default` so your existing docker setup is
# untouched — colima can't add k8s to a running profile in place. k3s here runs
# on cri-dockerd, which is why the local overlay can use a locally-built image
# with imagePullPolicy: Never (no registry push needed).
# Start a colima profile with kubernetes (k3s) and switch kubectl to it.
k8s-start:
    colima start {{k8s_profile}} --kubernetes --cpu 4 --memory 6 --disk 60
    kubectl config use-context colima-{{k8s_profile}}

# Stop the local cluster (keeps the VM's disk; `colima delete {{k8s_profile}}` to wipe).
k8s-stop:
    colima stop {{k8s_profile}}

# Copies each overlay's config.example.toml to config.toml (never overwriting an
# existing one) and creates the gitignored secrets/ directory. The real config.toml
# and everything in secrets/ stay local — see k8s/README.md.
# Set up the local, gitignored config + secrets files. Safe to re-run.
k8s-init:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p secrets
    for env in local cern; do
        src="k8s/overlays/$env/config.example.toml"
        dst="k8s/overlays/$env/config.toml"
        if [ -e "$dst" ]; then
            echo "keep   $dst (already exists)"
        else
            cp "$src" "$dst"
            echo "create $dst"
        fi
    done
    echo
    echo "Next: edit those, then drop your credentials into secrets/ :"
    echo "  secrets/private-key.pem   GitHub App private key"
    echo "  secrets/clouds.yaml       openstacksdk profile (or: just k8s-secrets clouds=~/.config/openstack/clouds.yaml)"
    echo "Then: just k8s-secrets"

# Reads from the gitignored secrets/ dir. Re-run to rotate: the create|apply pipe
# below updates in place rather than erroring on a re-run.
#
# `env` picks the clouds.yaml: secrets/clouds.<env>.yaml if it exists, else the
# shared secrets/clouds.yaml. That's how local and live can authenticate into
# DIFFERENT OpenStack projects while the manifests stay identical — the profile
# name in config.toml (`cloud = "cern"`) stays the same, only the credential
# behind it changes. Override either file explicitly with pem=/clouds=.
# Load the GitHub App key + clouds.yaml into the cluster as Secrets (env = local | cern).
k8s-secrets env="local" pem="secrets/private-key.pem" clouds="":
    #!/usr/bin/env bash
    set -euo pipefail
    pem="$(eval echo {{pem}})"
    clouds="$(eval echo '{{clouds}}')"
    if [ -z "$clouds" ]; then
        if [ -f "secrets/clouds.{{env}}.yaml" ]; then
            clouds="secrets/clouds.{{env}}.yaml"
        else
            clouds="secrets/clouds.yaml"
        fi
    fi
    echo "using clouds file: $clouds"
    if [ ! -f "$pem" ]; then
        echo "App private key not found: $pem" >&2
        echo "Put it there (\`just k8s-init\` creates secrets/), or pass one:" >&2
        echo "  just k8s-secrets pem=/path/to/key.pem" >&2
        exit 1
    fi
    # A PEM is useless to huskd if it's actually the public key or a stray file;
    # cheaper to catch here than as a JWT signing failure in the pod. (config.py
    # applies the same "PRIVATE KEY" check at load time.)
    grep -q "PRIVATE KEY" "$pem" || { echo "$pem does not look like a private key" >&2; exit 1; }
    if [ ! -f "$clouds" ]; then
        echo "clouds.yaml not found: $clouds" >&2
        echo "Copy yours in, or pass one explicitly:" >&2
        echo "  just k8s-secrets {{env}} clouds=~/.config/openstack/clouds.yaml" >&2
        exit 1
    fi
    # The profile named in config.toml must exist in this file, or huskd fails at
    # first reconcile inside the pod rather than here. Cheap to check now.
    profile="$(awk '/^[[:space:]]*cloud[[:space:]]*=/ {gsub(/[\"'"'"']/,"",$3); print $3; exit}' "k8s/overlays/{{env}}/config.toml" 2>/dev/null)"
    if [ -n "$profile" ] && ! grep -qE "^[[:space:]]+${profile}:" "$clouds"; then
        echo "warning: k8s/overlays/{{env}}/config.toml wants cloud = \"$profile\"," >&2
        echo "         but $clouds has no such profile. huskd will fail to connect." >&2
    fi
    kubectl create namespace {{k8s_namespace}} --dry-run=client -o yaml | kubectl apply -f -
    # --dry-run|apply so re-running rotates the contents instead of erroring.
    # NB "$pem"/"$clouds", not {{pem}}/{{clouds}} — the shell vars are the ~-expanded
    # ones. The Secret KEYS (private-key.pem, clouds.yaml) are what the Deployment
    # references, so they're fixed regardless of the source filename.
    kubectl create secret generic huskd-github \
        --from-file=private-key.pem="$pem" \
        -n {{k8s_namespace}} --dry-run=client -o yaml | kubectl apply -f -
    kubectl create secret generic huskd-openstack \
        --from-file=clouds.yaml="$clouds" \
        -n {{k8s_namespace}} --dry-run=client -o yaml | kubectl apply -f -
    echo "secrets huskd-github + huskd-openstack are in namespace {{k8s_namespace}}"

# Good first check before any apply: `just k8s-render cern | less`.
# Render an overlay to stdout without applying (env = local | cern).
k8s-render env="local":
    kubectl kustomize k8s/overlays/{{env}}

# Runs `huskctl validate`, which parses the TOML and checks it WITHOUT touching
# OpenStack, libvirt or GitHub — so it is instant and safe to run anywhere. This
# is the same check the deployment's validate-config initContainer runs, so a
# config that passes here will not fail the rollout for parse reasons.
#
# Uses secrets/private-key.pem when present, so the key's readability is checked
# too; otherwise it substitutes a placeholder and validates structure only (the
# loader insists on *a* key, but validate never uses it for anything).
# Validate an overlay's config.toml locally, without a cluster (env = local | cern).
k8s-validate env="local":
    #!/usr/bin/env bash
    set -euo pipefail
    cfg="k8s/overlays/{{env}}/config.toml"
    [ -f "$cfg" ] || { echo "$cfg not found — run: just k8s-init" >&2; exit 1; }
    if [ -f secrets/private-key.pem ]; then
        export HUSK_GITHUB__PRIVATE_KEY="$(cat secrets/private-key.pem)"
    else
        echo "note: secrets/private-key.pem absent — validating structure only" >&2
        # One line on purpose: a continuation at column 0 ends the recipe body.
        export HUSK_GITHUB__PRIVATE_KEY="$(printf -- '-----BEGIN PRIVATE KEY-----\nplaceholder\n-----END PRIVATE KEY-----\n')"
    fi
    uv run huskctl validate --config "$cfg"

# Validate both overlays (what CI would check).
k8s-validate-all: (k8s-validate "local") (k8s-validate "cern")

# Explicitly targets the `husk` profile's docker daemon rather than whatever
# `docker context show` happens to point at. This matters: each colima profile has
# its OWN daemon, so building against the `default` profile puts the image in a VM
# the husk k3s node can't see — and because the local overlay sets
# imagePullPolicy: Never, that fails at RUN time as ErrImageNeverPull rather than
# at build time. Built for the host arch (arm64 here); the live amd64 image is
# built by CI (.github/workflows/build-huskd.yml), never on a laptop.
# Build the huskd image into the husk profile's docker daemon.
k8s-build:
    docker --context colima-{{k8s_profile}} build -t {{image}} .

# The image tag is unchanged between builds, so this forces a rollout restart —
# otherwise the Deployment spec is identical and k8s keeps the old pod running.
# Build the huskd image locally and deploy it to the colima cluster.
k8s-local: k8s-build
    #!/usr/bin/env bash
    set -euo pipefail
    # Guard against deploying into the wrong cluster (e.g. still pointed at CERN
    # from an earlier `oc login`). Cheap check, expensive mistake.
    ctx="$(kubectl config current-context)"
    if [ "$ctx" != "colima-{{k8s_profile}}" ]; then
        echo "kubectl context is '$ctx', expected 'colima-{{k8s_profile}}'." >&2
        echo "Run: kubectl config use-context colima-{{k8s_profile}}" >&2
        exit 1
    fi
    kubectl apply -k k8s/overlays/local
    kubectl rollout restart deployment/huskd -n {{k8s_namespace}}
    just k8s-wait

# `kubectl rollout status` only ever prints "waiting for rollout to finish" — it
# cannot distinguish "still pulling a 2 GB golden" from "this pod will NEVER
# start", so a config error looks identical to slow progress until the timeout
# expires. This polls the pod's real state instead and bails the moment it hits a
# reason that will never resolve on its own, printing the message, the events and
# the logs that explain it.
# Wait for the huskd rollout, failing fast with diagnostics on a stuck pod.
k8s-wait timeout="600":
    #!/usr/bin/env bash
    set -uo pipefail
    ns="{{k8s_namespace}}"
    deadline=$(( $(date +%s) + {{timeout}} ))

    # Waiting reasons that never fix themselves: the kubelet has already decided
    # it cannot run this container. (ContainerCreating / PodInitializing are
    # NOT here — those are normal progress.)
    terminal="CreateContainerConfigError|CreateContainerError|InvalidImageName|ErrImageNeverPull|ImagePullBackOff|ErrImagePull|CrashLoopBackOff|RunContainerError"

    diagnose() {
        echo
        echo "──────── pods ────────"
        kubectl get pods -n "$ns" -l app=huskd -o wide
        echo
        echo "──────── why ────────"
        # NB one long line on purpose: a continuation starting at column 0 would
        # terminate the recipe body, and just would parse the rest as justfile syntax.
        kubectl get pods -n "$ns" -l app=huskd -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{range .status.containerStatuses[*]}  waiting: {.state.waiting.reason}{"\n"}  message: {.state.waiting.message}{"\n"}  restarts: {.restartCount}{"\n"}{end}{end}'
        echo
        echo "──────── recent events ────────"
        kubectl get events -n "$ns" --sort-by=.lastTimestamp 2>/dev/null | tail -12
        echo
        echo "──────── logs (if the container ever started) ────────"
        kubectl logs -n "$ns" deployment/huskd --tail=40 --all-containers 2>&1 | tail -40
    }

    # Stream huskd's own logs as soon as its container is running, so the wait
    # shows real progress ("pulling golden: 43%") instead of sitting mute. Killed
    # on exit so the recipe doesn't leave a follower behind.
    logs_pid=""
    cleanup() { [ -n "$logs_pid" ] && kill "$logs_pid" 2>/dev/null; return 0; }
    trap cleanup EXIT

    last=""
    lastwarn=""
    while :; do
        # Echo the pod line whenever it CHANGES — Pending -> Init:0/1 -> Running
        # -> 1/1 — so every transition is visible without spamming a line per poll.
        now="$(kubectl get pods -n "$ns" -l app=huskd --no-headers 2>/dev/null | awk '{print $1, $2, $3, "restarts="$4}')"
        if [ -n "$now" ] && [ "$now" != "$last" ]; then
            printf '  %s\n' "$now"
            last="$now"
        fi

        # Surface Warning events as they appear. This is what turns an inscrutable
        # 10-minute ContainerCreating into "secret huskd-openstack not found" —
        # a stuck mount is not a waiting *reason*, so it is invisible otherwise.
        warn="$(kubectl get events -n "$ns" --field-selector type=Warning --sort-by=.lastTimestamp -o jsonpath='{.items[-1:].message}' 2>/dev/null)"
        if [ -n "$warn" ] && [ "$warn" != "$lastwarn" ]; then
            printf '  ! %s\n' "$warn"
            lastwarn="$warn"
        fi

        ready="$(kubectl get deploy huskd -n "$ns" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"
        if [ "${ready:-0}" -ge 1 ] 2>/dev/null; then
            echo "huskd is ready."
            exit 0
        fi

        # Once the main container is up, follow its logs for the rest of the wait.
        # (Before that there is nothing to read — no process has started.)
        if [ -z "$logs_pid" ]; then
            running="$(kubectl get pods -n "$ns" -l app=huskd -o jsonpath='{.items[*].status.containerStatuses[?(@.name=="huskd")].state.running.startedAt}' 2>/dev/null)"
            if [ -n "$running" ]; then
                echo "  ──── huskd logs ────"
                kubectl logs -n "$ns" -l app=huskd -c huskd -f --tail=20 2>/dev/null | sed 's/^/  | /' &
                logs_pid=$!
            fi
        fi

        # Any container stuck in a reason that won't resolve -> stop waiting.
        if kubectl get pods -n "$ns" -l app=huskd \
             -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}{end}' 2>/dev/null \
           | grep -qE "^($terminal)$"; then
            echo "huskd is stuck and will not start on its own:" >&2
            diagnose
            exit 1
        fi

        # A failing initContainer is NOT a waiting reason — it terminates non-zero
        # and the pod reads Init:Error / Init:CrashLoopBackOff in the STATUS column.
        # That's the validate-config container rejecting the ConfigMap.
        if printf '%s' "$now" | grep -qE "Init:(Error|CrashLoopBackOff)"; then
            echo "the validate-config initContainer rejected the config:" >&2
            kubectl logs -n "$ns" -l app=huskd -c validate-config --tail=30 2>&1 | sed 's/^/  /' >&2
            diagnose
            exit 1
        fi

        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "timed out after {{timeout}}s waiting for huskd to become ready." >&2
            echo "NB the first start pulls a ~2 GB golden image, which can legitimately" >&2
            echo "take a while; re-run with a longer budget: just k8s-wait 1800" >&2
            diagnose
            exit 1
        fi
        sleep 2
    done

# Confirms the image is in the daemon k3s actually reads, which is the failure
# `ErrImageNeverPull` is telling you about.
# Verify the locally-built image is visible to the husk cluster's docker daemon.
k8s-verify-image:
    docker --context colima-{{k8s_profile}} images {{image}}

# Selects by LABEL, not `deployment/huskd`: the deployment form resolves to one
# pod and errors out if that pod isn't running yet, whereas this waits and picks
# up whichever pod is current across a rollout. --pod-running-timeout blocks
# until the container starts rather than failing immediately, so you can run this
# straight after an apply and watch startup live.
#
# Logs exist as soon as the container RUNS — readiness is irrelevant, so huskd's
# minutes of golden-pulling stream fine. But a pod stuck in ContainerCreating /
# CreateContainerConfigError / ImagePullBackOff has no logs at all: no process
# ever started. Use `just k8s-status` (events) for that phase.
# Follow huskd's logs, waiting for the container to start if necessary.
k8s-logs *args:
    kubectl logs -n {{k8s_namespace}} -l app=huskd --all-containers --tail=200 -f --pod-running-timeout=10m {{args}}

# After a crash or a probe-kill, `kubectl logs` shows the NEW container — which
# usually says nothing useful. --previous retrieves the logs of the instance that
# actually died, which is where the reason is. First thing to reach for on a
# CrashLoopBackOff or an unexplained restart.
# Show logs from the PREVIOUS (crashed) container instance.
k8s-logs-prev:
    kubectl logs -n {{k8s_namespace}} -l app=huskd --all-containers --previous --tail=200

# The config-validation initContainer runs before huskd; if it rejects the config
# the pod sits in Init:Error and the main container never starts, so plain
# `k8s-logs` shows nothing. This reads that container specifically.
# Show the validate-config initContainer's output (why Init:Error).
k8s-logs-init:
    kubectl logs -n {{k8s_namespace}} -l app=huskd -c validate-config --tail=50

# Pod/deployment state, plus recent events (where image-pull and probe failures show up).
k8s-status:
    kubectl get pods,svc -n {{k8s_namespace}}
    kubectl get events -n {{k8s_namespace}} --sort-by=.lastTimestamp | tail -20

# Forward the dashboard to localhost:9100 (/status /metrics /healthz /events).
k8s-forward:
    @echo "dashboard  -> http://localhost:9100/"
    kubectl port-forward -n {{k8s_namespace}} svc/huskd 9100:9100

# Deletes the workload BY LABEL rather than `delete -k`, which would also delete
# the Namespace the overlay declares — and a namespace delete cascades to the
# Secrets inside it. That silently destroys huskd-github/huskd-openstack, and the
# next apply then hangs forever in ContainerCreating on
#   MountVolume.SetUp failed for volume "openstack": secret "huskd-openstack" not found
# Removing the workload while keeping the namespace and its Secrets is nearly
# always what's wanted; use k8s-nuke for the full teardown.
# Tear down the local deployment, keeping the namespace and Secrets.
k8s-local-down:
    kubectl delete deployment,service,configmap -n {{k8s_namespace}} -l app.kubernetes.io/name=huskd --ignore-not-found

# Deletes the namespace and everything in it, INCLUDING the Secrets — you will
# need `just k8s-secrets` again afterwards.
# Delete the whole husk namespace, Secrets included.
k8s-nuke:
    kubectl delete namespace {{k8s_namespace}} --ignore-not-found

# ── kubernetes: live (CERN OpenShift) ───────────────────────────────────────
# The CERN OpenShift API is not reachable from GitHub-hosted runners, so deploy is
# a MANUAL step over the CERN VPN — CI only builds and pushes the image
# (.github/workflows/build-huskd.yml). These recipes assume the VPN is up, plus
# `oc login` and an existing {{k8s_namespace}} project.
# Untested until the local run is proven.

# The image is built by CI, not here: `build-app-image.yml` pushes
# {{oc_image}}:sha-<short> for every main commit. Deploying by that tag
# means what reaches the cluster is exactly what CI built and tested — a laptop
# build could carry uncommitted changes, and would need an emulated amd64 build
# on Apple silicon anyway.
# Check that the image for the current commit exists on ghcr before deploying.
k8s-live-check:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "$(git status --porcelain)" ]; then
        echo "warning: working tree is dirty — HEAD ({{oc_sha}}) is not what you have locally" >&2
    fi
    if ! git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
        echo "warning: HEAD is not on origin/main — CI may not have built {{oc_sha}}" >&2
    fi
    echo "looking for {{oc_image}}:{{oc_sha}} on ghcr..."
    if docker manifest inspect {{oc_image}}:{{oc_sha}} >/dev/null 2>&1; then
        echo "OK: {{oc_image}}:{{oc_sha}} exists"
    else
        echo "NOT FOUND: {{oc_image}}:{{oc_sha}}" >&2
        echo "Has the build-app-image workflow finished for this commit? Check:" >&2
        echo "  gh run list --workflow build-app-image.yml -L5" >&2
        exit 1
    fi

# Apply the cern overlay WITHOUT changing the image (manifest-only changes).
k8s-live-apply:
    oc apply -k k8s/overlays/cern -n {{k8s_namespace}}

# Show what applying the cern overlay would change, against the live cluster.
k8s-live-diff:
    oc diff -k k8s/overlays/cern -n {{k8s_namespace}} || true

# Needs the CERN VPN. Pins the CI-built image for the current commit, so it fails
# fast if that image was never built rather than rolling out something stale.
# Deploy: verify the CI image exists, apply manifests, pin the SHA, wait for rollout.
k8s-live-deploy: k8s-live-check k8s-live-apply
    oc set image deployment/huskd huskd={{oc_image}}:{{oc_sha}} -n {{k8s_namespace}}
    oc rollout status deployment/huskd -n {{k8s_namespace}} --timeout=10m

# Roll back the live deployment one revision.
k8s-live-rollback:
    oc rollout undo deployment/huskd -n {{k8s_namespace}}

# There is no automatic eviction — see the sizing note in k8s/overlays/cern/pvc.yaml.
# Show how much of the golden-image cache PVC is in use.
k8s-live-cache:
    oc exec -n {{k8s_namespace}} deployment/huskd -- du -sh /app/.cache/husk/images/

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
