resource "aws_ecr_repository" "control" {
  name                 = "${var.name}-control"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

locals {
  control_env = {
    BED_TABLE         = aws_dynamodb_table.state.name
    BED_CONFIG_BUCKET = aws_s3_bucket.config.id
    BED_CONFIG_KEY    = aws_s3_object.config.key
    BED_RUNTIME_ARN   = aws_bedrockagentcore_agent_runtime.worker.agent_runtime_arn
    BED_MEMORY_ID     = aws_bedrockagentcore_memory.exploration.id
    BED_CONFIG_REV    = filemd5(var.config_path)
  }
  control_functions = {
    schedule = "bed.platform.aws.handlers.schedule_handler"
    result   = "bed.platform.aws.handlers.result_handler"
  }
}

resource "aws_cloudwatch_log_group" "control" {
  for_each          = local.control_functions
  name              = "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = 30
}

resource "aws_lambda_function" "schedule" {
  function_name = "${var.name}-schedule"
  role          = aws_iam_role.control.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.control.repository_url}:${var.control_image_tag}"
  architectures = ["arm64"]
  timeout       = 900
  memory_size   = 1024

  image_config {
    command = [local.control_functions.schedule]
  }

  environment {
    variables = local.control_env
  }

  depends_on = [aws_cloudwatch_log_group.control]
}

resource "aws_lambda_function" "result" {
  function_name = "${var.name}-result"
  role          = aws_iam_role.control.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.control.repository_url}:${var.control_image_tag}"
  architectures = ["arm64"]
  timeout       = 900
  memory_size   = 1024

  image_config {
    command = [local.control_functions.result]
  }

  environment {
    variables = local.control_env
  }

  depends_on = [aws_cloudwatch_log_group.control]
}

resource "aws_lambda_event_source_mapping" "results" {
  event_source_arn        = aws_sqs_queue.results.arn
  function_name           = aws_lambda_function.result.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
}
