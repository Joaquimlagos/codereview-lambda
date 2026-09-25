# Publishes each Lambda's ARN to the SSM path codereview-infra's lambda_arns.tf actually
# reads (confirmed directly against that repo's current lambda_arns.tf):
#   /${var.project_name}/lambda/<state>/arn
# e.g. /codereview/lambda/route-model/arn — NOT /codereview/{environment}/lambda/{state}-arn;
# that older convention (used by the now-removed scripts/publish_arns.py and reflected in
# some of this feature's spec docs) no longer matches what codereview-infra looks up.
#
# This replaces the manual post-deploy step (scripts/publish_arns.py): Terraform now
# publishes each ARN natively, from the aws_lambda_function resource itself, as part of the
# same apply that creates the function — no separate script run required.
resource "aws_ssm_parameter" "route_model_arn" {
  name  = "/${var.project_name}/lambda/route-model/arn"
  type  = "String"
  value = aws_lambda_function.route_model.arn

  tags = local.common_tags
}

resource "aws_ssm_parameter" "retrieve_context_arn" {
  name  = "/${var.project_name}/lambda/retrieve-context/arn"
  type  = "String"
  value = aws_lambda_function.retrieve_context.arn

  tags = local.common_tags
}

resource "aws_ssm_parameter" "invoke_llm_arn" {
  name  = "/${var.project_name}/lambda/invoke-llm/arn"
  type  = "String"
  value = aws_lambda_function.invoke_llm.arn

  tags = local.common_tags
}

resource "aws_ssm_parameter" "post_comment_arn" {
  name  = "/${var.project_name}/lambda/post-comment/arn"
  type  = "String"
  value = aws_lambda_function.post_comment.arn

  tags = local.common_tags
}
