# Technis auth frontend

`Dockerfile` builds Authelia 4.39.28 from its tagged source and applies
`frontend.patch`. The patch changes presentation and brand text; Authelia keeps
handling sign-in, MFA, passkeys, consent, and sessions. The existing `/config`
volume keeps settings persistent; the image includes the Technis logo and favicon.

From the repository root, build and update only Authelia on Stargate:

```sh
./bin/tctl -s stargate build authelia
./bin/tctl -s stargate up -d --no-deps authelia
```

If the custom image fails, switch the Compose image back to the official
`authelia/authelia:4.39.28` image and recreate only Authelia.

When upgrading Authelia, update the source tag, checked source commit, Go image,
final image tag, and Compose image tag together. Apply the patch to the new
source and rerun the frontend tests and build before switching the service.
