# Komodo Compose Releases

Renovate opens PRs for digest-pinned images. GitHub checks the effective
Compose service and its registry manifest. Eligible digest, patch, and minor
updates use Renovate automerge. The release coordinator follows `main` and
promotes a commit only after **Compose Policy** and **Gitleaks** pass on that
exact commit.
Komodo deploys the service within the host's existing Compose project.

[releases.toml](releases.toml) describes each eligible host, service, image
repository, runtime environment, and probe. Nexus enrolls FlareSolverr and
Redis Exporter. The release engine supports services without persistent storage and
services with one verified empty volume mounted read-only. Each enrolled
service has its own journal. One host lock serializes updates within the
project. Spec changes and stateful upgrades require an explicit recovery
procedure before they can use this release path.

Redis Exporter shares a Compose file with Redis and inherits its non-root
user from the pinned image. The coordinator checks the image user and
volume defaults, keeps the exporter settings, and requires the running
Redis dependency to be healthy. The deployment updates only the exporter
and the client probe verifies `redis_up 1` through its metrics endpoint.

## Choose the Runtime

Core, MongoDB, and Periphery run in Compose. The coordinator is a systemd
oneshot with a timer because it needs to inspect host Docker volumes, keep
recovery state outside the managed stack, and load encrypted host credentials
using `LoadCredentialEncrypted`. The timer runs two minutes after boot and
two minutes after the previous run finishes.

A Compose service could package the same coordinator. It would need its own
bootstrap project, persistent host state, Docker socket access and a
host-volume inspection mechanism. It would also need a way to load the
Infisical bootstrap identity into memory on startup. Compose secret mounts
can pass that credential to the container after the host prepares it. Keep
the coordinator in systemd while it performs those host-local operations.

## Verify the Configuration

```sh
python3 -m unittest discover -s docker/scripts -p test_release.py -v
python3 docker/scripts/release.py check
actionlint .github/workflows/compose.yml
docker compose -f docker/komodo/compose.yaml config --quiet
ansible-playbook ansible/playbooks/komodo.yaml --syntax-check
```

The checkout must contain a valid catalog and digest for each managed
service. GitHub Actions uses fixture values to render Compose. Owners review
changes to policy, workflows, and deployment tooling.

## Prepare a Host

1. Keep each Renovate automerge rule limited to a cataloged service and its
   approved update types. Renovate checks pull-request CI before it merges.
   The release coordinator checks **Compose Policy** and **Gitleaks** again
   on the resulting commit on `main`.
2. Create a private GitHub incident repository with Issues enabled and
   install a GitHub App there with Issues write and Metadata read. Store
   the App private key and Komodo service-user credentials in Infisical.
   Grant the service user Execute access to its host's Stack. Bind Core to
   loopback or use a private TLS management endpoint.
3. Install the coordinator with
   `ansible-playbook ansible/playbooks/komodo.yaml -e komodo_release_revision=<merge-commit>`.
   The play targets `nexus` by default and accepts `komodo_release_hosts`
   for another enrolled host. Provision the encrypted systemd credentials
   `/etc/credstore.encrypted/technis-release-id` and
   `/etc/credstore.encrypted/technis-release-secret` under the credential
   names `infisical_id` and `infisical_secret`, using a scoped host
   machine identity.
4. For a new Core, resolve the database password, initial admin password,
   and JWT signing secret from Infisical directly into private tmpfs files
   `/run/technis/komodo/database`, `/run/technis/komodo/admin`, and
   `/run/technis/komodo/jwt`. Create the key directory at
   `/run/technis/komodo/keys` with mode `0700`. Keep the top-level runtime
   directory root-only. Set the database file to owner root, group 999 and
   mode `0440` so Mongo can read it after dropping privileges. Restore these
   runtime files after host reboot before restarting the management plane.
   Start Core with `docker compose -f docker/komodo/compose.yaml up -d`.
   Apply [stack.toml](stack.toml) after previewing it. Each host has one
   Files on Host Stack.
5. Put the settings below in root-owned mode `0600`
   `/var/lib/technis/releases/settings.json`. Supply the local Komodo Server
   ID and app installation identifiers through Infisical-backed host
   provisioning. For a target with an empty read-only volume, inspect its
   current Docker volume and add the volume name to `volumes`. Run
   `python3 docker/scripts/release.py enroll flaresolverr` from the
   installed reviewed checkout under the scoped Infisical identity. It
   checks the running image, client route, and volume before staging
   the baseline. Stop other writers for that service before enrolling it.
   The CLI detects Komodo ownership after the first managed deployment.
6. Rerun the installation play. It enables and starts
   `technis-release.timer` once the settings, credentials, and enrollment
   are present. Production promotion defaults to enabled. Run the issue
   delivery drill against the configured private repository and use
   one scoped update to verify promotion on the host.

```json
{
  "host": "nexus",
  "server_id": "vault://technis/komodo/server-id",
  "volumes": { "flaresolverr": "<existing-empty-volume-name>" },
  "repository": "jovalle/technis",
  "komodo_url": "http://127.0.0.1:9120",
  "komodo_key": "vault://technis/komodo/release-key",
  "komodo_secret": "vault://technis/komodo/release-secret",
  "incident_repository": "vault://technis/komodo/incident-repository",
  "incident_app_id": "vault://technis/komodo/incident-app-id",
  "incident_installation": "vault://technis/komodo/incident-installation",
  "incident_key": "vault://technis/komodo/incident-app-private-key"
}
```

## Recover a Failed Release

The coordinator journals the attempt before changing a service, pre-pulls
the candidate, and waits for Komodo's completed Update. It verifies the
running image, container health, volume identity, and client route for five
minutes. Recovery waits for in-flight Komodo work, restores the last healthy
image, and runs the same checks. A failed digest stays quarantined until a
different image is merged. Use a reviewed fix or revert PR to reconcile Git.
After an operator applies an approved configuration change or rewrites `main`
away from the enrolled revision, rerun enrollment
against the new verified baseline. Enrollment preserves quarantine and
incident history.

A private outbox reports the service, pins, exact Git revision, failure
phase, and recovery result. Publishable fields are allowlisted. The
coordinator keeps raw responses and credentials out of the issue payload.

To pause fresh promotions while keeping recovery active, set `promote` to
`false` in the host settings. For emergency manual recovery, stop the timer,
isolate the executor, and confirm it has no work in flight. Restore the pin
from the service's root-only journal through the staged model with
`--no-deps --no-build --pull never`. Verify its normal client route before
resuming automation.
