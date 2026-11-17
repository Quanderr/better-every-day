output "worker_repository_url" {
  value = aws_ecr_repository.worker.repository_url
}

output "control_repository_url" {
  value = aws_ecr_repository.control.repository_url
}

output "runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.worker.agent_runtime_arn
}

output "table_name" {
  value = aws_dynamodb_table.state.name
}

output "results_queue_url" {
  value = aws_sqs_queue.results.url
}
