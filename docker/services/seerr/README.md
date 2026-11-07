# Seerr Tracearr Adapter

The `seerr-tracearr` service exposes the Tautulli API subset used by Seerr. Its settings are stored in Infisical under `seerr/nexus`: `ADAPTER_API_KEY`, `TRACEARR_API_KEY`, and `TRACEARR_SERVER_ID`. The Tracearr key belongs to its owner account; the server ID identifies the Plex server in Tracearr.

Sync changes with `just sync nexus seerr`, then restart the adapter with `just compose nexus restart seerr-tracearr`. Seerr's Tautulli settings use `seerr-tracearr:8181`, without SSL, and the adapter key.

The adapter reads grouped plays from Tracearr's public v2 API and caches results for two minutes. It looks up shows in Tracearr's library catalog and requests history for the matching title only. Its counts can differ from Tautulli's, and records without Plex rating keys or account IDs cannot be matched. Show history cannot be matched when Tracearr has no catalog record for that show.

On a new host, ensure the synced `seerr/adapter.py` and `plex/discovery.py` are readable by UID or GID 568 before starting their services.
