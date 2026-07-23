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

# Refuse to run a LOCAL recipe against a remote cluster. `oc login` writes into the
# same kubeconfig kubectl reads, so a CERN login silently redirects every plain
# `kubectl` in this file — and the namespace is called `husk` in both places, so
# nothing about the command looks wrong. Without this, `just k8s-nuke` deletes the
# CERN namespace rather than the laptop one.
#
# Compares the API SERVER, not the context name: `oc project` mints extra contexts
# for the same cluster (e.g. husk/127-0-0-1:PORT/system:admin alongside
# colima-husk), so a name check refuses on the local cluster under a different
# name — and would keep refusing until you noticed why.
_local-only:
    #!/usr/bin/env bash
    want="$(kubectl config view -o jsonpath='{.clusters[?(@.name=="colima-{{k8s_profile}}")].cluster.server}' 2>/dev/null)"
    have="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null)"
    ctx="$(kubectl config current-context 2>/dev/null || echo none)"
    if [ -z "$want" ]; then
        echo "REFUSING: no colima-{{k8s_profile}} cluster in kubeconfig." >&2
        echo "Start it with: just k8s-start" >&2
        exit 1
    fi
    if [ "$have" != "$want" ]; then
        echo "REFUSING: this recipe is local-only, but kubectl points elsewhere." >&2
        echo "  context: $ctx" >&2
        echo "  server:  ${have:-<none>}" >&2
        echo "  want:    $want" >&2
        echo "Switch with: kubectl config use-context colima-{{k8s_profile}}" >&2
        exit 1
    fi
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
    # This one serves BOTH environments, so it can't take the _local-only guard —
    # but writing credentials into the wrong cluster is still worth preventing.
    # env=local must be the colima context; anything else just announces itself
    # loudly, since the namespace is `husk` in both places and looks identical.
    ctx="$(kubectl config current-context 2>/dev/null || echo none)"
    # Server comparison, not context name — see _local-only.
    want="$(kubectl config view -o jsonpath='{.clusters[?(@.name=="colima-{{k8s_profile}}")].cluster.server}' 2>/dev/null)"
    have="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null)"
    if [ "{{env}}" = "local" ] && [ "$have" != "$want" ]; then
        echo "REFUSING: env=local but kubectl points at a non-local cluster." >&2
        echo "  context: $ctx" >&2
        echo "  server:  ${have:-<none>}" >&2
        echo "Switch with: kubectl config use-context colima-{{k8s_profile}}" >&2
        echo "(or pass env=cern if you really mean the remote cluster)" >&2
        exit 1
    fi
    echo "target cluster: $ctx"
    echo "  server:    ${have:-<none>}"
    echo "  namespace: {{k8s_namespace}}"
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
    # Create the namespace only if it is genuinely missing. Applying an existing
    # one needs update rights on the Namespace object, which a plain OpenShift
    # project member does not have — so an unconditional apply fails at CERN, where
    # the project already exists and was made for us.
    if ! kubectl get namespace {{k8s_namespace}} >/dev/null 2>&1; then
        kubectl create namespace {{k8s_namespace}}
    fi
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
k8s-local: _local-only (k8s-validate "local") k8s-build
    #!/usr/bin/env bash
    set -euo pipefail
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

# kustomize's configMapGenerator mints a NEW hash-suffixed ConfigMap every time
# config.toml changes, and `apply -k` never removes the old one — nothing in
# Kubernetes expires them, so they accumulate forever. Harmless in size, but they
# make `get configmap` ambiguous about which config is actually running.
#
# Safe by construction: it keeps every ConfigMap referenced by the Deployment's
# desired spec AND by any live pod. The second part matters mid-rollout, where an
# old pod still mounts the previous ConfigMap and would fail to restart without it.
# Delete huskd ConfigMaps no longer referenced (env-agnostic; prints what it does).
k8s-configmap-prune:
    #!/usr/bin/env bash
    set -euo pipefail
    ns="{{k8s_namespace}}"
    echo "context: $(kubectl config current-context)"
    keep="$(kubectl get deploy huskd -n "$ns" -o jsonpath='{.spec.template.spec.volumes[?(@.name=="config")].configMap.name}' 2>/dev/null || true)"
    keep="$keep $(kubectl get pods -n "$ns" -l app=huskd -o jsonpath='{range .items[*]}{range .spec.volumes[?(@.name=="config")]}{.configMap.name}{" "}{end}{end}' 2>/dev/null || true)"
    echo "in use: $keep"
    # Selected by NAME PREFIX, not label: ConfigMaps generated before the overlays
    # started labelling them carry no labels at all, and a label selector would
    # skip exactly the stale ones worth removing. The generator always names them
    # huskd-config-<hash>, so the prefix is the reliable handle.
    n=0
    for cm in $(kubectl get configmap -n "$ns" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}' 2>/dev/null | tr ' ' '\n' | grep '^huskd-config-'); do
        case " $keep " in *" $cm "*) continue ;; esac
        kubectl delete configmap "$cm" -n "$ns"
        n=$((n+1))
    done
    echo "pruned $n unreferenced ConfigMap(s)"

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
k8s-local-down: _local-only
    kubectl delete deployment,service,configmap -n {{k8s_namespace}} -l app.kubernetes.io/name=huskd --ignore-not-found

# Deletes the namespace and everything in it, INCLUDING the Secrets — you will
# need `just k8s-secrets` again afterwards.
# Delete the whole husk namespace, Secrets included.
k8s-nuke: _local-only
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

# The in-cluster half of config validation, run BEFORE anything is applied — the
# whole point is that the live pod is still serving while this runs, and a failure
# costs nothing but the exit code.
#
# `just k8s-validate cern` already parses the same file on your laptop. This adds
# the fidelity that check cannot have: the exact amd64 image CI built for THIS
# commit, the config as a mounted ConfigMap, and the App key from the real secret.
# So it catches the mismatch class the local check is blind to — a config using a
# key the deployed build does not know yet, or vice versa.
#
# Notes on the mechanics:
#  * The ConfigMap is temporary and named to stay clear of the hashed
#    huskd-config-<hash> the kustomize generator produces; the trap removes it even
#    when validate fails. Its content is the same file the generator reads, so what
#    is validated here is byte-identical to what the deploy will apply.
#  * --overrides REPLACES spec.containers wholesale (it is a JSON merge patch, and
#    a merge patch cannot merge into a list), so the container is spelled out in
#    full there. --image/--command are still required by `oc run` itself.
#  * `oc run --attach` exits with the container's own exit code, so a validation
#    failure aborts the recipe — and with it the deploy — before `oc apply`.
# Validate the config in-cluster against this commit's image, changing nothing.
k8s-live-preflight:
    #!/usr/bin/env bash
    set -euo pipefail
    cfg="k8s/overlays/cern/config.toml"
    [ -f "$cfg" ] || { echo "$cfg not found — run: just k8s-init" >&2; exit 1; }
    ns="{{k8s_namespace}}"
    cm="huskd-config-preflight"
    pod="huskd-preflight"

    cleanup() { oc delete configmap "$cm" -n "$ns" --ignore-not-found >/dev/null 2>&1 || true; }
    trap cleanup EXIT

    # A leftover pod from an interrupted run would make `oc run` fail on a name clash.
    oc delete pod "$pod" -n "$ns" --ignore-not-found >/dev/null 2>&1 || true
    oc create configmap "$cm" --from-file=config.toml="$cfg" -n "$ns" \
        --dry-run=client -o yaml | oc apply -n "$ns" -f - >/dev/null

    echo "preflight: validating $cfg against {{oc_image}}:{{oc_sha}} in $ns..."
    overrides=$(cat <<JSON
    {"spec": {
      "restartPolicy": "Never",
      "containers": [{
        "name": "$pod",
        "image": "{{oc_image}}:{{oc_sha}}",
        "command": ["huskctl", "validate", "--config", "/etc/husk/config.toml"],
        "env": [{"name": "HUSK_GITHUB__PRIVATE_KEY",
                 "valueFrom": {"secretKeyRef": {"name": "huskd-github", "key": "private-key.pem"}}}],
        "volumeMounts": [{"name": "config", "mountPath": "/etc/husk", "readOnly": true}],
        "securityContext": {"runAsNonRoot": true, "allowPrivilegeEscalation": false,
                            "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                      "limits": {"cpu": "500m", "memory": "256Mi"}}
      }],
      "volumes": [{"name": "config", "configMap": {"name": "$cm"}}]
    }}
    JSON
    )
    oc run "$pod" -n "$ns" --image="{{oc_image}}:{{oc_sha}}" --restart=Never \
        --attach --rm --quiet --pod-running-timeout=5m --overrides="$overrides" \
        --command -- huskctl validate --config /etc/husk/config.toml

# Apply the cern overlay WITHOUT changing the image (manifest-only changes).
# Validated first — this recipe is what writes the ConfigMap, so it is the last
# point at which a bad config costs nothing. (just runs a dependency once per
# invocation, so going through k8s-live-deploy does not re-validate.)
k8s-live-apply: (k8s-validate "cern")
    oc apply -k k8s/overlays/cern -n {{k8s_namespace}}

# Show what applying the cern overlay would change, against the live cluster.
k8s-live-diff:
    oc diff -k k8s/overlays/cern -n {{k8s_namespace}} || true

# Needs the CERN VPN. Pins the CI-built image for the current commit, so it fails
# fast if that image was never built rather than rolling out something stale.
#
# The config is validated HERE, before anything is applied. The validate-config
# initContainer still runs in-cluster (it is what guards a ConfigMap edited by
# hand, or applied from another machine), but by then the ConfigMap is already
# live and the old pod is already gone — Recreate, so a bad config means huskd is
# DOWN until you fix and re-apply. Catching it locally keeps that from ever
# starting.
# BOTH containers are pinned, never just huskd. The validate-config initContainer
# reads the same config with the same models, so an older build of it rejects a
# setting the daemon's build understands — and since the base leaves it on
# :latest with imagePullPolicy IfNotPresent, "older" means "whatever copy of
# :latest this node last cached", which can be months stale and differs per node.
# That fails as the initContainer crash-looping on a config that is actually
# valid, and it reproduces only on the nodes with a stale copy.
# Deploy: validate locally, check the image, validate in-cluster, apply, pin, wait.
k8s-live-deploy: (k8s-validate "cern") k8s-live-check k8s-live-preflight k8s-live-apply
    oc set image deployment/huskd huskd={{oc_image}}:{{oc_sha}} validate-config={{oc_image}}:{{oc_sha}} -n {{k8s_namespace}}
    oc rollout status deployment/huskd -n {{k8s_namespace}} --timeout=10m

# Roll back the live deployment one revision.
k8s-live-rollback:
    oc rollout undo deployment/huskd -n {{k8s_namespace}}

# ESCAPE HATCH for iterating on the live cluster without waiting for CI: build the
# image here and push it over this commit's CI tag. It PUSHES ONLY — deploying is
# still `just k8s-live-deploy`, which validates the config and applies the overlay.
#
# That split is the whole point. The recipe this replaced also rolled the
# deployment, which meant it shipped new CODE against whatever ConfigMap was
# already live: it never ran `oc apply -k`. A change that moves a config key and
# the model that reads it in the same commit then lands as an initContainer
# rejecting a key the daemon understands, and the pod sits in Init:CrashLoopBackOff
# — with no logs, because `kubectl logs --all-containers` skips init containers.
# Pushing and deploying are separate concerns; only one of them needs the config.
#
# Two things it does deliberately:
#
#  * --platform linux/amd64 ONLY. CERN is amd64; the local colima cluster runs a
#    separately built native husk:local, so nothing needs a multi-arch manifest and
#    building one would just double an already-emulated build.
#  * Warns when the tree is dirty, because the tag then claims to be a commit whose
#    content it does not contain.
#
# It prints the pushed DIGEST, and you should usually deploy with it rather than
# the tag: both containers are imagePullPolicy IfNotPresent, so a node that already
# cached this tag (from CI's build of the same commit, or an earlier push) can
# silently reuse those layers and "deploy" the old image. A digest the node has
# never seen always pulls. `k8s-live-deploy` sets the tag, which is right for a CI
# image — after a local push, re-pin by digest with the command this prints.
#
# The tag it overwrites no longer matches what CI built from that commit, so the
# provenance k8s-live-deploy relies on is broken until the next CI build. Use it to
# iterate; land the change and let CI rebuild before anything you intend to keep.
#
# The first amd64 build is SLOW: libvirt-python compiles from source under QEMU.
# Later builds reuse the local buildx cache and only redo the project layer.
# Build amd64 here and push over this commit's CI tag. Deploys nothing.
k8s-live-push:
    #!/usr/bin/env bash
    set -euo pipefail
    tag="{{oc_image}}:{{oc_sha}}"
    meta="$(mktemp -t husk-build)"
    trap 'rm -f "$meta"' EXIT

    echo "image: $tag  (overwrites the CI-built tag)"
    if [ -n "$(git status --porcelain)" ]; then
        echo "WARNING: working tree is dirty — this image is NOT {{oc_sha}}'s content." >&2
    fi

    docker buildx build --platform linux/amd64 -t "$tag" --push --metadata-file "$meta" .

    digest="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$meta")"
    echo
    echo "pushed digest: $digest"
    echo
    echo "deploy it with (validates the config, applies the overlay, pins the tag):"
    echo "    just k8s-live-deploy"
    echo "then re-pin by digest, so IfNotPresent cannot serve a cached copy of this tag:"
    echo "    oc set image deployment/huskd huskd={{oc_image}}@$digest validate-config={{oc_image}}@$digest -n {{k8s_namespace}}"
    echo "    oc rollout status deployment/huskd -n {{k8s_namespace}} --timeout=10m"

# Both run `huskctl reap` inside the pod, which already has the App key and config
# mounted — no credentials on your laptop.
#
# reap is scoped to the pools' own vm_prefix, so it cannot touch runners husk did
# not create. Worth knowing WHY that scoping is not optional: the underlying
# listing is the target's entire runner set, and the runner GROUP does not narrow
# it (`runner_group` applies only when registering — generate_jitconfig sets
# runner_group_id; the read path ignores it). `huskctl reap --all` opts out of the
# scope and is deliberately not exposed here.
#
# For routine cleanup prefer the daemon's own reaper (controller.reap_runners),
# which additionally knows the live slot set and so can tell a mid-boot slot from
# a dead one. This CLI cannot: it sees GitHub only, so a slot whose runner has yet
# to connect may lose its registration and be rebuilt.
# Show which runner registrations reap WOULD delete, deleting nothing.
k8s-reap-dry:
    @echo "context: $(kubectl config current-context)"
    kubectl exec -n {{k8s_namespace}} deployment/huskd -- huskctl reap --config /etc/husk/config.toml --dry-run

# Gated because it deletes GitHub state: run k8s-reap-dry first and read the list.
# Delete husk's dead runner registrations (requires: just k8s-reap yes).
k8s-reap confirm="":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{confirm}}" != "yes" ]; then
        echo "refusing to reap without confirmation." >&2
        echo >&2
        echo "This deletes GitHub runner registrations. It is scoped to husk's own" >&2
        echo "vm_prefix, so other people's runners are safe — but a slot that is" >&2
        echo "mid-boot reads 'offline' until its runner connects, and this CLI has no" >&2
        echo "view of the backend, so it can delete a registration a slot still needs." >&2
        echo "That slot then never registers and is rebuilt after its grace period." >&2
        echo >&2
        echo "See exactly what would go:  just k8s-reap-dry" >&2
        echo "context: $(kubectl config current-context)" >&2
        echo "Then, to proceed:           just k8s-reap yes" >&2
        exit 1
    fi
    echo "context: $(kubectl config current-context)"
    kubectl exec -n {{k8s_namespace}} deployment/huskd -- huskctl reap --config /etc/husk/config.toml

# Runs `huskctl recycle` inside the pod, which already has the App key, config and
# backend creds mounted — no credentials on your laptop. A recycle stops slots so
# huskd rebuilds them on its next tick with freshly rendered cloud-init: the way to
# roll a new image or firewall onto already-running slots. huskd must be running
# for the rebuild to follow. Idle/ACTIVE slots only unless --force; --dry-run
# changes nothing. Everything after the recipe name passes straight to huskctl:
#   just k8s-recycle --all --dry-run              # whole fleet, show only
#   just k8s-recycle --all --pool cern            # every idle slot in one pool
#   just k8s-recycle husk-cern-3 --pool cern      # one slot
#   just k8s-recycle husk-cern-3 --pool cern -f   # also if it is busy (kills its job)
# Recycle slots on the cluster (args → huskctl recycle; try --dry-run first).
k8s-recycle *args:
    @echo "context: $(kubectl config current-context)"
    kubectl exec -n {{k8s_namespace}} deployment/huskd -- huskctl recycle --config /etc/husk/config.toml {{args}}

# huskd evicts unpinned goldens itself — see the sizing note in k8s/overlays/cern/pvc.yaml.
#
# `df` is NOT redundant with du. GC bounds what husk puts on the volume; it says
# nothing about what else is on it, and df is the only view of that. It also tells
# you whether huskd's husk_filesystem_* metrics mean anything here: those come from
# statvfs, and on CephFS statvfs reports the subvolume QUOTA only if ceph-csi set
# one and client_quota_df is on. If Size shows ~50G the headroom alert works; if it
# shows the whole Ceph cluster's capacity it does not — fall back to
# kubelet_volume_stats_* (authoritative for PVCs).
# Show golden-image cache usage (du) and the PVC's real capacity/headroom (df).
k8s-live-cache:
    oc exec -n {{k8s_namespace}} deployment/huskd -- du -sh /app/.cache/husk/images/
    oc exec -n {{k8s_namespace}} deployment/huskd -- df -h /app/.cache/husk/images/

# Confirms the metrics PVC is actually being written — the thing that is easy to
# get wrong (a read-only mount) fails SILENTLY by design: huskd logs a warning and
# carries on rather than dying over a bookkeeping file. So check the mtime moves.
# Show huskd's persisted metrics state (size + when it was last flushed).
k8s-live-metrics-state:
    oc exec -n {{k8s_namespace}} deployment/huskd -- ls -l --time-style=full-iso /var/lib/husk/metrics.json

# ── openstack ───────────────────────────────────────────────────────────────

# An application credential is permanently bound to the project it was created
# under, and that binding lives in Keystone — so an app-cred profile has no
# project field and clouds.yaml CANNOT tell you where it lands. Two differently
# named profiles may be one project. Since pools sharing a name are kept apart
# only by the project, this is the check that says whether the boundary is real.
# Prefers secrets/clouds.yaml (what the pod gets) over ~/.config/openstack.
# Show which OpenStack project each clouds.yaml profile authenticates into.
openstack-whoami *profiles:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f secrets/clouds.yaml ]; then
        export OS_CLIENT_CONFIG_FILE="$PWD/secrets/clouds.yaml"
        echo "using $OS_CLIENT_CONFIG_FILE"
    fi
    uv run python scripts/openstack-whoami.py {{profiles}}

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
