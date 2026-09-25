# Tags applied to every taggable AWS resource in this repo. Centralized here instead of
# repeating the same map on each resource.
locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
