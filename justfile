set shell := ["bash", "-eu", "-o", "pipefail", "-c"]
set positional-arguments

# List available commands.
default:
    @just --list

# Deploy a stack or service (all stacks by default), removing orphans.
deploy *args:
    @./docker/scripts/stacks deploy "$@"

# Validate a stack and show its status, or check the workstation.
check *args:
    @if [[ "${1:-}" == workstation ]]; then ./docker/scripts/stacks doctor; else ./docker/scripts/stacks check "$@"; fi

# Show container status without resolving secrets.
status *args:
    @./docker/scripts/stacks status "$@"

# Show logs for a stack or service; add -f to follow.
logs +args:
    @./docker/scripts/stacks logs "$@"

# Pull images and recreate a stack or service.
update +args:
    @case "$1" in pilot) python3 docker/scripts/pilot_update.py "${@:2}" ;; *) ./docker/scripts/stacks upgrade "$@" ;; esac

# Test the pilot deployment and rollback on Nexus.
test target:
    @case "$1" in pilot) python3 docker/scripts/verify_pilot.py ;; *) echo 'Usage: just test pilot' >&2; exit 2 ;; esac

# Sync service files to a stack; add -n after the stack to preview.
sync stack *args:
    @./docker/scripts/stacks -s "$1" sync "${@:2}"

# Run a Docker Compose command for one stack.
compose stack +args:
    @./docker/scripts/stacks -s "$1" compose "${@:2}"

# Set an Infisical field; use -p for a password or -e for an editor.
vault +args:
    @python3 tools/vault.py "$@"

# Set up the workstation or Docker contexts.
prepare target:
    @case "$1" in workstation) ./docker/scripts/stacks bootstrap ;; contexts) ./docker/scripts/stacks ctx ;; *) echo 'Usage: just prepare workstation|contexts' >&2; exit 2 ;; esac

# Open the hardware console, optionally for one host.
open +args:
    @node tools/launch.mjs "$@"

# Stop local console servers.
stop:
    @node tools/launch.mjs stop
