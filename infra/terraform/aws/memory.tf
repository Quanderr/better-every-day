resource "aws_bedrockagentcore_memory" "exploration" {
  name                  = "${local.runtime_key}_exploration"
  description           = "Validated repository exploration checkpoints; no automatic extraction"
  event_expiry_duration = 365
}
