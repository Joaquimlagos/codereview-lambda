"""API-key resolution: local `.env`/env var first, AWS Secrets Manager otherwise
(Principle IV). A key fetched from Secrets Manager is cached in a module-level dict so a
warm Lambda execution environment resolves it at most once per cold start.

Each caller looks its secret's ARN up from its own SSM parameter (e.g.
`/codereview/secrets/typesafe-api-key-arn`), so each Lambda's IAM execution role can be
scoped to `secretsmanager:GetSecretValue` on exactly the one ARN it needs — never the other
function's secret.
"""

import os
from abc import ABC, abstractmethod

_cache: dict[str, str] = {}


class SecretsBackend(ABC):
    @abstractmethod
    def fetch(self, ssm_arn_param: str) -> str:
        """Resolve `ssm_arn_param` to a secret ARN via SSM, then return that secret's value."""


class AwsSecretsBackend(SecretsBackend):
    def __init__(self, ssm_client=None, secrets_client=None):
        import boto3

        self._ssm = ssm_client or boto3.client("ssm")
        self._secrets = secrets_client or boto3.client("secretsmanager")

    def fetch(self, ssm_arn_param: str) -> str:
        arn = self._ssm.get_parameter(Name=ssm_arn_param)["Parameter"]["Value"]
        return self._secrets.get_secret_value(SecretId=arn)["SecretString"]


class StubSecretsBackend(SecretsBackend):
    """Network-free stand-in for tests (Principle IV, FR-011)."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = values or {}

    def fetch(self, ssm_arn_param: str) -> str:
        return self.values[ssm_arn_param]


def resolve_api_key(env_var: str, ssm_arn_param: str, backend: SecretsBackend | None = None) -> str:
    """Local-dev `.env`/env var first; otherwise Secrets Manager via `backend`, cached by
    `ssm_arn_param` for the lifetime of this process.
    """
    local_value = os.environ.get(env_var)
    if local_value:
        return local_value

    if ssm_arn_param in _cache:
        return _cache[ssm_arn_param]

    backend = backend or AwsSecretsBackend()
    value = backend.fetch(ssm_arn_param)
    _cache[ssm_arn_param] = value
    return value
