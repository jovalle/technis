<div align="center">

<img src="assets/icon.svg" alt="Technis Logo" width="400" height="400">

# Technis

[![Home Internet](https://img.shields.io/endpoint?url=https://status.techn.is/api/v1/endpoints/_connectivity/health/badge.shields&style=for-the-badge&logo=ubiquiti&logoColor=white&label=Home%20Internet&cacheSeconds=60)](https://status.techn.is/endpoints/_connectivity) [![Status Page](https://img.shields.io/endpoint?url=https://status.techn.is/api/v1/endpoints/_status/health/badge.shields&style=for-the-badge&logo=statuspage&logoColor=white&label=Status%20Page&cacheSeconds=60)](https://status.techn.is)

![Availability](https://status.techn.is/api/v1/endpoints/_status/uptimes/24h/badge.svg) ![Node Uptime](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_min_uptime&style=flat&label=uptime) ![CPU Usage](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_cpu_usage&style=flat&label=cpu) ![Memory Usage](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_memory_usage&style=flat&label=memory) ![Docker Containers](https://img.shields.io/endpoint?url=https%3A%2F%2Fstat.techn.is%2Fquery%3Fmetric%3Ddocker_containers_running%26v%3D2&style=flat&label=containers&cacheSeconds=300)

</div>

## Project layout

- `ansible/`, `docker/`, `kubernetes/`, and `terraform/` contain infrastructure configuration.
- `console/` contains the local hardware console.
- `docs/` and `web/` are separate site repositories; `assets/` and `tools/` provide shared resources and utilities.

## Local tools

```sh
just open console ms-01  # Open one AMT console; omit the host to open all.
just stop                # Stop the background web servers.
```

Save controller credentials to Infisical with `just vault ITEM/HOST/FIELD`, e.g. `just vault amt/ms-01/password`.
See [tool setup and credential handling](console/README.md).

## Secrets

No secret lives in this repository. Infisical holds the Technis secrets; Git holds
only `vault://` references. The Compose resolver and hardware console use the
Infisical API directly.

Compose stacks read layered `.env` files, each line a literal or a
`{{ vault://technis/ITEM/[SECTION/]FIELD }}` reference:

| Scope                | File                                   |
| -------------------- | -------------------------------------- |
| Every stack          | `docker/.env`                          |
| One stack            | `docker/stacks/HOST/.env`              |
| One app, every stack | `docker/services/APP/.env`             |
| One app on one stack | `docker/stacks/HOST/services/APP/.env` |

The files are tracked and hold only references and non-secret literals.

The `just` stack commands resolve them with the Infisical API into a private temporary directory
for each run, so each app interpolates only common, stack and its own values.
Plain `docker compose` in a stack directory requires the generated environment from these commands.

Use `just deploy [target]` and `just check [target]` for all stacks or a
specific target; `check` also shows container status. `just logs <target> [-f]`
shows logs, and `just update <target>` pulls images before recreating services. A target can
be a stack, a service found across stacks, or `stack/service`; repeat `-s`
(or use comma-separated names) to filter stacks. For example, `just logs tailscale -f` follows Tailscale on every matching stack, and `just deploy immich -s nexus`
updates Immich on Nexus. Other multi-stack changes require confirmation or `--yes`.
Use `just compose nexus <args...>` for other Compose commands; the existing
restriction on commands that print interpolated configuration still applies.

Detached deployments show each reported Compose resource as it changes. On a
terminal the view fits multiple columns and cycles through pages when necessary;
a complete list and Compose errors follow the live display. Redirected output is
plain text. Container logs are not collected automatically; use `just logs`
for details. Set `STACKS_PROGRESS=compose` to use Compose's own progress view.
`Started` reports Compose's state and does not establish service health.

`just sync nexus` copies files from the shared service `files/` directories to the
host; use `just sync nexus -n` to preview, or name services after the stack.
Stack-specific files are not included. Use `just vault -p ITEM/FIELD` to store a
secret interactively, or `just vault -e ITEM/FIELD` for a multiline value.
Set `INFISICAL_URL` and either `INFISICAL_TOKEN` or `INFISICAL_CLIENT_ID` and
`INFISICAL_CLIENT_SECRET` in your local environment. The command defaults to
the Technis project and its `prod` environment; `INFISICAL_PROJECT_ID` and
`INFISICAL_ENVIRONMENT` override those defaults. Run `just` to list commands.

`check` validates the complete local Compose model, and `deploy` and `update`
run the same preflight before making changes. If a variable has no value or default,
preflight prompts on a terminal without echoing the value; Enter accepts, Esc
skips, and Ctrl-C stops. Supplied values last only for that invocation. Skipped
values or invalid models block changes. Without a terminal, missing values fail
the preflight. Preflight resolves Infisical references and validates the resulting model before
any changes. It does not check remote files, devices, or external volumes.
Read-only commands (`ps`, `logs`, `config --services`) use placeholders and never
contact Infisical. Values that nothing references yet are kept in the `archive` item.

Kubernetes configuration is absent from this checkout and has not been migrated.

### Pilot image update

The pilot currently accepts only `nexus/flaresolverr` and an immutable image
reference in the form `ghcr.io/flaresolverr/flaresolverr@sha256:<digest>`.
Find the current candidate with:

```sh
docker buildx imagetools inspect ghcr.io/flaresolverr/flaresolverr:latest
```

Copy the top-level `Digest` shown before the per-platform `Manifests` and append
it to the image name after `@`. Run `just update pilot nexus/flaresolverr <image>`
to preview, or add `--apply` to pull and deploy the candidate. The pilot renders
only FlareSolverr with placeholders for unrelated secrets; it fetches no
Infisical secrets and stops if the service requires one. It observes health and
restores and verifies the previous image if validation fails.

`just test pilot` exercises deployment and rollback with disposable, network-isolated
containers and images on Nexus. It requires `busybox:latest` to be cached there
and removes the test resources afterward.

An interrupted run or failed recovery leaves a stopped Docker container named
`technis-update-lock-flaresolverr` on Nexus. Do not remove it until the service
and its image are verified and recovery is complete; its image ID identifies the
previous image. Avoid other deployment tools during a pilot run: the lock only
coordinates pilot commands. The staged GitHub deployment and Renovate policy are
disabled until the restricted host identity, Tailscale and Infisical workload
identities, branch-protection-compatible Git recovery, and end-to-end failure
reporting are provisioned and verified. The GitHub check workflow runs without
access to Nexus or secrets.

The deployment workflow expects a Tailscale OIDC identity restricted to the
`pilot` GitHub environment and a `tag:pilot-ci` node that can reach only Nexus
SSH. Its SSH key is read from Infisical `/ci/pilot/PILOT_DEPLOY_SSH_KEY` and
loaded into an ephemeral agent. The Nexus key must force
`/usr/bin/python3 -I /root/technis-pilot/docker/scripts/pilot_gateway.py` with
SSH's `restrict` option; the gateway is not a general shell or Docker API.
Recovery uses a separate Infisical OIDC identity scoped to
`/ci/pilot-recovery/PILOT_GITHUB_APP_KEY` and a GitHub App with repository-only
contents-write access and the required branch-protection bypass. The workflow's
own token is limited to creating issues. Both GitHub environments must be
restricted to `main` before either identity is trusted.

The workflow reads these GitHub Actions variables (they are identifiers and
public connection details, not secret values):

- `PILOT_DEPLOY_ENABLED`: set to `true` only after the checks below pass.
- `PILOT_TS_CLIENT_ID` and `PILOT_TS_AUDIENCE`: Tailscale workload identity.
- `PILOT_INFISICAL_DEPLOY_PROJECT_ID`, `PILOT_INFISICAL_RECOVERY_PROJECT_ID`,
  `PILOT_INFISICAL_IDENTITY_ID`, and `PILOT_INFISICAL_RECOVERY_ID`: the two
  isolated Infisical projects and their respective read-only identities. Each
  project must contain only its designated pilot credential.
- `PILOT_GITHUB_APP_ID` and `PILOT_GITHUB_INSTALLATION_ID`: recovery app.
- `PILOT_HOST` and `PILOT_HOST_KEY`: Nexus Tailscale address and its verified
  SSH host public key.

Before enabling, require the pilot check and up-to-date branches in GitHub
branch protection, permit only the recovery app to bypass that protection, and
configure Renovate automerge for the pilot. Verify a successful deployment and
a deliberately failed candidate end to end, including the recovery commit,
issue, and retained lock when recovery cannot be confirmed. An unsuccessful
deployment leaves the workflow failed even if rollback succeeds. An issue
links to the workflow run for details; raw container logs are not published.
