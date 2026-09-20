FROM golang:1.25.1-alpine AS build

WORKDIR /src
COPY main.go .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /go-check main.go

FROM scratch

COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /go-check /go-check
USER 65532:65532
ENTRYPOINT ["/go-check"]
