locals {
  allowed_domains = distinct(concat([
    "github.com", ".github.com", ".githubusercontent.com",
    "gitlab.com",
    ".npmjs.org", ".yarnpkg.com",
    "pypi.org", ".pypi.org", ".pythonhosted.org",
    "rubygems.org", ".rubygems.org",
    "repo.maven.apache.org", "plugins.gradle.org", "services.gradle.org",
    "proxy.golang.org", "sum.golang.org",
    "mise.jdx.dev", "mise-versions.jdx.dev", "mise-java.jdx.dev",
    "nodejs.org", "download.java.net",
    "go.dev", "dl.google.com",
    "cache.ruby-lang.org",
  ], var.extra_allowed_domains))

  dns_allowed_domains = distinct(concat(
    [for d in local.allowed_domains : startswith(d, ".") ? "*${d}" : d],
    [for d in local.allowed_domains : trimprefix(d, ".") if startswith(d, ".")],
    ["amazonaws.com", "*.amazonaws.com"],
  ))

  firewall_endpoints = {
    for state in aws_networkfirewall_firewall.main.firewall_status[0].sync_states :
    state.availability_zone => state.attachment[0].endpoint_id
  }
}

resource "aws_networkfirewall_rule_group" "egress_allowlist" {
  name     = "${var.name}-egress-allowlist"
  type     = "STATEFUL"
  capacity = 200

  rule_group {
    rule_variables {
      ip_sets {
        key = "HOME_NET"
        ip_set {
          definition = [var.vpc_cidr]
        }
      }
    }

    rules_source {
      rules_source_list {
        generated_rules_type = "ALLOWLIST"
        target_types         = ["TLS_SNI"]
        targets              = local.allowed_domains
      }
    }

    stateful_rule_options {
      rule_order = "STRICT_ORDER"
    }
  }
}

resource "aws_networkfirewall_firewall_policy" "main" {
  name = "${var.name}-egress"

  firewall_policy {
    stateless_default_actions          = ["aws:forward_to_sfe"]
    stateless_fragment_default_actions = ["aws:forward_to_sfe"]
    stateful_default_actions           = ["aws:drop_established", "aws:alert_established"]

    stateful_engine_options {
      rule_order = "STRICT_ORDER"
    }

    stateful_rule_group_reference {
      priority     = 100
      resource_arn = aws_networkfirewall_rule_group.egress_allowlist.arn
    }
  }
}

resource "aws_networkfirewall_firewall" "main" {
  name                = var.name
  vpc_id              = aws_vpc.main.id
  firewall_policy_arn = aws_networkfirewall_firewall_policy.main.arn

  dynamic "subnet_mapping" {
    for_each = aws_subnet.firewall[*].id
    content {
      subnet_id = subnet_mapping.value
    }
  }
}

resource "aws_cloudwatch_log_group" "firewall" {
  name              = "/${var.name}/network-firewall/alert"
  retention_in_days = 30
}

resource "aws_networkfirewall_logging_configuration" "main" {
  firewall_arn = aws_networkfirewall_firewall.main.arn

  logging_configuration {
    log_destination_config {
      log_type             = "ALERT"
      log_destination_type = "CloudWatchLogs"
      log_destination = {
        logGroup = aws_cloudwatch_log_group.firewall.name
      }
    }
  }
}

resource "aws_route53_resolver_firewall_domain_list" "allowed" {
  name    = "${var.name}-allowed"
  domains = local.dns_allowed_domains
}

resource "aws_route53_resolver_firewall_domain_list" "everything" {
  name    = "${var.name}-everything"
  domains = ["*"]
}

resource "aws_route53_resolver_firewall_rule_group" "worker" {
  name = "${var.name}-worker-dns"
}

resource "aws_route53_resolver_firewall_rule" "allow" {
  name                    = "allow-listed"
  action                  = "ALLOW"
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.worker.id
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.allowed.id
  priority                = 100
}

resource "aws_route53_resolver_firewall_rule" "block" {
  name                    = "block-everything-else"
  action                  = "BLOCK"
  block_response          = "NXDOMAIN"
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.worker.id
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.everything.id
  priority                = 200
}

resource "aws_route53_resolver_firewall_rule_group_association" "worker" {
  name                   = "${var.name}-worker-dns"
  firewall_rule_group_id = aws_route53_resolver_firewall_rule_group.worker.id
  priority               = 101
  vpc_id                 = aws_vpc.main.id
}
