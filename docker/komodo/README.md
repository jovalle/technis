# Nexus FlareSolverr Pilot

The pilot follows only `ghcr.io/flaresolverr/flaresolverr:v3.x.y@sha256:…` from
protected `main`. It takes over one service in the existing `nexus` Compose
project. Komodo sees a generated Files on Host model containing that service,
the existing network, and its existing empty `/config` volume. The volume is
attached read-only. The coordinator accepts image-only updates and keeps
the rest of the enrolled model fixed. Renovate opens PRs for review.

## Verify the Candidate

```sh
python3 -m unittest discover -s docker/scripts -p test_pilot.py -v
python3 docker/scripts/pilot.py check
actionlint .github/workflows/compose.yml
docker compose -f docker/komodo/compose.yaml config --quiet
ansible-playbook ansible/playbooks/komodo.yaml --syntax-check
```

The policy checks render the complete Nexus Compose model with placeholders and
verify the effective FlareSolverr service, the pinned image, and the amd64
manifest. The first pin matches the registry content currently
running on Nexus. Subsequent changes to the service model or its env input
layers require a reviewed policy and enrollment change.

## Prepare the Host

1. In GitHub, require PRs on `main`, code-owner review of owned paths, strict
   required checks from the GitHub Actions integration, and rules blocking
   branch deletion and non-fast-forward updates. Require `Compose Policy` and
   `Gitleaks`. The coordinator also verifies the checks on each exact merged
   commit. Leave automerge off throughout the pilot.
2. Provision a private GitHub incident repository with Issues enabled and
   install a GitHub App scoped to that repository with Issues write and
   Metadata read. Store its private key and the Komodo service-user API
   key and secret in Infisical. Give that user Execute access to the Nexus
   Stack. Keep Core and Periphery on the private management plane. The Compose
   manifest binds Core to loopback, so use a trusted operator SSH tunnel for UI
   access.
3. Install the reviewed Git revision with
   `ansible-playbook ansible/playbooks/komodo.yaml -e komodo_pilot_revision=<merge-commit>`.
   The playbook installs the coordinator and timer with the timer inactive on
   a fresh host. Use a per-host Infisical identity scoped to the pilot's
   credentials. Provision the encrypted systemd credentials at
   `/etc/credstore.encrypted/technis-pilot-id` and
   `/etc/credstore.encrypted/technis-pilot-secret`, using the names
   `infisical_id` and `infisical_secret` when encrypting.
4. For a new Core, put the Infisical database password, initial Core admin
   password, and JWT signing secret into the files
   `/run/technis/komodo/database`, `/run/technis/komodo/admin`, and
   `/run/technis/komodo/jwt` by redirecting the corresponding `bin/vault get`
   calls into private files on tmpfs. Create `/run/technis/komodo/keys` with
   mode `0700`. Keep the top-level runtime directory root-only. Set the
   database file to owner root, group 999 and mode `0440` so the Mongo image
   can read it after dropping privileges. Rehydrate the files after a host
   reboot before restarting the management plane. Start the pinned Core and
   Periphery with `docker compose -f docker/komodo/compose.yaml up -d` from the
   installed checkout. If a private Core already exists, attach the Nexus
   Periphery agent there instead. Apply [stack.toml](stack.toml) as a resource
   sync after previewing it. Keep the Stack's direct webhooks, automatic
   updates, and global auto-update eligibility off.
5. On Nexus, find the existing volume name with
   `docker inspect -f '{{(index .Mounts 0).Name}}' flaresolverr`. Put it in the
   root-only `/var/lib/technis/pilot/settings.json` file along with the fields
   below. Use the actual service identity and eligible credentials to run
   `python3 docker/scripts/pilot.py enroll` from the installed checkout. It
   confirms the live image ID, healthy browser API through Prowlarr, and
   empty volume before staging the baseline. It does not restart the live
   service.

```json
{
  "project": "nexus",
  "stack": "nexus",
  "container": "flaresolverr",
  "network": "nexus_default",
  "architecture": "amd64",
  "observe_seconds": 300,
  "volume": "<existing-volume-name>",
  "repository": "jovalle/technis",
  "komodo_url": "http://127.0.0.1:9120",
  "komodo_key": "vault://technis/komodo/pilot-key",
  "komodo_secret": "vault://technis/komodo/pilot-secret",
  "incident_repository": "owner/private-incidents",
  "incident_app_id": 123,
  "incident_installation": 456,
  "incident_key": "vault://technis/komodo/incident-app-private-key",
  "promote": false
}
```

Run a disposable deployment and rollback through the selected Core and test
incident delivery in the configured private repository before changing
`promote` to `true` and starting `technis-pilot.timer`. Also confirm no other
manager can update FlareSolverr. After its first managed deployment, the
updated `stacks` CLI refuses whole-project Nexus changes and FlareSolverr
mutations through its manual entry points. Use explicit targets for other
Nexus services and keep earlier checkouts out of routine deployment.

## Recovery

The coordinator pre-pulls the candidate, captures its deployment phase on
disk, then waits for Komodo's completed Update. It checks the actual image,
health and restarts, confirms volume identity, and probes through Prowlarr
for the observation window. An interrupted attempt waits for in-flight
Komodo work before checking or restoring the last known healthy image.
Incident issues include the candidate and prior pins, exact Git revision,
failure phase and check, time, and recovery status. A private, durable
outbox retries unavailable GitHub calls without posting duplicate issues.

A failed digest remains quarantined even if it is still on `main`. Use an
owner-reviewed image fix or revert PR. The pilot does not write to the source
repository. If Core is unavailable and urgent recovery is needed, stop the
timer and isolate the executor, check for in-flight work, and restore only
the prior image recorded in root-only `state.json` using the staged model with
`--no-deps --no-build --pull never`. Confirm the recorded volume and test the
Prowlarr route before resuming. Keep the last healthy image cached while the
pilot is active.
