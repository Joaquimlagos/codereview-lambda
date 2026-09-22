# PostComment Lambda's own least-privilege execution role (Principle II). Scope: CloudWatch
# Logs plus read access to exactly its own secret (GitHub token, used by RestGitHubClient to
# post the review comment). No S3 access: this function only reads `invokeLlm`/`analysis`
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

# ARN published by codereview-infra's secrets.tf at /codereview/secrets/github-token-arn —
# looked up here instead of hardcoded, mirroring the same pattern as the other two secrets
# (iam_route_model.tf, iam_invoke_llm.tf).
data "aws_ssm_parameter" "github_token_arn" {
  name = "/${var.project_name}/secrets/github-token-arn"
}

data "aws_iam_policy_document" "post_comment_read_github_token" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_ssm_parameter.github_token_arn.value]
  }
}

# Least privilege: PostComment can read only its own secret — never route-model's or
# invoke-llm's secrets (see iam_route_model.tf / iam_invoke_llm.tf, which grant the reverse).
resource "aws_iam_role_policy" "post_comment_read_github_token" {
  name   = "${var.project_name}-post-comment-read-github-token"
  role   = aws_iam_role.post_comment.id
  policy = data.aws_iam_policy_document.post_comment_read_github_token.json
}

resource "aws_lambda_function" "post_comment" {
  function_name = "${var.project_name}-post-comment"
  role          = aws_iam_role.post_comment.arn
  handler       = "post_comment.handler.handler"
  runtime       = "python3.14"
  timeout       = 30

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  # GITHUB_TOKEN is deliberately NOT set here — resolved at runtime from Secrets Manager, so
  # no secret value ever lands in Terraform state.

  tags = local.common_tags
}
