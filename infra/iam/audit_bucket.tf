# WORM audit bucket. Object Lock in COMPLIANCE mode cannot be shortened or removed
# by any principal, including the account root, until the retention period ends.
resource "aws_s3_bucket" "audit" {
  bucket              = "content-agent-audit-worm"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 2555 # 7 years
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.audit.arn
    }
  }
}

resource "aws_kms_key" "audit" {
  description         = "content-agent audit log"
  enable_key_rotation = true
}

# The agent's runtime role may only PUT. No Get/Delete/List - forensics use a separate auditor role.
data "aws_iam_policy_document" "agent_audit_write" {
  statement {
    actions   = ["s3:PutObject", "s3:PutObjectRetention"]
    resources = ["${aws_s3_bucket.audit.arn}/content-agent/audit/*"]
  }
  statement {
    actions   = ["s3:GetBucketObjectLockConfiguration"]
    resources = [aws_s3_bucket.audit.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey"]
    resources = [aws_kms_key.audit.arn]
  }
}

resource "aws_iam_policy" "agent_audit_write" {
  name   = "content-agent-audit-write-only"
  policy = data.aws_iam_policy_document.agent_audit_write.json
}
