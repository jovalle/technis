# Dispatcharr

Dispatcharr runs on `nexus` and publishes port `9191` on loopback. Plex uses the
`Plex` channel profile at `http://127.0.0.1:9191/hdhr/Plex` and its guide at
`http://127.0.0.1:9191/output/epg/Plex?days=3&cachedlogos=false`.

## Administration

Run `ssh -N -L 9191:127.0.0.1:9191 nexus`, then open `http://127.0.0.1:9191`.
Sign in as `admin` using `vault://technis/dispatcharr/ADMIN_PASSWORD`.

The three Xtream accounts use a shared server group and one upstream connection.
To add a recovered endpoint to the live lineup, enable and refresh its account
under **M3U & EPG Manager**. Match its streams to the existing channels under
**Channels** and order the streams by endpoint number, starting with endpoint 1.
The channel numbers and guide mappings stay attached to the existing channels.

## Encrypted Storage

`dispatcharr-storage.service` unlocks and mounts the encrypted ZFS dataset at
`${STACK_DATA_ROOT}/dispatcharr/secure` before Docker starts. Compose binds its
`data` directory and requires the directory to exist. Check the mount service
with `systemctl status dispatcharr-storage.service` on `nexus` if Compose reports
a missing bind source.

The host uses a systemd encrypted credential for unattended startup. The
recovery passphrase is in `vault://technis/dispatcharr/ZFS_PASSPHRASE`. Recreate
the encrypted credential from Infisical when moving the dataset to a new host.
Use a raw ZFS send (`zfs send -w`) to preserve encryption in a ZFS backup.

Run `just compose nexus up -d --wait --no-deps dispatcharr` to redeploy the service.
