"""Configuration resolution: required env-var reads (FR-009).

The Lambda ARN registry itself is now published natively by Terraform
(infra/arn_publish.tf) — this module no longer publishes ARNs; see git history for the
removed ArnPublisher/SsmArnPublisher/StubArnPublisher/arn_ssm_path, superseded by that
Terraform resource plus codereview-infra's lambda_arns.tf, which reads it directly.

The non-secret SSM-config resolver that used to live here (`resolve_config_value` +
`SsmConfigBackend`/`AwsSsmConfigBackend`/`StubSsmConfigBackend`) was removed too: it only
ever had one caller, DIFF_BUCKET, and RetrieveContext/InvokeLLM now get their bucket from
the event itself (`pr_event.diff_bucket`) instead of from Lambda-level config — see git
history and specs/001-pr-review-pipeline/research.md's "Non-secret config resolution"
decision, which is now stale.
"""

import os


class ConfigError(Exception):
    """Raised when required configuration is missing."""


def require_env(name: str) -> str:
    """Read a required environment variable; every config value comes only from here (FR-009)."""
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value
