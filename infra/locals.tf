# Tags applied to every taggable AWS resource in this repo. Centralized here instead of
# repeating the same map on each resource.
locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# Function names, defined once: each aws_lambda_function's name and its log group's name
# (`/aws/lambda/<function name>`) both derive from here, so they cannot drift apart. The log
# group cannot be named from the function resource itself: the function depends on its log
# group, which would be a dependency cycle.
locals {
  function_names = {
    route_model      = "${var.project_name}-route-model"
    retrieve_context = "${var.project_name}-retrieve-context"
    invoke_llm       = "${var.project_name}-invoke-llm"
    post_comment     = "${var.project_name}-post-comment"
  }

  # How long CloudWatch keeps each Lambda's logs. Without a retention policy they are kept
  # forever, and cost accrues for as long as they exist.
  log_retention_days = 7
}
