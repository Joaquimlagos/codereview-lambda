"""Unit tests: API-key resolution prefers a local env var, otherwise falls back to Secrets
Manager (via a stub backend here) and caches the result at module level.
"""

import integrations.secrets as secrets_module
from integrations.secrets import StubSecretsBackend, resolve_api_key


def test_env_var_takes_precedence_over_secrets_manager(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "from-env")
    backend = StubSecretsBackend({"/codereview/secrets/some-arn": "from-secrets-manager"})

    value = resolve_api_key("SOME_API_KEY", "/codereview/secrets/some-arn", backend=backend)

    assert value == "from-env"


def test_falls_back_to_secrets_manager_when_env_var_absent(monkeypatch):
    monkeypatch.delenv("OTHER_API_KEY", raising=False)
    secrets_module._cache.clear()
    backend = StubSecretsBackend({"/codereview/secrets/other-arn": "from-secrets-manager"})

    value = resolve_api_key("OTHER_API_KEY", "/codereview/secrets/other-arn", backend=backend)

    assert value == "from-secrets-manager"


def test_secrets_manager_result_is_cached_across_calls(monkeypatch):
    monkeypatch.delenv("CACHED_API_KEY", raising=False)
    secrets_module._cache.clear()

    class CountingBackend(StubSecretsBackend):
        def __init__(self, values):
            super().__init__(values)
            self.fetch_count = 0

        def fetch(self, ssm_arn_param: str) -> str:
            self.fetch_count += 1
            return super().fetch(ssm_arn_param)

    backend = CountingBackend({"/codereview/secrets/cached-arn": "cached-value"})

    first = resolve_api_key("CACHED_API_KEY", "/codereview/secrets/cached-arn", backend=backend)
    second = resolve_api_key("CACHED_API_KEY", "/codereview/secrets/cached-arn", backend=backend)

    assert first == second == "cached-value"
    assert backend.fetch_count == 1
