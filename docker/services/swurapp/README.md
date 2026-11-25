# Swurapp Releases on Nexus

Swurapp writes `state.json` to its dedicated ZFS dataset at
`$STACK_DATA_ROOT/swurapp`. The container mounts that directory at
`/config` and keeps its read-only root filesystem. Provision the dataset
before deploying. Its mountpoint must be owned by UID and GID 568 with
mode `0700`. Compose refuses to create a missing source directory.

## Check a Candidate

Use SHA-256 image pins for the current and candidate images. Rehearse both
with a disposable Sonarr API and synthetic episodes. Check a full worker
cycle, including the written JSON, episode changes, and queued searches.
Restart the candidate to check state persistence. Exercise a failed
deployment by restoring the previous image and pre-release state.

## Deploy on Nexus

1. Verify the dedicated ZFS dataset is mounted at the expected path and
   the current Sonarr container is healthy.
2. Cache both pinned images before stopping Swurapp. Record the current
   pin and stop only Swurapp so it cannot write during the snapshot.
3. Snapshot the dedicated Swurapp dataset with a unique pre-release name.
   Deploy only the reviewed Swurapp service with `--no-deps --pull never`.
4. Verify the running pin, read-only root filesystem, writable `/config`
   bind, and a successful worker cycle. Check that the worker writes valid
   state and stays running without restarts.

## Restore the Previous Release

1. Stop and verify that the candidate has stopped. Take a separate snapshot
   of its state for inspection.
2. Preserve both snapshots. Restore the previous `state.json` from the
   pre-release snapshot with its original ownership. If the snapshot was
   empty, move the candidate state aside. Use an atomic rename within the
   dataset when replacing the active state file.
3. Deploy the prior digest and its reviewed Compose revision, targeting
   only Swurapp. Older revisions may have no `/config` mount. Check the
   running pin and worker cycle before reconciling Git with a revert PR.

Image and dataset recovery cover Swurapp itself. Review Sonarr monitoring
changes and queued searches separately. Keep Renovate automerge disabled for
Swurapp and leave it outside the Komodo release timer.
