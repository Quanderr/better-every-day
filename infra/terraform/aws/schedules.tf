locals {
  repos_by_name = { for repo in local.cfg.repos : repo.name => repo }

  cron_parts = merge(
    { for name, repo in local.repos_by_name : "repo:${name}" => split(" ", trimspace(repo.schedule)) },
    try(local.cfg.notifications, null) == null ? {} : {
      "summary" = split(" ", trimspace(local.cfg.notifications.summary_schedule))
    },
  )

  eventbridge_cron = {
    for key, p in local.cron_parts : key => (
      p[4] == "*"
      ? "cron(${p[0]} ${p[1]} ${p[2]} ${p[3]} ? *)"
      : format("cron(%s %s ? %s %s *)", p[0], p[1], p[3], join(",", [
        for part in split(",", p[4]) : join("-", [
          for x in split("-", part) : can(tonumber(x)) ? tostring(tonumber(x) % 7 + 1) : x
        ])
      ]))
    )
  }
}

resource "aws_scheduler_schedule" "repo" {
  for_each                     = local.repos_by_name
  name                         = substr("${var.name}-${replace(each.key, "/[^A-Za-z0-9_.-]/", "-")}", 0, 64)
  schedule_expression          = local.eventbridge_cron["repo:${each.key}"]
  schedule_expression_timezone = each.value.timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.schedule.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ action = "run", repo = each.key })

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

resource "aws_scheduler_schedule" "summary" {
  count                        = try(local.cfg.notifications, null) == null ? 0 : 1
  name                         = "${var.name}-summary"
  schedule_expression          = local.eventbridge_cron["summary"]
  schedule_expression_timezone = local.cfg.notifications.timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.schedule.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ action = "summary" })

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
