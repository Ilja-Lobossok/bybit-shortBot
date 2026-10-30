"""Shared, environment-only Grafana Cloud credentials and HTTPS exporter factory."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Messages contain variable names, never their values."""


@dataclass(frozen=True)
class CloudCredentials:
    endpoint: str = field(repr=False)
    username: str = field(repr=False)
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> CloudCredentials:
        def required(name):
            value = env.get(name, "")
            if not value or value != value.strip() or any(char.isspace() for char in value):
                raise ConfigurationError(f"Set a non-empty, whitespace-free {name}")
            return value

        base = required("OTEL_EXPORTER_OTLP_ENDPOINT")
        try:
            url = urlsplit(base)
            valid = (url.scheme == "https" and url.hostname is not None
                     and url.hostname.endswith(".grafana.net") and url.port in (None, 443)
                     and url.username is None and url.password is None
                     and not url.query and not url.fragment
                     and url.path.rstrip("/") == "/otlp" and "\\" not in base)
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError(
                "OTEL_EXPORTER_OTLP_ENDPOINT must be the HTTPS Grafana Cloud base "
                "endpoint ending in /otlp, without credentials, query, or fragment"
            )
        username = required("GRAFANA_CLOUD_OTLP_USERNAME")
        if not username.isascii() or not username.isdecimal():
            raise ConfigurationError("GRAFANA_CLOUD_OTLP_USERNAME must be the numeric OTLP instance ID")
        token = required("GRAFANA_CLOUD_API_TOKEN")
        if not token.isascii():
            raise ConfigurationError("GRAFANA_CLOUD_API_TOKEN must contain ASCII characters")
        return cls(base.rstrip("/") + "/v1/metrics", username, token)


def create_otlp_exporter(credentials, *, timeout_seconds=5.0, exporter_type=None):
    # Lazy imports keep disabled live monitoring independent of the SDK.
    import requests
    if exporter_type is None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        exporter_type = OTLPMetricExporter
    authorization = base64.b64encode(
        f"{credentials.username}:{credentials.token}".encode("ascii")
    ).decode("ascii")
    session = requests.Session()
    session.trust_env = False
    try:
        return exporter_type(
            endpoint=credentials.endpoint,
            headers={"Authorization": "Basic " + authorization},
            timeout=timeout_seconds, session=session, max_request_size=64 * 1024,
        )
    except BaseException:
        session.close()
        raise
