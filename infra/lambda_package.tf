# Shared deployment package for all four Lambdas: `src/` is one importable tree (each
# handler does top-level imports like `from contracts.models import ...`), so every
# function's zip is the same contents — only the `handler` string differs per function
# (see each aws_lambda_function resource). No Lambda Layer: one self-contained zip per
# function, built by combining vendored production dependencies with this repo's own code
# (see build_package.sh / requirements-lambda.txt) — simplest option for 4 small functions.
#
# `null_resource` + `local-exec` runs the build script before `archive_file` zips its
# output; `depends_on` on the data source forces that ordering (a data source normally
# reads during plan, but this one's content only exists after the build step runs — a
# known, standard-enough Terraform idiom for this exact case).
resource "null_resource" "build_lambda_package" {
  triggers = {
    requirements_hash = filesha256("${path.module}/requirements-lambda.txt")
    src_hash          = sha256(join("", [for f in fileset("${path.module}/../src", "**") : filesha256("${path.module}/../src/${f}")]))
  }

  provisioner "local-exec" {
    command     = "bash ${path.module}/build_package.sh"
    interpreter = ["bash", "-c"]
    environment = {
      PYTHON_BIN = var.python_bin
    }
  }
}

data "archive_file" "lambda_src" {
  type        = "zip"
  source_dir  = "${path.module}/.build/package"
  output_path = "${path.module}/.build/lambda_src.zip"

  depends_on = [null_resource.build_lambda_package]
}

# Shared artifacts bucket published by codereview-infra's s3.tf (one bucket, split by prefix:
# prs/ for diff claim checks, index/ for the RAG index). Read here instead of hardcoded —
# used to scope retrieve-context's and invoke-llm's S3 policies. The application code itself
# never resolves this via env var/SSM: each function gets its bucket directly from the
# incoming event (PullRequestEvent.diff_bucket).
data "aws_ssm_parameter" "artifacts_bucket_name" {
  name = "/${var.project_name}/s3/artifacts-bucket-name"
}
