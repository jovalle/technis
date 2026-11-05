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
