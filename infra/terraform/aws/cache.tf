resource "aws_s3_bucket" "cache" {
  bucket_prefix = "${var.name}-cache-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "cache" {
  bucket                  = aws_s3_bucket.cache.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cache" {
  bucket = aws_s3_bucket.cache.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cache" {
  bucket = aws_s3_bucket.cache.id

  rule {
    id     = "expire"
    status = "Enabled"
    filter {}

    expiration {
      days = var.cache_expiry_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "cache_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.control.arn]
    }
  }
}

data "aws_iam_policy_document" "cache" {
  statement {
    sid       = "CacheObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.cache.arn}/repos/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceVpce"
      values   = [aws_vpc_endpoint.s3.id]
    }
  }
}

resource "aws_iam_role" "cache" {
  name_prefix          = "${var.name}-cache-"
  assume_role_policy   = data.aws_iam_policy_document.cache_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "cache" {
  role   = aws_iam_role.cache.id
  policy = data.aws_iam_policy_document.cache.json
}
