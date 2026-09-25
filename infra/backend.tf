# Remote state in the shared artifacts bucket codereview-infra provisions, under the same
# terraform-state/ prefix as codereview-infra's own state but a key of its own, so the two
# configurations never read or overwrite each other's state. That prefix is outside the
# bucket's only lifecycle rule (prs/), and bucket versioning keeps prior state versions
# recoverable.
#
# Backend blocks can't reference variables, so bucket/key/region are literal here.
#
# Locking uses S3-native lockfiles (use_lockfile, Terraform >= 1.10 — see versions.tf): a
# .tflock object is written next to the state key for the duration of each operation, so a
# concurrent plan/apply fails fast instead of silently overwriting state. No DynamoDB table.
terraform {
  backend "s3" {
    bucket       = "codereview-artifacts"
    key          = "terraform-state/codereview-lambda.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
