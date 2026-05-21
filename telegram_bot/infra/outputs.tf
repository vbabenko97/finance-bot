output "api_gateway_url" {
  description = "API Gateway endpoint URL"
  value       = aws_apigatewayv2_api.webhook.api_endpoint
}

output "lambda_function_name" {
  description = "Lambda function name"
  value       = aws_lambda_function.webhook.function_name
}

output "webhook_url" {
  description = "Full webhook URL for Telegram setWebhook"
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/webhook"
}

output "dynamodb_table_name" {
  description = "DynamoDB table name"
  value       = aws_dynamodb_table.finance_bot.name
}

output "allowed_user_id" {
  description = "Allowed Telegram user ID"
  value       = var.allowed_user_id
}

output "telegram_bot_token_parameter_name" {
  description = "SSM parameter name for the Telegram bot token"
  value       = local.telegram_bot_token_parameter_name
}

output "webhook_secret_parameter_name" {
  description = "SSM parameter name for the webhook secret"
  value       = local.webhook_secret_parameter_name
}

output "sns_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarm notifications"
  value       = aws_sns_topic.alerts.arn
}
