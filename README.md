# codereview-lambda
Harness serverless que roteia PRs entre modelos LLM (Gemini) conforme complexidade, usa RAG para contexto do projeto e comenta a análise automaticamente no PR.

## Known limitations (current stage)

- **`RetrieveContext` is a stub, not real RAG.** It does not query a vector index or any
  external knowledge source. It only regex-parses the `diff --git a/X b/Y` header lines already
  present in the diff text to list touched file paths, then stores that file list plus the same
  diff text back as "context". No project file content beyond the diff itself is ever read. See
  the docstring in `src/retrieve_context/handler.py` and `specs/001-pr-review-pipeline/research.md`'s
  "simple text/file-relevance based strategy" decision.
