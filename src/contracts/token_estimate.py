"""A deliberately pessimistic token estimate, shared by RetrieveContext and InvokeLLM.

Two decisions need a token count without calling any tokenizer: whether a diff query fits
the embedding model's input limit (RetrieveContext), and how much retrieved context fits a
provider's prompt budget (InvokeLLM). A real tokenizer would be the wrong one for most of
those uses — gpt-oss (Groq, Cerebras), Gemini and the embedding model each count
differently — and would add a dependency to the deployment package for every Lambda.

So the count is estimated from length alone, at a ratio chosen to *over*-count for every
tokenizer involved. Measured on codereview-app's diffs (specs/002-method-chunking/
baseline.md): 4.12-4.36 characters per token on gpt-oss's tokenizer, about 3.4 on Gemini's.
3.0 sits below all of them, so an estimate never under-counts on this code; it over-counts
gpt-oss prompts by ~30%, the price of one shared, safe number.
"""

import math

CHARS_PER_TOKEN = 3.0

# gemini-embedding-001 silently drops input beyond 2,048 tokens. Anything estimated above
# this is split before embedding: at 3.0 chars/token the worst real size that still passes
# is ~1,590 embedding tokens, comfortably inside the limit. codereview-app's index builder
# uses the same threshold, so both sides agree on what "too big" means.
EMBEDDING_SPLIT_THRESHOLD_TOKENS = 1800


def estimate_tokens(text: str) -> int:
    """Estimated token count of `text` — never lower than any provider's real count here."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)
