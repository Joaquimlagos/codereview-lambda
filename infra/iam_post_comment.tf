# PostComment Lambda's own least-privilege execution role (Principle II). Scope: CloudWatch
# Logs plus read access to exactly its own secret (the GitHub App's private key, which
# GitHubAppAuth uses to mint an installation token for posting the review). No S3 access: this function only reads `invokeLlm`/`analysis`
# from the Step Functions event, never touches the diffs bucket.

data "aws_iam_policy_document" "post_comment_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "post_comment" {
  name               = "${var.project_name}-post-comment-role"
  assume_role_policy = data.aws_iam_policy_document.post_comment_assume_role.json

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "post_comment_basic_execution" {
  role       = aws_iam_role.post_comment.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ARN published by codereview-infra's secrets.tf at
# /codereview/secrets/github-app-private-key-arn — looked up here instead of hardcoded,
# mirroring the same pattern as the other secrets (iam_route_model.tf, iam_invoke_llm.tf).
# The older github-token secret still exists in codereview-infra, but this function no longer
# uses it and its role is not granted access to it.
data "aws_ssm_parameter" "github_app_private_key_arn" {
  name = "/${var.project_name}/secrets/github-app-private-key-arn"
}

data "aws_iam_policy_document" "post_comment_read_github_app_private_key" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_ssm_parameter.github_app_private_key_arn.value]
  }
}

# Least privilege: PostComment can read only its own secret — never route-model's or
# invoke-llm's secrets (see iam_route_model.tf / iam_invoke_llm.tf, which grant the reverse).
resource "aws_iam_role_policy" "post_comment_read_github_app_private_key" {
  name   = "${var.project_name}-post-comment-read-github-app-private-key"
  role   = aws_iam_role.post_comment.id
  policy = data.aws_iam_policy_document.post_comment_read_github_app_private_key.json
}

# The function's log group. Lambda would otherwise create it on first invocation, outside
# Terraform and with no expiry; declaring it here lets Terraform set the retention. Named from
# the function's name (locals.tf) and depended on by the function below, so on a fresh
# deployment the group exists, with its retention, before the function can log to it.
resource "aws_cloudwatch_log_group" "post_comment" {
  name              = "/aws/lambda/${local.function_names.post_comment}"
  retention_in_days = local.log_retention_days

  tags = local.common_tags
}

resource "aws_lambda_function" "post_comment" {
  function_name = local.function_names.post_comment
  role          = aws_iam_role.post_comment.arn
  handler       = "post_comment.handler.handler"
  runtime       = "python3.14"
  timeout       = 30
  # 256 MB: at 128 MB a cold start alone used ~104 MB (boto3 + pydantic), and Lambda
  # scales CPU with memory, so 128 MB also made cold starts slow.
  memory_size = 256

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  # GITHUB_APP_ID and GITHUB_APP_INSTALLATION_ID are identifiers, not secrets (values in
  # terraform.tfvars). The App's private key is deliberately NOT set here: no secret value
  # ever lands in Terraform state. GITHUB_APP_PRIVATE_KEY_SECRET_ARN is only the secret's ARN,
  # taken from the same SSM-published data source this role's GetSecretValue policy is
  # scoped to; the function passes it straight to Secrets Manager (integrations/secrets.py),
  # so the role needs no ssm:GetParameter at all.
  environment {
    variables = {
      GITHUB_APP_ID                     = var.github_app_id
      GITHUB_APP_INSTALLATION_ID        = var.github_app_installation_id
      GITHUB_APP_PRIVATE_KEY_SECRET_ARN = data.aws_ssm_parameter.github_app_private_key_arn.value
    }
  }

  depends_on = [aws_cloudwatch_log_group.post_comment]

  tags = local.common_tags
}
