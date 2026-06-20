terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # For real use, store state remotely (encrypted, locked) instead of locally.
  # Uncomment and fill in once you have an S3 bucket + DynamoDB lock table:
  #
  # backend "s3" {
  #   bucket         = "my-tfstate-bucket"
  #   key            = "nama/rds.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "my-tfstate-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region
}
