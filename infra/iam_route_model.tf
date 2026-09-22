# RouteModel Lambda's own least-privilege execution role (Principle II). Scope: CloudWatch
# Logs, read access to exactly its own secret (TypeSafe/Jev API key), and read-only access
# to the shared PR-diffs bucket (it reads the diff to classify it — S3Storage.get_text in
# route_model/handler.py). Not requested explicitly in this task's item 1 (only
# retrieve-context's S3 need was named), but added here because the handler code genuinely
# requires it — omitting it would leave this role unable to do its own job.

data "aws_iam_policy_document" "route_model_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "route_model" {
  name               = "${var.project_name}-route-model-role"
  assume_role_policy = data.aws_iam_policy_document.route_model_assume_role.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "route_model_basic_execution" {
  role       = aws_iam_role.route_model.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ARN published by codereview-infra's secrets.tf at /codereview/secrets/typesafe-api-key-arn
# — looked up here instead of hardcoded, so this repo never needs to know the secret's
# actual ARN value ahead of time (mirrors the same SSM-lookup pattern codereview-infra uses
# for Lambda ARNs in lambda_arns.tf).
data "aws_ssm_parameter" "typesafe_api_key_arn" {
  name = "/${var.project_name}/secrets/typesafe-api-key-arn"
}

data "aws_iam_policy_document" "route_model_read_typesafe_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_ssm_parameter.typesafe_api_key_arn.value]
  }
}

# Least privilege: RouteModel can read only its own secret — never invoke-llm's
# gemini-api-key secret (see iam_invoke_llm.tf, which grants the reverse).
resource "aws_iam_role_policy" "route_model_read_typesafe_secret" {
  name   = "${var.project_name}-route-model-read-typesafe-secret"
  role   = aws_iam_role.route_model.id
  policy = data.aws_iam_policy_document.route_model_read_typesafe_secret.json
}

data "aws_iam_policy_document" "route_model_read_diffs" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${data.aws_ssm_parameter.pr_diffs_bucket_name.value}/*"]
  }
}

resource "aws_iam_role_policy" "route_model_read_diffs" {
  name   = "${var.project_name}-route-model-read-diffs"
  role   = aws_iam_role.route_model.id
  policy = data.aws_iam_policy_document.route_model_read_diffs.json
}

resource "aws_lambda_function" "route_model" {
  function_name = "${var.project_name}-route-model"
  role          = aws_iam_role.route_model.arn
  handler       = "route_model.handler.handler"
  runtime       = "python3.14"
  timeout       = 30

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  # TYPESAFE_API_BASE is static, non-secret config: safe as a plain Lambda env var.
  # TYPESAFE_API_KEY and DIFF_BUCKET are deliberately NOT set here — they're resolved at
  # runtime from Secrets Manager / SSM (resolve_api_key / DIFF_BUCKET's SSM lookup), so no
  # secret value or environment-specific bucket name ever lands in Terraform state.
  environment {
    variables = {
      TYPESAFE_API_BASE = "https://api.typesafe.ai"
    }
  }

  tags = local.common_tags
}
