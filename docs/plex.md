# Plex

## Modifying Database

### Bulk File Path Changes

Deployed an alpine container and exec'ed inside:

```sh
apk add --no-cache sqlite
sqlite3 "/mnt/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
```

#### SQL Commands

```sql
SELECT id, file FROM media_parts WHERE file LIKE '%/data/series%';
UPDATE media_parts SET file = REPLACE(file, '/data/series', '/mnt/media/series');
SELECT id, file FROM media_parts WHERE file LIKE '%/data/movies%';
UPDATE media_parts SET file = REPLACE(file, '/data/movies', '/mnt/media/movies');
```
