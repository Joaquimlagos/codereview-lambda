# RetrieveContext Lambda's own least-privilege execution role (Principle II). Scope:
# CloudWatch Logs, read-only access to the two artifact prefixes it consumes (the PR diff
# under prs/ and the RAG index under index/), and read access to exactly its own copy of the
# Gemini secret — it needs that key to embed the diff as a retrieval query
# (retrieve_context/handler.py). It writes nothing: retrieved chunks are returned inline to
# InvokeLLM, so no s3:PutObject is granted.

data "aws_iam_policy_document" "retrieve_context_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "retrieve_context" {
  name               = "${var.project_name}-retrieve-context-role"
  assume_role_policy = data.aws_iam_policy_document.retrieve_context_assume_role.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "retrieve_context_basic_execution" {
  role       = aws_iam_role.retrieve_context.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Both prefixes are named explicitly rather than granting the whole bucket: this function
# reads the diff (prs/{pr}/{sha}.diff) and the index (index/develop/index.json), and nothing
# else in the artifacts bucket.
data "aws_iam_policy_document" "retrieve_context_read_artifacts" {
  statement {
    actions = ["s3:GetObject"]
    resources = [
      "arn:aws:s3:::${data.aws_ssm_parameter.artifacts_bucket_name.value}/prs/*",
      "arn:aws:s3:::${data.aws_ssm_parameter.artifacts_bucket_name.value}/index/*",
    ]
  }
}

resource "aws_iam_role_policy" "retrieve_context_read_artifacts" {
  name   = "${var.project_name}-retrieve-context-read-artifacts"
  role   = aws_iam_role.retrieve_context.id
  policy = data.aws_iam_policy_document.retrieve_context_read_artifacts.json
}

# Same Gemini secret InvokeLLM reads (the ARN data source is declared in iam_invoke_llm.tf):
# RetrieveContext embeds the diff, InvokeLLM generates the review, both against Gemini.
# Still least privilege — neither can read route-model's TypeSafe key or post-comment's
# GitHub token.
data "aws_iam_policy_document" "retrieve_context_read_gemini_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_ssm_parameter.gemini_api_key_arn.value]
  }
}

resource "aws_iam_role_policy" "retrieve_context_read_gemini_secret" {
  name   = "${var.project_name}-retrieve-context-read-gemini-secret"
  role   = aws_iam_role.retrieve_context.id
  policy = data.aws_iam_policy_document.retrieve_context_read_gemini_secret.json
}

resource "aws_lambda_function" "retrieve_context" {
  function_name = "${var.project_name}-retrieve-context"
  role          = aws_iam_role.retrieve_context.arn
  handler       = "retrieve_context.handler.handler"
  runtime       = "python3.14"
  # Downloads and parses the whole index, then makes an embedding API call — more work than
  # the 30s the other non-LLM stages get.
  timeout = 60
  # 256 MB: at 128 MB a cold start alone used ~104 MB (boto3 + pydantic), and Lambda
  # scales CPU with memory, so 128 MB also made cold starts slow.
  memory_size = 256

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  # GEMINI_API_BASE is static, non-secret config (the embedding call needs it). No bucket env
  # var: it rides on the incoming event (PullRequestEvent.diff_bucket).
  # GEMINI_API_KEY is deliberately NOT set here: no secret value ever lands in Terraform
  # state. GEMINI_API_KEY_SECRET_ARN is only the secret's ARN (an identifier, not the
  # value), taken from the same SSM-published data source this role's GetSecretValue
  # policy is scoped to; the function passes it straight to Secrets Manager
  # (integrations/secrets.py), so the role needs no ssm:GetParameter at all.
  environment {
    variables = {
      GEMINI_API_BASE           = "https://generativelanguage.googleapis.com/v1beta"
      GEMINI_API_KEY_SECRET_ARN = data.aws_ssm_parameter.gemini_api_key_arn.value
    }
  }

  tags = local.common_tags
}
