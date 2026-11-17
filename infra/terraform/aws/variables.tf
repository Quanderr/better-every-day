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

variable "bedrock_daily_token_alarm" {
  type    = number
  default = 50000000
}

variable "extra_allowed_domains" {
  type    = list(string)
  default = []
}
