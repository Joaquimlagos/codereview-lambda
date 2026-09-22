# Standard AWS provider — this repo now deploys directly to real AWS, not LocalStack.
provider "aws" {
  region = var.aws_region
}
