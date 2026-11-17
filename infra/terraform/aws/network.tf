data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs                 = slice(data.aws_availability_zones.available.names, 0, 2)
  private_cidrs       = [for i in range(2) : cidrsubnet(var.vpc_cidr, 4, i)]
  firewall_cidrs      = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, 32 + i)]
  public_cidrs        = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, 48 + i)]
  interface_endpoints = ["sqs", "ecr.api", "ecr.dkr", "logs", "bedrock-runtime"]
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = {
    Name = var.name
  }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags = {
    Name = "${var.name}-private-${count.index}"
  }
}

resource "aws_subnet" "firewall" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.firewall_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags = {
    Name = "${var.name}-firewall-${count.index}"
  }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.public_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags = {
    Name = "${var.name}-public-${count.index}"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_eip" "nat" {
  domain = "vpc"
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  dynamic "route" {
    for_each = { for i in range(2) : i => local.private_cidrs[i] }
    content {
      cidr_block      = route.value
      vpc_endpoint_id = local.firewall_endpoints[local.azs[route.key]]
    }
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "firewall" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
}

resource "aws_route_table_association" "firewall" {
  count          = 2
  subnet_id      = aws_subnet.firewall[count.index].id
  route_table_id = aws_route_table.firewall.id
}

resource "aws_route_table" "private" {
  count  = 2
  vpc_id = aws_vpc.main.id

  route {
    cidr_block      = "0.0.0.0/0"
    vpc_endpoint_id = local.firewall_endpoints[local.azs[count.index]]
  }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    sid       = "OwnAccountBuckets"
    actions   = ["s3:GetObject"]
    resources = ["*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid       = "EcrImageLayers"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::prod-${var.region}-starport-layer-bucket/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  policy            = data.aws_iam_policy_document.s3_endpoint.json
}

data "aws_iam_policy_document" "interface_endpoint" {
  statement {
    actions   = ["*"]
    resources = ["*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
  }
}

data "aws_iam_policy_document" "sqs_endpoint" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.results.arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_security_group" "endpoints" {
  name_prefix = "${var.name}-endpoints-"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(local.interface_endpoints)
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  policy = (
    each.key == "sqs"
    ? data.aws_iam_policy_document.sqs_endpoint.json
    : data.aws_iam_policy_document.interface_endpoint.json
  )
}

resource "aws_security_group" "worker" {
  name_prefix = "${var.name}-worker-"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
