locals {
  telegram_bot_token_parameter_name = "/${var.project_name}/telegram_bot_token"
  webhook_secret_parameter_name     = "/${var.project_name}/webhook_secret_token"
}

resource "aws_lambda_function" "webhook" {
  function_name    = "${var.project_name}-webhook"
  runtime          = "python3.11"
  handler          = "telegram_bot.handler.lambda_handler"
  filename         = "${path.module}/lambda.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda.zip")
  role             = aws_iam_role.lambda_role.arn
  memory_size      = 256
  timeout          = 10

  environment {
    variables = {
      DYNAMODB_TABLE_NAME        = aws_dynamodb_table.finance_bot.name
      ALLOWED_USER_ID            = var.allowed_user_id
      TELEGRAM_BOT_TOKEN_PARAM   = local.telegram_bot_token_parameter_name
      WEBHOOK_SECRET_TOKEN_PARAM = local.webhook_secret_parameter_name
    }
  }

  tags = {
    Project = var.project_name
  }
}

resource "aws_cloudwatch_log_group" "webhook" {
  name              = "/aws/lambda/${aws_lambda_function.webhook.function_name}"
  retention_in_days = var.log_retention_days

  tags = {
    Project = var.project_name
  }
}
