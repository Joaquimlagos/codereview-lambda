# InvokeLLM Lambda's own least-privilege execution role (Principle II). Scope: CloudWatch
# Logs, read access to exactly its own two secrets (the Gemini and Groq API keys, used to call
# each provider's API directly — no separate routing service), and read-only access to the
# artifacts bucket's prs/ prefix, where the diff it reviews lives (S3Storage.get_text in
# invoke_llm/handler.py). It does not read index/: retrieved context arrives inline from
# RetrieveContext, so this role never needs the RAG index.

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

# Same pattern for the Groq API key: its ARN is published by codereview-infra's secrets.tf at
# /codereview/secrets/groq-api-key-arn, and the policy covers exactly that one secret.
data "aws_ssm_parameter" "groq_api_key_arn" {
  name = "/${var.project_name}/secrets/groq-api-key-arn"
}

data "aws_iam_policy_document" "invoke_llm_read_groq_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_ssm_parameter.groq_api_key_arn.value]
  }
}

resource "aws_iam_role_policy" "invoke_llm_read_groq_secret" {
  name   = "${var.project_name}-invoke-llm-read-groq-secret"
  role   = aws_iam_role.invoke_llm.id
  policy = data.aws_iam_policy_document.invoke_llm_read_groq_secret.json
}

data "aws_iam_policy_document" "invoke_llm_read_diffs" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${data.aws_ssm_parameter.artifacts_bucket_name.value}/prs/*"]
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
  # 150s: each model attempt allows 5s to connect + 45s to read (llm_router.py), so three
  # attempts that all run to their limit take ~150s. The router also checks the remaining
  # time before every attempt and stops with LlmTransientError when less than one full
  # attempt (50s) is left, so a longer list, or cold start and the secret fetch eating into
  # the budget, ends in an error Step Functions retries rather than a Lambda timeout.
  timeout = 150
  # 256 MB: at 128 MB a cold start alone used ~104 MB (boto3 + pydantic), and Lambda
  # scales CPU with memory, so 128 MB also made cold starts slow.
  memory_size = 256

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  # The API bases and the three per-tier model lists are static, non-secret config: safe as
  # plain Lambda env vars. Each LLM_MODELS_* is a comma-separated fallback list of
  # provider:model[:reasoning] entries, in order of preference; every tier mixes both
  # providers (research.md, "Multi-provider model fallback", has the measurements).
  # The API keys are deliberately NOT set here: no secret value ever lands in Terraform
  # state. The *_SECRET_ARN values are only the secrets' ARNs (identifiers, not values),
  # taken from the same SSM-published data sources this role's GetSecretValue policies are
  # scoped to; the function passes them straight to Secrets Manager
  # (integrations/secrets.py), so the role needs no ssm:GetParameter at all.
  environment {
    variables = {
      GEMINI_API_BASE           = "https://generativelanguage.googleapis.com/v1beta"
      GEMINI_API_KEY_SECRET_ARN = data.aws_ssm_parameter.gemini_api_key_arn.value
      GROQ_API_BASE             = "https://api.groq.com/openai/v1"
      GROQ_API_KEY_SECRET_ARN   = data.aws_ssm_parameter.groq_api_key_arn.value
      LLM_MODELS_LOW            = "groq:openai/gpt-oss-120b:low,gemini:gemini-3.5-flash:low"
      LLM_MODELS_MEDIUM         = "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:low"
      LLM_MODELS_HIGH           = "groq:openai/gpt-oss-120b:medium,gemini:gemini-3.5-flash:low"
    }
  }

  tags = local.common_tags
}
