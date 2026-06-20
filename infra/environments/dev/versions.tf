terraform {
  required_version = ">= 1.10" # use_lockfile (native S3 state locking) needs >= 1.10

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # Remote state in S3. One state file per environment (key below). The bucket is
  # globally unique and account-specific, so it's supplied at init:
  #   terraform init -backend-config="bucket=YOUR_STATE_BUCKET"
  backend "s3" {
    key          = "dev/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # native S3 locking — no DynamoDB table required
  }
}

provider "aws" {
  region = var.region

  # Every resource in this environment is tagged automatically — no per-resource
  # tagging needed.
  default_tags {
    tags = {
      Project     = "nama"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
