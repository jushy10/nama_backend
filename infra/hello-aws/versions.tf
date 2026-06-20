terraform {
  required_version = ">= 1.10" # use_lockfile (native S3 state locking) needs >= 1.10

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # Remote state in S3 so deploys are repeatable and safe from both CI and your
  # laptop. The bucket name is globally unique and account-specific, so it isn't
  # committed here — supply it at init:
  #   terraform init -backend-config="bucket=YOUR_STATE_BUCKET"
  backend "s3" {
    key          = "hello-aws/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # native S3 locking — no DynamoDB table required
  }
}

provider "aws" {
  region = var.region
}
