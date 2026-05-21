resource "aws_cloudwatch_event_rule" "daily_cron" {
  name                = "${var.project_name}-daily-cron"
  description         = "Daily cron for recurring bookings and pace alerts at 06:00 UTC"
  schedule_expression = "cron(0 6 * * ? *)"

  tags = {
    Project = var.project_name
  }
}

resource "aws_cloudwatch_event_target" "daily_cron_lambda" {
  rule      = aws_cloudwatch_event_rule.daily_cron.name
  target_id = "${var.project_name}-daily-cron-lambda"
  arn       = aws_lambda_function.webhook.arn
}

resource "aws_lambda_permission" "events_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.webhook.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_cron.arn
}
