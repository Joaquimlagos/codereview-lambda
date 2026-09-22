variable "project_name" {
  description = "Prefix used in resource names, matching codereview-infra's convention."
  type        = string
  default     = "codereview"
}

variable "aws_region" {
  description = "AWS region every resource in this repo's Terraform is deployed to."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment name. Used for tagging and to resolve the SSM parameter path codereview-infra publishes each secret's ARN under."
  type        = string
  default     = "local"
}

variable "python_bin" {
  description = "Python interpreter used by build_package.sh to pip-install Lambda dependencies. Default assumes a standard `python3` on PATH (true on typical CI/Linux/Mac); override if your local interpreter is named differently (e.g. a Windows dev machine without a `python3` alias)."
  type        = string
  default     = "python3"
}
