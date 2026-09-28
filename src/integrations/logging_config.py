"""Raises this project's own loggers to INFO, without touching the root logger or any
third-party library's loggers (boto3, urllib3, etc. stay at their own default level, so
CloudWatch isn't flooded with SDK internals just to see this project's own success-path logs,
e.g. llm_router.py's per-call finish_reason/usage lines).

A logger created with `logging.getLogger(__name__)` starts at NOTSET and defers to its
nearest ancestor with an explicit level when deciding whether to emit a record. Since every
Lambda in this repo is its own top-level package (contracts, integrations, invoke_llm,
route_model, retrieve_context, post_comment — Principle II), there is no single shared
ancestor above all of them except the root logger itself; setting each package name below to
INFO individually is what lets a call like `logging.getLogger("integrations.llm_router")`
resolve to INFO without going anywhere near `logging.getLogger()` (the root) or an unrelated
package such as boto3's own "botocore"/"urllib3" loggers.
"""

import logging

# One entry per top-level package in this repo that may log. Not just "integrations" (the
# only one that actually logs anything today, in llm_router.py) — every Lambda handler
# package is included too, so a handler that adds its own logging later doesn't end up
# silently suppressed the same way this bug did.
_PROJECT_LOGGER_NAMES = (
    "integrations",
    "invoke_llm",
    "route_model",
    "retrieve_context",
    "post_comment",
)


def configure_project_logging(level: int = logging.INFO) -> None:
    """Call once per Lambda cold start, from each handler.py's module scope. Idempotent —
    cheap to call again on a warm invocation."""
    for name in _PROJECT_LOGGER_NAMES:
        logging.getLogger(name).setLevel(level)
