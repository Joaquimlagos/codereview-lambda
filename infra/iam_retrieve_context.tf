# RetrieveContext Lambda's own least-privilege execution role (Principle II). Scope:
# CloudWatch Logs plus read/write access to the shared PR-diffs bucket — it reads the diff
# and writes the assembled context text back to the same bucket (S3Storage.get_text/
# put_text in retrieve_context/handler.py). No Secrets Manager access: this function calls
# no external API requiring a credential.

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

data "aws_iam_policy_document" "retrieve_context_read_write_diffs" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["arn:aws:s3:::${data.aws_ssm_parameter.pr_diffs_bucket_name.value}/*"]
  }
}

resource "aws_iam_role_policy" "retrieve_context_read_write_diffs" {
  name   = "${var.project_name}-retrieve-context-read-write-diffs"
  role   = aws_iam_role.retrieve_context.id
  policy = data.aws_iam_policy_document.retrieve_context_read_write_diffs.json
}

resource "aws_lambda_function" "retrieve_context" {
  function_name = "${var.project_name}-retrieve-context"
  role          = aws_iam_role.retrieve_context.arn
  handler       = "retrieve_context.handler.handler"
  runtime       = "python3.14"
  timeout       = 30

  filename         = data.archive_file.lambda_src.output_path
  source_code_hash = data.archive_file.lambda_src.output_base64sha256

  # DIFF_BUCKET is deliberately NOT set here — resolved at runtime via SSM, so no
  # environment-specific bucket name lands in Terraform state.

  tags = local.common_tags
}
