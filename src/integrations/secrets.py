"""API-key resolution: local `.env`/env var first, AWS Secrets Manager otherwise
(Principle IV). A key fetched from Secrets Manager is cached in a module-level dict so a
warm Lambda execution environment resolves it at most once per cold start.

Each Lambda receives its secret's ARN directly as an env var (e.g.
`TYPESAFE_API_KEY_SECRET_ARN`), set by Terraform from the same SSM-published ARN its IAM
policy is scoped to (infra/iam_*.tf). That keeps each execution role down to exactly one
permission — `secretsmanager:GetSecretValue` on its own secret's ARN, never another
function's — with no runtime `ssm:GetParameter` call to resolve the ARN first.
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path

from integrations.config import require_env

_cache: dict[str, str] = {}


class SecretsBackend(ABC):
    @abstractmethod
    def fetch(self, secret_arn: str) -> str:
        """Return the value of the secret identified by `secret_arn`."""


class AwsSecretsBackend(SecretsBackend):
    def __init__(self, secrets_client=None):
        import boto3

        self._secrets = secrets_client or boto3.client("secretsmanager")

    def fetch(self, secret_arn: str) -> str:
        return self._secrets.get_secret_value(SecretId=secret_arn)["SecretString"]


class StubSecretsBackend(SecretsBackend):
    """Network-free stand-in for tests (Principle IV, FR-011)."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = values or {}

    def fetch(self, secret_arn: str) -> str:
        return self.values[secret_arn]


def resolve_api_key(
    env_var: str, secret_arn_env_var: str, backend: SecretsBackend | None = None
) -> str:
    """Local-dev `.env`/env var `env_var` first; otherwise the secret whose ARN is in
    `secret_arn_env_var`, fetched from Secrets Manager via `backend` and cached by ARN for
    the lifetime of this process. Raises ConfigError if neither env var is set.
    """
    local_value = os.environ.get(env_var)
    if local_value:
        return local_value
    return _fetch_secret(secret_arn_env_var, backend)


def resolve_secret_file(
    path_env_var: str, secret_arn_env_var: str, backend: SecretsBackend | None = None
) -> str:
    """For secrets that are files rather than one-line keys (e.g. a GitHub App's `.pem`
    private key): local dev reads the file whose path is in `path_env_var`; otherwise the
    secret whose ARN is in `secret_arn_env_var` is fetched and cached exactly as in
    `resolve_api_key`. Raises ConfigError if neither env var is set.
    """
    local_path = os.environ.get(path_env_var)
    if local_path:
        return Path(local_path).read_text(encoding="utf-8")
    return _fetch_secret(secret_arn_env_var, backend)


def _fetch_secret(secret_arn_env_var: str, backend: SecretsBackend | None) -> str:
    secret_arn = require_env(secret_arn_env_var)
    if secret_arn in _cache:
        return _cache[secret_arn]

    backend = backend or AwsSecretsBackend()
    value = backend.fetch(secret_arn)
    _cache[secret_arn] = value
    return value
