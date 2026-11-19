locals {
  proxy_port = 8443

  proxy_nginx_conf = <<-CONF
    user nginx;
    worker_processes auto;
    error_log /var/log/nginx/error.log;
    pid /run/nginx.pid;
    include /usr/share/nginx/modules/*.conf;

    events {
      worker_connections 4096;
    }

    stream {
      log_format egress '$time_iso8601 $remote_addr $ssl_preread_server_name $upstream_addr $status';
      access_log /var/log/nginx/egress.log egress;
      resolver 169.254.169.253 valid=60s ipv6=off;

      map $ssl_preread_server_name $egress_target {
        hostnames;
        default 127.0.0.1:9;
    %{for domain in local.allowed_domains~}
        ${domain} $ssl_preread_server_name:443;
    %{endfor~}
      }

      server {
        listen ${local.proxy_port};
        ssl_preread on;
        proxy_pass $egress_target;
        proxy_connect_timeout 10s;
      }
    }
  CONF

  proxy_nftables_conf = <<-CONF
    table ip egress {
      chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        ip saddr { ${join(", ", local.private_cidrs)} } tcp dport 443 redirect to :${local.proxy_port}
      }
    }
  CONF

  proxy_user_data = <<-SH
    #!/bin/bash
    set -euo pipefail
    dnf install -y nginx nginx-mod-stream nftables
    sysctl -w net.ipv4.ip_forward=0
    echo 'net.ipv4.ip_forward = 0' > /etc/sysctl.d/90-egress-proxy.conf
    cat > /etc/nginx/nginx.conf <<'NGINX'
    ${local.proxy_nginx_conf}
    NGINX
    cat > /etc/sysconfig/nftables.conf <<'NFT'
    ${local.proxy_nftables_conf}
    NFT
    systemctl enable --now nftables
    systemctl enable --now nginx
  SH
}

data "aws_ssm_parameter" "al2023_arm64" {
  count = local.use_proxy ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "proxy" {
  count       = local.use_proxy ? 1 : 0
  name_prefix = "${var.name}-proxy-"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = local.private_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_iam_policy_document" "proxy_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "proxy" {
  count              = local.use_proxy ? 1 : 0
  name_prefix        = "${var.name}-proxy-"
  assume_role_policy = data.aws_iam_policy_document.proxy_trust.json
}

resource "aws_iam_role_policy_attachment" "proxy_ssm" {
  count      = local.use_proxy ? 1 : 0
  role       = aws_iam_role.proxy[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "proxy" {
  count       = local.use_proxy ? 1 : 0
  name_prefix = "${var.name}-proxy-"
  role        = aws_iam_role.proxy[0].name
}

resource "aws_instance" "proxy" {
  count                       = local.use_proxy ? 1 : 0
  ami                         = data.aws_ssm_parameter.al2023_arm64[0].value
  instance_type               = var.proxy_instance_type
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.proxy[0].id]
  iam_instance_profile        = aws_iam_instance_profile.proxy[0].name
  source_dest_check           = false
  associate_public_ip_address = false
  user_data                   = local.proxy_user_data
  user_data_replace_on_change = true

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted = true
  }

  tags = {
    Name = "${var.name}-egress-proxy"
  }

  lifecycle {
    ignore_changes = [ami]
  }

  depends_on = [aws_iam_role_policy_attachment.proxy_ssm]
}

resource "aws_eip_association" "proxy" {
  count         = local.use_proxy ? 1 : 0
  allocation_id = aws_eip.nat.id
  instance_id   = aws_instance.proxy[0].id
}
