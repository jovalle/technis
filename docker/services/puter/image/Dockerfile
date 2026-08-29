FROM ghcr.io/heyputer/puter:main@sha256:393e0838d1432a22e5760dc78377ab9e1eedc7631348b9220e194b5a771c5f45

# Puter currently relies on Express's two-label default when classifying
# subdomains. Internal techn.is services use a three-label application domain,
# so set the offset from the configured public domain before Puter starts.
COPY express-domain-offset.cjs /opt/puter/express-domain-offset.cjs
ENV NODE_OPTIONS=--require=/opt/puter/express-domain-offset.cjs
