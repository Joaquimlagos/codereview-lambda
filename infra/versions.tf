terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 6.x required: the ~> 5.0 line's aws_lambda_function runtime validation predates
      # python3.14 and rejects it outright (confirmed by running `terraform validate`).
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}
