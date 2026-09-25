# RouteModel Lambda's own least-privilege execution role (Principle II). Scope: CloudWatch
# Logs plus read access to exactly its own secret (TypeSafe/Jev API key) — nothing else.
# RouteModel decides complexity/needsContext from lightweight event metadata alone
# (files_changed, lines_added, lines_removed, paths — see route_model/handler.py); it never
# reads the diff body and never touches S3, so no S3 permission is granted here.

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

resource "aws_lambda_function" "route_model" {
  function_name = "${var.project_name}-route-model"
  role          = aws_iam_role.route_model.arn
  handler       = "route_model.handler.handler"
  runtime       = "python3.14"
  timeout       = 30
  # 256 MB: at 128 MB a cold start alone used ~104 MB (boto3 + pydantic), and Lambda
  # scales CPU with memory, so 128 MB also made cold starts slow.
  memory_size = 256

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  # TYPESAFE_API_BASE is static, non-secret config: safe as a plain Lambda env var.
  # TYPESAFE_API_KEY is deliberately NOT set here: no secret value ever lands in Terraform state.
  # TYPESAFE_API_KEY_SECRET_ARN is only the secret's ARN (an identifier, not the value), taken from the
  # same SSM-published data source this role's GetSecretValue policy is scoped to; the
  # function passes it straight to Secrets Manager (integrations/secrets.py), so the role
  # needs no ssm:GetParameter at all.
  environment {
    variables = {
      TYPESAFE_API_BASE           = "https://api.typesafe.ai"
      TYPESAFE_API_KEY_SECRET_ARN = data.aws_ssm_parameter.typesafe_api_key_arn.value
    }
  }

  tags = local.common_tags
}
