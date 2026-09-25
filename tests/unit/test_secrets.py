"""Unit tests: API-key resolution prefers a local env var, otherwise fetches from Secrets
Manager (via a stub backend here) using the secret ARN Terraform sets as an env var, and
caches the result at module level.
"""

import pytest

import integrations.secrets as secrets_module
from integrations.config import ConfigError
from integrations.secrets import (
    AwsSecretsBackend,
    StubSecretsBackend,
    resolve_api_key,
    resolve_secret_file,
)

SOME_ARN = "arn:aws:secretsmanager:us-east-1:000000000000:secret:codereview/some-key-AbCdEf"


@pytest.fixture(autouse=True)
def _clear_cache():
    secrets_module._cache.clear()


def test_env_var_takes_precedence_over_secrets_manager(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "from-env")
    monkeypatch.setenv("SOME_API_KEY_SECRET_ARN", SOME_ARN)
    backend = StubSecretsBackend({SOME_ARN: "from-secrets-manager"})

    value = resolve_api_key("SOME_API_KEY", "SOME_API_KEY_SECRET_ARN", backend=backend)

    assert value == "from-env"


def test_falls_back_to_secret_arn_from_env_var(monkeypatch):
    monkeypatch.delenv("SOME_API_KEY", raising=False)
    monkeypatch.setenv("SOME_API_KEY_SECRET_ARN", SOME_ARN)
    backend = StubSecretsBackend({SOME_ARN: "from-secrets-manager"})

    value = resolve_api_key("SOME_API_KEY", "SOME_API_KEY_SECRET_ARN", backend=backend)

    assert value == "from-secrets-manager"


def test_missing_key_and_secret_arn_raises_config_error(monkeypatch):
    """Neither a local key nor a deployed secret ARN: fail with a message naming the missing
    env var, rather than an opaque AWS error from a lookup that could never succeed."""
    monkeypatch.delenv("SOME_API_KEY", raising=False)
    monkeypatch.delenv("SOME_API_KEY_SECRET_ARN", raising=False)

    with pytest.raises(ConfigError, match="SOME_API_KEY_SECRET_ARN"):
        resolve_api_key("SOME_API_KEY", "SOME_API_KEY_SECRET_ARN", backend=StubSecretsBackend())


def test_secrets_manager_result_is_cached_across_calls(monkeypatch):
    monkeypatch.delenv("SOME_API_KEY", raising=False)
    monkeypatch.setenv("SOME_API_KEY_SECRET_ARN", SOME_ARN)

    class CountingBackend(StubSecretsBackend):
        def __init__(self, values):
            super().__init__(values)
            self.fetch_count = 0

        def fetch(self, secret_arn: str) -> str:
            self.fetch_count += 1
            return super().fetch(secret_arn)

    backend = CountingBackend({SOME_ARN: "cached-value"})

    first = resolve_api_key("SOME_API_KEY", "SOME_API_KEY_SECRET_ARN", backend=backend)
    second = resolve_api_key("SOME_API_KEY", "SOME_API_KEY_SECRET_ARN", backend=backend)

    assert first == second == "cached-value"
    assert backend.fetch_count == 1


def test_aws_backend_calls_get_secret_value_directly_with_the_arn():
    """The execution roles grant only secretsmanager:GetSecretValue on one ARN — no
    ssm:GetParameter — so the real backend MUST NOT resolve the ARN through SSM first."""

    class FakeSecretsManagerClient:
        def __init__(self):
            self.calls = []

        def get_secret_value(self, SecretId):
            self.calls.append(SecretId)
            return {"SecretString": "the-secret"}

    client = FakeSecretsManagerClient()

    value = AwsSecretsBackend(secrets_client=client).fetch(SOME_ARN)

    assert value == "the-secret"
    assert client.calls == [SOME_ARN]


def test_secret_file_path_takes_precedence_over_secrets_manager(monkeypatch, tmp_path):
    pem_file = tmp_path / "app.pem"
    pem_file.write_text("-----BEGIN PRIVATE KEY-----\nlocal\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("SOME_KEY_PATH", str(pem_file))
    monkeypatch.setenv("SOME_KEY_SECRET_ARN", SOME_ARN)
    backend = StubSecretsBackend({SOME_ARN: "from-secrets-manager"})

    value = resolve_secret_file("SOME_KEY_PATH", "SOME_KEY_SECRET_ARN", backend=backend)

    assert "local" in value


def test_secret_file_falls_back_to_secrets_manager_without_a_path(monkeypatch):
    monkeypatch.delenv("SOME_KEY_PATH", raising=False)
    monkeypatch.setenv("SOME_KEY_SECRET_ARN", SOME_ARN)
    backend = StubSecretsBackend({SOME_ARN: "pem-from-secrets-manager"})

    value = resolve_secret_file("SOME_KEY_PATH", "SOME_KEY_SECRET_ARN", backend=backend)

    assert value == "pem-from-secrets-manager"
