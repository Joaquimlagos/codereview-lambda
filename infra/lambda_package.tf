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

# Bucket name published by codereview-infra's s3.tf, read here instead of hardcoded —
# used by route-model, retrieve-context, and invoke-llm (all three read/write PR diffs or
# retrieved-context text via the same bucket, per DIFF_BUCKET in the application code).
data "aws_ssm_parameter" "pr_diffs_bucket_name" {
  name = "/${var.project_name}/s3/pr-diffs-bucket-name"
}
