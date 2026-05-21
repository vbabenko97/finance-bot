terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

provider "aws" {
  region = var.aws_region
}
