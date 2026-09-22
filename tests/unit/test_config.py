"""Unit tests: non-secret config resolution prefers a local env var, otherwise falls back
to a plain SSM parameter (via a stub backend here) and caches the result at module level.
"""

import integrations.config as config_module
from integrations.config import StubSsmConfigBackend, resolve_config_value


def test_env_var_takes_precedence_over_ssm(monkeypatch):
    monkeypatch.setenv("DIFF_BUCKET", "from-env-bucket")
    backend = StubSsmConfigBackend({"/codereview/s3/pr-diffs-bucket-name": "from-ssm-bucket"})

    value = resolve_config_value(
        "DIFF_BUCKET", "/codereview/s3/pr-diffs-bucket-name", backend=backend
    )

    assert value == "from-env-bucket"


def test_falls_back_to_ssm_when_env_var_absent(monkeypatch):
    monkeypatch.delenv("DIFF_BUCKET", raising=False)
    config_module._config_cache.clear()
    backend = StubSsmConfigBackend({"/codereview/s3/pr-diffs-bucket-name": "from-ssm-bucket"})

    value = resolve_config_value(
        "DIFF_BUCKET", "/codereview/s3/pr-diffs-bucket-name", backend=backend
    )

    assert value == "from-ssm-bucket"


def test_ssm_result_is_cached_across_calls(monkeypatch):
    monkeypatch.delenv("DIFF_BUCKET", raising=False)
    config_module._config_cache.clear()

    class CountingBackend(StubSsmConfigBackend):
        def __init__(self, values):
            super().__init__(values)
            self.fetch_count = 0

        def fetch(self, ssm_param: str) -> str:
            self.fetch_count += 1
            return super().fetch(ssm_param)

    backend = CountingBackend({"/codereview/s3/pr-diffs-bucket-name": "cached-bucket"})

    first = resolve_config_value(
        "DIFF_BUCKET", "/codereview/s3/pr-diffs-bucket-name", backend=backend
    )
    second = resolve_config_value(
        "DIFF_BUCKET", "/codereview/s3/pr-diffs-bucket-name", backend=backend
    )

    assert first == second == "cached-bucket"
    assert backend.fetch_count == 1
