# Technis auth frontend

`Dockerfile` builds Authelia 4.39.28 from its tagged source and applies
`frontend.patch`. The patch makes passkeys the only first-factor sign-in method:
it removes the password form and unregisters `POST /api/firstfactor`. Signed-in
password reauthentication remains available for account and consent flows.
Authelia still handles MFA, consent, and sessions. The existing `/config` volume
keeps settings persistent; the image includes the Technis logo and favicon.

Set the `forward-auth` endpoint in Stargate's persistent
`/opt/stargate/authelia/configuration.yml` to use only the session cookie:

```yaml
server:
  endpoints:
    authz:
      forward-auth:
        implementation: ForwardAuth
        authn_strategies:
          - name: CookieSession
```

Authelia otherwise enables HTTP Basic authentication for this endpoint by
default. The live Stargate configuration was updated and validated on
2026-09-27.

From the repository root, build and update only Authelia on Stargate:

```sh
./bin/tctl -s stargate build authelia
./bin/tctl -s stargate up -d --no-deps --force-recreate authelia
./bin/tctl -s stargate ps authelia
test "$(curl -sS -o /dev/null -w '%{http_code}' -X POST https://auth.techn.is/api/firstfactor)" = 404
```

Confirm a discoverable passkey is enrolled before updating the service. Current
access rules require `one_factor`, which Authelia passkey login satisfies. A
future `two_factor` rule may prompt for a password after a passkey.

If this release fails on Stargate, retag the saved previous image
`technis/authelia:4.39.28-prev` as `technis/authelia:4.39.28`, restore
`configuration.yml.pre-passkey` if needed, and recreate only Authelia.

When upgrading Authelia, update the source tag, checked source commit, Go image,
final image tag, and Compose image tag together. Apply the patch to the new
source and rerun the frontend tests and build before switching the service.
