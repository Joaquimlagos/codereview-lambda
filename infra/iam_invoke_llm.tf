# InvokeLLM Lambda's own least-privilege execution role (Principle II). Scope: CloudWatch
# Logs, read access to exactly its own secret (Gemini API key, used to call the Gemini API
# directly per Principle III — no separate routing service), and read-only access to the
# shared PR-diffs bucket (it reads the diff, and the retrieved-context text when present —
# S3Storage.get_text in invoke_llm/handler.py). Not requested explicitly in this task's
# item 1 (only retrieve-context's S3 need was named), but added here because the handler
# code genuinely requires it.

data "aws_iam_policy_document" "invoke_llm_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "invoke_llm" {
  name               = "${var.project_name}-invoke-llm-role"
  assume_role_policy = data.aws_iam_policy_document.invoke_llm_assume_role.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "invoke_llm_basic_execution" {
  role       = aws_iam_role.invoke_llm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ARN published by codereview-infra's secrets.tf at /codereview/secrets/gemini-api-key-arn
# — looked up here instead of hardcoded, so this repo never needs to know the secret's
# actual ARN value ahead of time (mirrors the same SSM-lookup pattern codereview-infra uses
# for Lambda ARNs in lambda_arns.tf).
data "aws_ssm_parameter" "gemini_api_key_arn" {
  name = "/${var.project_name}/secrets/gemini-api-key-arn"
}

data "aws_iam_policy_document" "invoke_llm_read_gemini_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_ssm_parameter.gemini_api_key_arn.value]
  }
}

# Least privilege: InvokeLLM can read only its own secret — never route-model's
# typesafe-api-key secret (see iam_route_model.tf, which grants the reverse).
resource "aws_iam_role_policy" "invoke_llm_read_gemini_secret" {
  name   = "${var.project_name}-invoke-llm-read-gemini-secret"
  role   = aws_iam_role.invoke_llm.id
  policy = data.aws_iam_policy_document.invoke_llm_read_gemini_secret.json
}

data "aws_iam_policy_document" "invoke_llm_read_diffs" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${data.aws_ssm_parameter.pr_diffs_bucket_name.value}/*"]
  }
}

resource "aws_iam_role_policy" "invoke_llm_read_diffs" {
  name   = "${var.project_name}-invoke-llm-read-diffs"
  role   = aws_iam_role.invoke_llm.id
  policy = data.aws_iam_policy_document.invoke_llm_read_diffs.json
}

resource "aws_lambda_function" "invoke_llm" {
  function_name = "${var.project_name}-invoke-llm"
  role          = aws_iam_role.invoke_llm.arn
  handler       = "invoke_llm.handler.handler"
  runtime       = "python3.14"
  timeout       = 60 # Gemini generateContent calls run longer than the other three stages.

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  # GEMINI_API_BASE and the three model names are static, non-secret config: safe as plain
  # Lambda env vars. GEMINI_API_KEY and DIFF_BUCKET are deliberately NOT set here — resolved
  # at runtime from Secrets Manager / SSM, so no secret value or environment-specific bucket
  # name ever lands in Terraform state.
  environment {
    variables = {
      GEMINI_API_BASE     = "https://generativelanguage.googleapis.com/v1beta"
      GEMINI_MODEL_LOW    = "gemini-2.5-flash-lite"
      GEMINI_MODEL_MEDIUM = "gemini-2.5-flash"
      GEMINI_MODEL_HIGH   = "gemini-2.5-pro"
    }
  }

  tags = local.common_tags
}
