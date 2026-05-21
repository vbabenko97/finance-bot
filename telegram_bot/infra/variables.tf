variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Project name prefix for resources"
  type        = string
  default     = "finance-bot"
}

variable "allowed_user_id" {
  description = "Telegram user ID allowed to use the bot"
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the Lambda log group"
  type        = number
  default     = 14
}
