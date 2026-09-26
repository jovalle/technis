FROM pgvector/pgvector:pg17

# Pinned to the version Tracearr's data was dumped from; upgrade with ALTER EXTENSION afterwards.
ARG TIMESCALEDB_VERSION=2.28.3~debian12-1710
ARG TIMESCALEDB_TOOLKIT_VERSION=1:1.26.0~debian12

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/timescaledb.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/timescaledb.gpg] https://packagecloud.io/timescale/timescaledb/debian/ bookworm main" \
      > /etc/apt/sources.list.d/timescaledb.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      "timescaledb-2-loader-postgresql-17=${TIMESCALEDB_VERSION}" \
      "timescaledb-2-postgresql-17=${TIMESCALEDB_VERSION}" \
      "timescaledb-toolkit-postgresql-17=${TIMESCALEDB_TOOLKIT_VERSION}"; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*
