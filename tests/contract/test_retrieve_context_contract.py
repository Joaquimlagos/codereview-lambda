"""Contract test for RetrieveContext: output MUST validate against RetrievedContext (FR-012)."""

from contracts.models import RetrievedContext
from integrations.storage import StubStorage
from retrieve_context.handler import retrieve_context


def test_retrieve_context_output_matches_contract(pr_event, stub_storage):
    output = retrieve_context(pr_event, storage=stub_storage)

    validated = RetrievedContext.model_validate(output)
    assert validated.pr_id == pr_event["pr_id"]
    assert validated.sources  # small_diff fixture touches one file


def test_retrieve_context_allows_empty_sources(pr_event):
    diff_without_recognizable_header = "no diff --git header here, just plain text\n"
    storage = StubStorage(initial={pr_event["diff_ref"]: diff_without_recognizable_header})

    output = retrieve_context(pr_event, storage=storage)

    validated = RetrievedContext.model_validate(output)
    assert validated.sources == []
