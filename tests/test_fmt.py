from __future__ import annotations

from tctl.fmt import format_compose_text


def test_format_compose_text_normalizes_order_and_key_value_lists() -> None:
    source = """services:
  zebra:
    labels:
      traefik.http.services.zebra.loadbalancer.server.port: "8080"
      traefik.enable: "true"
    environment:
      Z_VAR: last
      A_VAR: first
    depends_on:
      redis:
        required: true
        condition: service_healthy
    image: zebra:latest
  alpha:
    image: alpha:latest
"""

    expected = """services:
  alpha:
    image: alpha:latest

  zebra:
    depends_on:
      redis:
        condition: service_healthy
        required: true
    environment:
      - A_VAR=first
      - Z_VAR=last
    image: zebra:latest
    labels:
      - traefik.enable=true
      - traefik.http.services.zebra.loadbalancer.server.port=8080
"""

    assert format_compose_text(source) == expected


def test_format_compose_text_is_idempotent() -> None:
    source = """include:
  - path:
      - services/zeta/compose.yaml
      - ../../services/zeta/compose.yaml
  - path: ../../services/alpha/compose.yaml
name: demo
services:
  beta:
    environment:
      - Z_VAR=last
      - A_VAR=first
"""

    formatted = format_compose_text(source)

    assert format_compose_text(formatted) == formatted
