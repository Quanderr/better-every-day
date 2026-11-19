moved {
  from = aws_route_table.firewall
  to   = aws_route_table.firewall[0]
}

moved {
  from = aws_security_group.endpoints
  to   = aws_security_group.endpoints[0]
}

moved {
  from = aws_networkfirewall_rule_group.egress_allowlist
  to   = aws_networkfirewall_rule_group.egress_allowlist[0]
}

moved {
  from = aws_networkfirewall_firewall_policy.main
  to   = aws_networkfirewall_firewall_policy.main[0]
}

moved {
  from = aws_networkfirewall_firewall.main
  to   = aws_networkfirewall_firewall.main[0]
}

moved {
  from = aws_cloudwatch_log_group.firewall
  to   = aws_cloudwatch_log_group.firewall[0]
}

moved {
  from = aws_networkfirewall_logging_configuration.main
  to   = aws_networkfirewall_logging_configuration.main[0]
}

moved {
  from = aws_nat_gateway.main
  to   = aws_nat_gateway.main[0]
}
