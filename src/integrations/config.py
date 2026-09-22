"""Configuration resolution: required env-var reads (FR-009), plus non-secret config
(e.g. the diffs bucket name) that falls back to a plain SSM parameter in production.

The Lambda ARN registry itself is now published natively by Terraform
(infra/arn_publish.tf) — this module no longer publishes ARNs; see git history for the
removed ArnPublisher/SsmArnPublisher/StubArnPublisher/arn_ssm_path, superseded by that
Terraform resource plus codereview-infra's lambda_arns.tf, which reads it directly.
"""

import os
from abc import ABC, abstractmethod


class ConfigError(Exception):
    """Raised when required configuration is missing."""


def require_env(name: str) -> str:
    """Read a required environment variable; every config value comes only from here (FR-009)."""
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


class SsmConfigBackend(ABC):
    @abstractmethod
    def fetch(self, ssm_param: str) -> str:
        """Fetch a plain (non-secret) SSM parameter's value."""


class AwsSsmConfigBackend(SsmConfigBackend):
    def __init__(self, client=None):
        import boto3

        self._client = client or boto3.client("ssm")

    def fetch(self, ssm_param: str) -> str:
        return self._client.get_parameter(Name=ssm_param)["Parameter"]["Value"]


class StubSsmConfigBackend(SsmConfigBackend):
    """Network-free stand-in for tests (Principle IV, FR-011)."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = values or {}

    def fetch(self, ssm_param: str) -> str:
        return self.values[ssm_param]


_config_cache: dict[str, str] = {}


def resolve_config_value(
    env_var: str, ssm_param: str, backend: SsmConfigBackend | None = None
) -> str:
    """Local-dev env var first; otherwise a plain SSM parameter, cached by `ssm_param` for
    the lifetime of this process. For non-secret, environment-specific config (e.g. the
    diffs bucket name published by codereview-infra's s3.tf) — secrets (API keys, tokens)
    use `resolve_api_key` in `integrations/secrets.py` instead, which reads Secrets Manager.
    """
    local_value = os.environ.get(env_var)
    if local_value:
        return local_value

    if ssm_param in _config_cache:
        return _config_cache[ssm_param]

    backend = backend or AwsSsmConfigBackend()
    value = backend.fetch(ssm_param)
    _config_cache[ssm_param] = value
    return value
