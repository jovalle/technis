FROM node:26-alpine AS frontend

RUN apk add --no-cache git && npm install -g pnpm@12.6.0
RUN git clone --depth 1 --branch v4.39.28 https://github.com/authelia/authelia.git /src \
    && test "$(git -C /src rev-parse HEAD)" = 8da42b2348b0351c6284a2940a9c5eec3d4cd3fd
WORKDIR /src
COPY docker/services/authelia/frontend.patch /tmp/frontend.patch
RUN git apply /tmp/frontend.patch
COPY assets/logo.svg web/public/static/media/technis-logo.svg
COPY docker/services/authelia/frontend/inter-latin.woff2 web/public/static/media/inter-latin.woff2
RUN cd web && pnpm install --frozen-lockfile --ignore-scripts && pnpm build && rm -rf node_modules

FROM golang:1.27.1-bookworm AS backend
WORKDIR /src
COPY --from=frontend /src .
RUN cp -r api internal/server/public_html/api \
    && CGO_ENABLED=1 go build -trimpath \
       -ldflags='-s -w -X github.com/authelia/authelia/v4/internal/utils.BuildTag=v4.39.28 -X github.com/authelia/authelia/v4/internal/utils.BuildState=tagged -X github.com/authelia/authelia/v4/internal/utils.BuildCommit=8da42b2348b0351c6284a2940a9c5eec3d4cd3fd -X github.com/authelia/authelia/v4/internal/utils.BuildExtra=technis' \
       -o /authelia ./cmd/authelia

FROM authelia/authelia:4.39.28
COPY --from=backend /authelia /app/authelia
COPY assets/logo.png assets/favicon.ico /app/assets/
COPY docker/services/authelia/frontend/OFL.txt /app/INTER-OFL.txt
