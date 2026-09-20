# tctl

`tctl` is the Technis Docker fleet console and command-line operator tool. It
replaces `scripts/docker/stack`. Rust, Ratatui, Tokio, and Bollard handle the UI,
concurrent host connections, and Docker API. Docker Compose still resolves and
applies the repository's includes, overlays, builds, and environment files.

## Prepare a clone

```sh
git clone https://github.com/jovalle/technis
cd technis
make
./bin/tctl
```

Run `make` after cloning and whenever you want the latest published binary. Git
has no automatic post-clone hook; preparation is an explicit required step.
The default Make target detects the OS and CPU, downloads the release archive,
verifies SHA-256, and replaces `bin/tctl` atomically. A failed download or checksum
leaves the previous binary untouched. No root access or Rust installation is
needed for a published binary. Linux archives use musl for static distribution.
Supported platforms are Linux and macOS on arm64 and x86_64.

The installer needs Bash, Make, curl, tar, and either `sha256sum` or `shasum`.
Before the first release exists, it builds the checkout using Rust. `make build`
always builds locally using `rust-toolchain.toml` and `Cargo.lock`. This is also
the installation path for private repositories. A source build needs network
access for the first Rust/toolchain/dependency download.

Use `TCTL_VERSION=tctl-COMMIT_SHA make` to install a specific published version.
`TCTL_REPOSITORY=owner/repository` selects a fork's releases. Add the checkout's
`bin` directory to PATH to use `tctl` directly. `--version` includes the release
commit, or `local` for a development build. The binary finds the repository from
the working directory or its own location; `--root PATH` overrides discovery.
The `docs` and `web` submodules are not required.

## Connect the hosts

`tctl.toml` maps stack names to explicit endpoints and expected daemon names.
The current Docker context and `DOCKER_HOST` are never used to choose a target.
A discovered stack without an entry defaults to `ssh://STACK` and daemon name
`STACK`. Additional discovered stack names must be valid resource names.

```toml
[hosts.nexus]
endpoint = "ssh://nexus"
expected_name = "nexus"
socket = "/var/run/docker.sock"
```

Configure SSH aliases, users, ports, jump hosts, and keys in `~/.ssh/config`.
Establish and verify host-key trust with SSH before opening the console. The
account needs access to the remote Docker Unix socket. Rootless Docker can use a
host-specific `socket` path. OpenSSH must permit Unix socket forwarding.

`tctl` opens private Unix-socket tunnels using OpenSSH and reads Docker through
Bollard. SSH agent authentication and SSH configuration continue to work.
Host-key verification stays enabled. No remote agent, exposed Docker TCP port,
local Docker daemon, or Docker CLI is needed for live inspection and existing
container start/stop/restart/removal. Each connection verifies the daemon name;
writes recheck it before executing.

For a local daemon, configure `endpoint = "unix:///absolute/path/docker.sock"`
and its exact Docker daemon name. SSH port-forward controls apply only to remote
SSH endpoints.

## Configure deployment

```sh
./bin/tctl init
```

This copies missing shared and stack `.env.example` files to `.env` with mode
`600`. Existing files are never overwritten. Fill the required settings before
deploying; empty examples are not working defaults. Set `STACK_DATA_ROOT` to the
actual remote data directory and provision the application's data, permissions,
configuration, devices, and external networks. Git does not contain databases,
certificates, or complete application state.

Install Docker CLI with a Compose plugin that supports `include`, env files, and
`up --wait`. Configuration validation and service/image listing run locally and
do not require a reachable daemon. `doctor` verifies daemon identity, Compose
configuration, and external networks. It does not provision or exhaustively
validate application bind mounts and device permissions.

Secrets may remain in ignored `.env` files or come from Vaultwarden. For automatic
per-host vault injection, configure item IDs in order:

```toml
[hosts.nexus]
endpoint = "ssh://nexus"
expected_name = "nexus"
vault_items = ["shared-item-uuid", "nexus-item-uuid"]
```

Install and authenticate the Bitwarden CLI, then unlock it in the launching shell:

```sh
bw config server https://vault.techn.is
bw login
export BW_SESSION="$(bw unlock --raw)"
./bin/tctl
```

Use hidden custom fields named after environment variables. Later items override
earlier items. Values are fetched per host and passed only to that host's Compose
children. They are never written to configuration or command arguments. Vault
fields cannot change process executables or Docker routing variables. Runtime
inspection and container lifecycle actions work while the vault is locked.
After unlocking or changing configuration, press `R` to retry desired-state
resolution. A session unlocked in a different shell does not update an already
running process; relaunch `tctl` with the new `BW_SESSION`.

The existing `scripts/secrets/run-with-vault` also works for individual CLI
commands. Do not inject one host's credentials globally for a fleet-wide command;
use `vault_items` for each host instead.

## Navigate

The console opens on Services across **all stacks**. Every resource row carries
its stack or host, and actions target that row. The header summarizes connections,
running containers, snapshot age, and configuration errors for the current scope.
Eight tabs cover stacks, services, containers, images, volumes, networks, ports,
and events. Images, volumes, and networks use a `HOST` column because they may be
shared or unmanaged; containers also show their Compose project when space allows.

Press `b` for a searchable stack picker. `:ctx nexus` selects one stack,
`:ctx nexus,stargate` selects a subset, and `:ctx all` restores the fleet.
`[` and `]` cycle through All and individual stacks. Scope persists across tabs.
Enter drills from stacks to services to containers to logs; Esc restores the
previous scope, filter, and selection.

Use `/` for fuzzy text and exact field filters, combined with spaces:
`stack:nexus state:unhealthy postgres`. `stack:nexus,stargate` matches either stack;
`host:` is an alias for `stack:`. `state:` also accepts connection labels such as
`offline` and `stale`. Press `o` to cycle state/name/stack sorting and `O` to reverse
it, or enter `:sort name`, `:sort stack`, or `:sort state desc`. Events appear in
newest-received order across hosts, with daemon timestamps in UTC; `O` reverses them.

Missing desired services remain visible when Compose configuration resolves.
When it does not, the services tab shows runtime services and the config error.
Unmanaged containers remain visible but do not receive Compose service actions.
Services show running/total containers, not desired replica counts, plus aggregate
CPU and memory when all running replicas have fresh samples. Connection state is
separate from workload state; a running container is not necessarily healthy.

Tables use one row per resource and hide secondary columns at narrow widths.
Press `p` to toggle a selected-row pane with endpoint, freshness, image, status,
and ports. The pane hides below 24 terminal rows. Logs and inspection use the full
body. No permanent fleet sidebar is needed.

| Keys                               | Behavior                                              |
| ---------------------------------- | ----------------------------------------------------- |
| `j` / `k`, arrows                  | Move selection or scroll details                      |
| `g` / `G`, Home / End              | First / last row; beginning / follow end of logs      |
| Ctrl-u / Ctrl-d, PageUp / PageDown | Move ten rows                                         |
| `h` / `l`, Tab / Shift-Tab         | Previous / next resource tab                          |
| `1` through `8`                    | Jump to a resource tab                                |
| `[` / `]`                          | Previous / next scope, including All                  |
| `b`, Ctrl-b                        | Open searchable stack scope picker                    |
| `p`                                | Toggle selected-row details                           |
| `o` / `O`                          | Cycle sort field / reverse order                      |
| `/`                                | Fuzzy text and field filters; log substring search    |
| `:`                                | Command mode                                          |
| Enter                              | Stack → services → containers → logs                  |
| `L` / `i` / `t`                    | Logs / diagnostic inspection / live container metrics |
| `e`                                | Shell in the selected managed service                 |
| `a`                                | Action menu                                           |
| `r` / `s` / `S`                    | Restart / stop / start                                |
| `D` / `U` / `x`                    | Deploy / update / remove service containers           |
| `R`                                | Refresh hosts and repository configuration            |
| `f`                                | Forward selected port; toggle follow in logs          |
| Esc                                | Close details or prompt, clear filter, or go back     |
| `?`                                | Keybinding help                                       |
| `q`, Ctrl-c                        | Quit when no mutation is running                      |

At narrow widths, secondary columns hide automatically. Text labels accompany colors.
The UI needs at least 45 columns and 12 rows; 120 columns is more comfortable.

Override bindings in the repository's `[keys]` table:

```toml
[keys]
"ctrl-b" = "scope"
"ctrl-r" = "refresh"
"J" = "page-down"
"K" = "page-up"
```

Available actions are `quit`, `up`, `down`, `first`, `last`, `page-up`, `page-down`,
`previous-tab`, `next-tab`, `previous-host`, `next-host`, `scope`, `preview`,
`sort`, `reverse-sort`, `filter`,
`command`, `help`, `open`, `back`, `logs`, `inspect`, `config`, `stats`, `shell`, `actions`,
`restart`, `start`, `stop`, `deploy`, `update`, `remove`, `refresh`, `forward`, and
`1` through `8`. Key names are case-sensitive. Restart the console after editing
bindings. The old `sidebar` action remains an alias for `scope`. Input prompts
use normal text editing rather than command bindings.

Commands include `:ctx all`, `:ctx nexus,stargate`, `:sort state`, tab names such
as `:services`, action names, `:config`, `:scale 2`, `:refresh`, `:cancel`, `:forward 8080`, `:unforward 8080`, and `:tunnels`.

## Operate and debug

Actions use the selected service. A container action affects its whole Compose
service, including replicas. Use the Stacks tab for whole-stack deployment.
Every UI action shows its immutable target and requires typing the host name.
Offline or stale targets cannot be mutated. One mutation runs per stack, enforced
across local processes with a filesystem lock. `:cancel` targets the operation
whose output is open, or the selected row; `:cancel STACK` names a target explicitly.
Cancellation stops local command execution, then refreshes state. It does not undo remote changes already applied.
Wait for completion or cancel before quitting.

Logs follow selected containers, support search and pause, and retain at most
5,000 lines or 5 MiB. Recreated services switch to their new container IDs. A
reconnect marker identifies a boundary where duplicates or a gap may occur.
Diagnostic inspection shows state, health output, restart count, image, mounts,
ports, and networks. Environment values, labels, and command arguments are omitted.
Application logs and health output can themselves contain secrets. The effective
config view allowlists images, ports, mounts, networks, dependencies, profiles,
and restart policy; environment values, labels, and command arguments stay hidden.

Select a TCP port and press `f`, then enter `forward LOCAL_PORT`. Published ports
forward through the daemon host. Unpublished ports work when the container has
exactly one network address reachable from the host. Multi-network containers
need a published port. Forward listeners bind only to `127.0.0.1`. The console
owns these SSH processes and closes them on exit. UDP forwarding is unsupported.

Images, volumes, and networks are inspect-only. There is no system prune or volume
deletion command. Container removal preserves volumes and bind-mounted data.
Swarm is outside this console's scope; a stack means a Compose project here.

## CLI replacement for stack

```sh
./bin/tctl status
./bin/tctl status nexus grafana postgres
./bin/tctl services nexus
./bin/tctl config nexus grafana
./bin/tctl images nexus
./bin/tctl validate
./bin/tctl doctor nexus
./bin/tctl logs nexus grafana --lines 200 --follow
./bin/tctl top nexus grafana
./bin/tctl shell nexus grafana
./bin/tctl deploy nexus grafana
./bin/tctl deploy netdata
./bin/tctl update nexus grafana --force
./bin/tctl scale nexus SERVICE --replicas 2
./bin/tctl restart nexus grafana --yes
```

`ms`, `nx`, and `sg` are aliases. `nexus/grafana` is accepted instead of two
arguments. Available commands also include `pull`, `build`, `start`, `stop`,
`remove`, and `logs-follow`. Use `--yes` for explicit noninteractive mutation
confirmation and `--force` to recreate during deploy/update. Replica counts and
log limits use named flags rather than positional numeric arguments.

`deploy` without a target rolls out all stacks serially; `deploy SERVICE` selects
stacks containing that service. Failure stops the remaining rollout. Deployment
reconciles `docker/services/SERVICE/files` onto `STACK_DATA_ROOT/SERVICE`, then
uses Compose `up --wait`. Changed managed files force recreation. Selected-service
deployments use `--no-deps`. `update` pulls and builds before applying; `pull` also
refreshes build bases, preserving the previous stack command's behavior.

## Fresh data

Each host has its own async connection and in-memory snapshot. Docker events
trigger coalesced refreshes; a full snapshot runs every 15 seconds and on reconnect.
Event replay is not the source of truth. Failed hosts reconnect with backoff up
to 30 seconds and retain clearly labeled old data. A snapshot older than 30 seconds
is stale. Slow hosts never block key handling or another host's connection.

Running containers in the selected scope stream CPU, memory, and network counters
through Bollard. All-stack scope streams across all hosts. Stream membership is
reconciled every three seconds, and narrowing scope closes unused host streams.
Rendering is capped at 30 frames per second. Samples older than ten seconds show
as unavailable. Memory subtracts Linux's inactive
file cache. Network columns are cumulative received/transmitted bytes.

Repository Compose and env-file metadata are checked every two seconds. Changed
files cause desired-service resolution. Configuration failures do not hide live
containers. Expensive work happens outside Ratatui's render loop.

## Build, test, and release

```sh
make check
make build
```

Tests cover configuration boundaries, non-overwriting init, stats calculations,
fuzzy matching, layout, log bounds, installer failure handling, and CLI operation
against a temporary Unix-socket Docker fixture. The CLI fixture verifies explicit
routing, wrong-host refusal, confirmation requirements, and fleet stop-on-failure.
A real PTY test verifies offline startup, help, command navigation, filtering, and
clean exit. Tests do not contact or deploy to the live fleet.

`.github/workflows/tctl.yml` checks PRs and builds four platform archives. Successful
changes on `main` automatically create a `tctl-FULL_COMMIT_SHA` release containing
those archives and `SHA256SUMS`, then mark it latest. A manual dispatch on `main`
can publish the initial release. Rerunning a published commit leaves its assets
unchanged. Only the release job has write permission; PR builds use hosted runners
and read-only tokens. Actions are pinned to commit SHAs.

## Design references

The interaction design draws on [k9s](https://github.com/derailed/k9s),
[d9cker](https://github.com/loyalpartner/d9cker), and [d4s](https://github.com/jr-k/d4s)
for keyboard navigation and resource tabs, [ctop](https://github.com/bcicen/ctop)
for live metrics, and [docktui](https://github.com/0xShady/docktui) for the fleet
sidebar and inline state. The implementation here is original; no upstream code
or branded assets were copied.
