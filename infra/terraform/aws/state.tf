data "aws_caller_identity" "current" {}

data "external" "config_validation" {
  program = [
    "bash", "-c",
    "uv run --project \"$1\" bed validate \"$2\" >&2 && printf '{\"valid\":\"true\"}'",
    "validate", abspath("${path.module}/../../.."), abspath(var.config_path),
  ]
}

locals {
  cfg         = yamldecode(file(var.config_path))
  account_id  = data.aws_caller_identity.current.account_id
  runtime_key = replace(var.name, "-", "_")
  config_key  = "config.yaml"

  connector_secrets = distinct(flatten([
    for kind in ["vcs", "tracker", "notifier"] : [
      for name, conn in try(local.cfg.connectors[kind], {}) : conn.auth_secret
    ]
  ]))
}

resource "aws_s3_bucket" "config" {
  bucket_prefix = "${var.name}-config-"
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "config" {
  bucket = aws_s3_bucket.config.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config" {
  bucket = aws_s3_bucket.config.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "config" {
  bucket       = aws_s3_bucket.config.id
  key          = local.config_key
  content      = file(var.config_path)
  content_type = "application/yaml"
  etag         = filemd5(var.config_path)

  lifecycle {
    precondition {
      condition     = data.external.config_validation.result.valid == "true"
      error_message = "Config failed validation."
    }
  }
}

resource "aws_dynamodb_table" "state" {
  name         = var.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_sqs_queue" "results_dlq" {
  name                      = "${var.name}-results-dlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_cloudwatch_metric_alarm" "results_dlq" {
  alarm_name          = "${var.name}-results-dlq-not-empty"
  alarm_description   = "Worker results failed processing and landed in the dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.results_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == null ? [] : [var.alarm_topic_arn]
  ok_actions          = var.alarm_topic_arn == null ? [] : [var.alarm_topic_arn]
}

resource "aws_sqs_queue" "results" {
  name                        = "${var.name}-results.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = 5400
  message_retention_seconds   = 345600
  sqs_managed_sse_enabled     = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.results_dlq.arn
    maxReceiveCount     = 5
  })
}

locals {
  price = var.bedrock_usd_per_million_tokens
}

resource "aws_cloudwatch_metric_alarm" "bedrock_cost" {
  alarm_name          = "${var.name}-bedrock-daily-cost"
  alarm_description   = "Estimated daily Bedrock spend for the configured model exceeds ${var.bedrock_daily_cost_alarm_usd} USD."
  evaluation_periods  = 1
  threshold           = var.bedrock_daily_cost_alarm_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == null ? [] : [var.alarm_topic_arn]

  metric_query {
    id          = "usd"
    expression  = "(FILL(input, 0) * ${local.price.input} + FILL(output, 0) * ${local.price.output} + FILL(cache_read, 0) * ${local.price.cache_read} + FILL(cache_write, 0) * ${local.price.cache_write}) / 1000000"
    label       = "Estimated USD per day"
    return_data = true
  }

  metric_query {
    id = "input"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "InputTokenCount"
      dimensions  = { ModelId = local.cfg.model.id }
      period      = 86400
      stat        = "Sum"
    }
  }

  metric_query {
    id = "output"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "OutputTokenCount"
      dimensions  = { ModelId = local.cfg.model.id }
      period      = 86400
      stat        = "Sum"
    }
  }

  metric_query {
    id = "cache_read"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "CacheReadInputTokenCount"
      dimensions  = { ModelId = local.cfg.model.id }
      period      = 86400
      stat        = "Sum"
    }
  }

  metric_query {
    id = "cache_write"
    metric {
      namespace   = "AWS/Bedrock"
      metric_name = "CacheWriteInputTokenCount"
      dimensions  = { ModelId = local.cfg.model.id }
      period      = 86400
      stat        = "Sum"
    }
  }
}
