variable "name" {
  type    = string
  default = "better-every-day"
}

variable "region" {
  type = string
}

variable "config_path" {
  type = string
}

variable "worker_image_tag" {
  type = string
}

variable "control_image_tag" {
  type = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "alarm_topic_arn" {
  type    = string
  default = null
}

variable "bedrock_daily_cost_alarm_usd" {
  type    = number
  default = 30
}

variable "bedrock_usd_per_million_tokens" {
  type = object({
    input       = number
    output      = number
    cache_read  = number
    cache_write = number
  })
  default = {
    input       = 4
    output      = 20
    cache_read  = 0.2
    cache_write = 5
  }
}

variable "extra_allowed_domains" {
  type    = list(string)
  default = []
}
