# Contract: `index/develop/index.json`, version 2

**Owner**: codereview-app `scripts/build_index.py` · **Consumer**: codereview-lambda
RetrieveContext · **Location**: unchanged (`s3://<artifacts bucket>/index/develop/index.json`)

```json
{
  "version": 2,
  "branch": "develop",
  "commit": "<40-char sha>",
  "generatedAt": "<ISO 8601 UTC>",
  "model": "gemini-embedding-001",
  "dimensions": 768,
  "chunks": [
    {
      "id": "src/main/java/com/codereview/app/auth/JwtValidator.java#JwtValidator.isValid(String):44-46",
      "path": "src/main/java/com/codereview/app/auth/JwtValidator.java",
      "kind": "method",
      "symbol": {"type": "JwtValidator", "method": "isValid"},
      "startLine": 44,
      "endLine": 46,
      "part": null,
      "header": "package com.codereview.app.auth;\n\n@Component\npublic class JwtValidator {\n    private final SecretKey signingKey;\n    private final Duration tokenTtl;",
      "text": "public boolean isValid(String token) {\n    return parseClaims(token).isPresent();\n}",
      "vector": [0.0]
    }
  ]
}
```

(The line numbers and header in the example are illustrative.)

## Compatibility rules

1. **v1 field names survive.** `path`, `text` and `vector` keep their v1 meaning, so a
   v1-only reader, which is today's deployed RetrieveContext, ranks v2 chunks without
   error. That covers a reversed deploy order (spec FR-029).
2. **Readers dispatch on `version`.** 1 → v1 path (unchanged); 2 → v2 path; anything else →
   `IndexCompatibilityError` (hard failure).
3. `model` and `dimensions` checks are unchanged for both versions.
4. **Writer guarantees**: unique `id`s; no embedded input above ~1,800 estimated tokens;
   no empty `text`; the index is written only after every embedding succeeded.

## Rollback

S3 versioning is enabled on the bucket. Restoring the previous object version returns the
pipeline to v1 immediately, without a Lambda deploy:
```sh
aws s3api list-object-versions --bucket codereview-artifacts --prefix index/develop/index.json
aws s3api copy-object --bucket codereview-artifacts --key index/develop/index.json \
  --copy-source "codereview-artifacts/index/develop/index.json?versionId=<previous v1 id>"
```
A push to `develop` rebuilds the index again, so a lasting rollback also needs the app
change reverted on `develop`.
