# Kasm Workspaces

`compose.yaml` defines the active Kasm Workspaces deployment on Nexus, and
`branding.compose.override.yaml` adds the Technis assets. The Nexus stack
includes both files as one repository-managed deployment.

## Persistent dependencies on Nexus

- `/mnt/data/kasm/current` points to the active Kasm release configuration,
  certificates, health checks, logs, and temporary directories.
- `/mnt/data/kasm/credentials/postgres_password` contains the database bootstrap
  password. It must be owned by numeric UID/GID `70:70` with mode `0400` so the
  container's `postgres` user can read it, and it is never stored in Git.
- `kasm_db_1.18.1` is the existing external PostgreSQL data volume.
- `kasm_default_network` is the external Kasm control-plane network.
- `/mnt/data/kasm/branding/assets` must contain the files tracked under
  `docker/stacks/nexus/services/kasm/_/branding`.
- `/mnt/data/kasm/config/technis-dockhand-label.sql` adds
  `dockhand.update=false` to the `All Users` Docker Run Config. Kasm merges
  that group setting into workspace definitions, so every user-session
  container is excluded from Dockhand update checks without replacing a
  workspace's existing Docker Run Config.

The release bind mounts use `/mnt/data/kasm/current`, not a versioned release
directory. Updating the symlink and `KASM_VERSION` keeps the version change in
Git instead of a second remote-only Compose tree.

## Operations

The repository currently has no root command that deploys Kasm or copies its
branding files to Nexus.

The legacy `kasm.service` unit on Nexus must remain disabled. Starting it would
reintroduce the installer-owned Compose project from
`/mnt/data/kasm/current/docker/docker-compose.yaml`.

Kasm web assets may be cached by the browser. Use a hard refresh after changing
branding.

Kasm's agent creates internal helper containers with a hard-coded
`kasm.helper` label and does not expose a configuration option for adding
labels to them. You can remove a stale helper after confirming that it is
stopped and has the `kasm.helper` label. The agent recreates helpers when
needed.
