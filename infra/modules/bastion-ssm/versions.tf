terraform {
  # Modules declare which providers they use, but never configure them
  # (no provider block, no region, no backend) — the environment does that.
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}
