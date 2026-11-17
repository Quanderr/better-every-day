resource "aws_ecr_repository" "worker" {
  name                 = "${var.name}-worker"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_bedrockagentcore_agent_runtime" "worker" {
  agent_runtime_name = local.runtime_key
  role_arn           = aws_iam_role.worker.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.worker.repository_url}:${var.worker_image_tag}"
    }
  }

  network_configuration {
    network_mode = "VPC"
    network_mode_config {
      subnets         = aws_subnet.private[*].id
      security_groups = [aws_security_group.worker.id]
    }
  }

  environment_variables = {
    BED_RESULT_QUEUE_URL = aws_sqs_queue.results.url
    AWS_REGION           = var.region
  }

  depends_on = [aws_iam_role_policy.worker, aws_route_table_association.private]
}
