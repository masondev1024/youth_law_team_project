data "aws_caller_identity" "current" {}

data "aws_ami" "ubuntu_2404" {
  owners = ["099720109477"] # Canonical

  filter {
    name   = "image-id"
    values = [var.ami_id]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "random_id" "suffix" {
  byte_length = 4
}

resource "time_static" "run_created" {}

locals {
  prefix = "ylr-${var.run_id}-${random_id.suffix.hex}"
  expires_at = formatdate(
    "YYYY-MM-DD'T'hh:mm:ss'Z'",
    timeadd(time_static.run_created.rfc3339, "12h")
  )
  common_tags = {
    Project   = "youth-law-deletion-rehearsal"
    RunId     = var.run_id
    DatasetId = var.dataset_id
    ExpiresAt = local.expires_at
    ManagedBy = "terraform"
  }
}

# A dedicated public subnet is used only for outbound bootstrap and SSM traffic.
# The security group has no ingress rules, so the public IPv4 is never an app endpoint.
resource "aws_vpc" "rehearsal" {
  cidr_block           = "10.82.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, { Name = "${local.prefix}-vpc" })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.rehearsal.id
  cidr_block              = "10.82.1.0/24"
  availability_zone       = "${var.aws_region}a"
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, { Name = "${local.prefix}-public" })
}

resource "aws_internet_gateway" "rehearsal" {
  vpc_id = aws_vpc.rehearsal.id
  tags   = merge(local.common_tags, { Name = "${local.prefix}-igw" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.rehearsal.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.rehearsal.id
  }

  tags = merge(local.common_tags, { Name = "${local.prefix}-public" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "instance" {
  name        = "${local.prefix}-instance"
  description = "Outbound HTTPS and VPC DNS only; no inbound access"
  vpc_id      = aws_vpc.rehearsal.id

  egress {
    description = "HTTPS for SSM and bootstrap"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS over UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.rehearsal.cidr_block]
  }

  egress {
    description = "VPC DNS over TCP"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.rehearsal.cidr_block]
  }

  tags = merge(local.common_tags, { Name = "${local.prefix}-instance" })
}

resource "aws_s3_bucket" "backups" {
  bucket        = "${local.prefix}-backup-${random_id.suffix.hex}"
  force_destroy = true # Dedicated run bucket; destroy removes all versions and delete markers.

  tags = merge(local.common_tags, { Name = "${local.prefix}-backups" })

  # Destroy versioned backup data before removing the deletion authority.
  depends_on = [aws_dynamodb_table.authority]
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "expire-rehearsal-backups"
    status = "Enabled"
    filter { prefix = "" }

    expiration {
      days = 1
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "remove-expired-delete-markers"
    status = "Enabled"
    filter { prefix = "" }
    expiration {
      expired_object_delete_marker = true
    }
  }
}

resource "aws_dynamodb_table" "authority" {
  name         = "${local.prefix}-authority"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = merge(local.common_tags, { Name = "${local.prefix}-authority" })
}

resource "aws_cloudwatch_log_group" "rehearsal" {
  name              = "/youth-law/rehearsal/${local.prefix}"
  retention_in_days = 1

  tags = merge(local.common_tags, { Name = "${local.prefix}-logs" })
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ssm" {
  name               = "${local.prefix}-ssm"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  description        = "Dedicated SSM management role for one rehearsal instance"
  tags               = merge(local.common_tags, { Name = "${local.prefix}-ssm" })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ssm_assume_app" {
  name = "${local.prefix}-assume-app-role"
  role = aws_iam_role.ssm.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = aws_iam_role.app.arn
    }]
  })
}

resource "aws_iam_instance_profile" "ssm" {
  name = "${local.prefix}-ssm"
  role = aws_iam_role.ssm.name
  tags = merge(local.common_tags, { Name = "${local.prefix}-ssm" })
}

resource "aws_cloudwatch_log_group" "authority_writer" {
  name              = "/aws/lambda/${local.prefix}-authority-writer"
  retention_in_days = 1
  tags              = merge(local.common_tags, { Name = "${local.prefix}-authority-writer-logs" })
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "authority_writer" {
  name               = "${local.prefix}-authority-writer"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  description        = "Append-only deletion authority gateway for this rehearsal"
  tags               = merge(local.common_tags, { Name = "${local.prefix}-authority-writer" })
}

data "aws_iam_policy_document" "authority_writer" {
  statement {
    sid       = "ReadAuthorityItems"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.authority.arn]
  }

  statement {
    sid       = "PutAndUpdateOnlyInsideTransactions"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.authority.arn]
    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "dynamodb:EnclosingOperation"
      values   = ["TransactWriteItems"]
    }
  }

  statement {
    sid       = "WriteOwnLogGroup"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.authority_writer.arn}:*"]
  }
}

resource "aws_iam_role_policy" "authority_writer" {
  name   = "${local.prefix}-authority-writer"
  role   = aws_iam_role.authority_writer.id
  policy = data.aws_iam_policy_document.authority_writer.json
}

data "archive_file" "authority_writer" {
  type        = "zip"
  source_file = "${path.module}/../../app/cloud_authority_handler.py"
  output_path = "${path.module}/.terraform/${local.prefix}-authority-writer.zip"
}

resource "aws_lambda_function" "authority_writer" {
  function_name    = "${local.prefix}-authority-writer"
  description      = "Conditional deletion-intent append gateway for the rehearsal"
  role             = aws_iam_role.authority_writer.arn
  runtime          = "python3.12"
  handler          = "cloud_authority_handler.lambda_handler"
  filename         = data.archive_file.authority_writer.output_path
  source_code_hash = data.archive_file.authority_writer.output_base64sha256
  memory_size      = 256
  timeout          = 10
  # This lab account has a concurrency quota of 10; reserving one would violate
  # Lambda's minimum unreserved pool. Synchronous calls are bounded by the
  # single-owner app and conditional writes; there is no public function URL.
  reserved_concurrent_executions = -1

  environment {
    variables = {
      AUTHORITY_TABLE = aws_dynamodb_table.authority.name
    }
  }

  tags = merge(local.common_tags, { Name = "${local.prefix}-authority-writer" })

  depends_on = [aws_iam_role_policy.authority_writer, aws_cloudwatch_log_group.authority_writer]
}

data "aws_iam_policy_document" "app" {
  statement {
    sid       = "ReadAuthority"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.authority.arn]
  }

  statement {
    sid       = "InvokeDeletionAuthorityWriter"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.authority_writer.arn]
  }

  statement {
    sid       = "ReadBundlesAndBackupObjects"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.backups.arn}/deployment/*", "${aws_s3_bucket.backups.arn}/backups/*"]
  }

  statement {
    sid       = "WriteOnlyBackupObjects"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.backups.arn}/backups/*"]
  }

  statement {
    sid       = "WriteBoundedRuntimeLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.rehearsal.arn}:*"]
  }
}

resource "aws_iam_role" "app" {
  name = "${local.prefix}-app"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = aws_iam_role.ssm.arn }
    }]
  })
  description = "Application data-plane role without delete, table administration, or model access"
  tags        = merge(local.common_tags, { Name = "${local.prefix}-app" })
}

resource "aws_iam_role_policy" "app" {
  name   = "${local.prefix}-app-data-plane"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_instance" "app" {
  ami                         = data.aws_ami.ubuntu_2404.id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.instance.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.ssm.name

  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_size           = 10
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
    tags                  = merge(local.common_tags, { Name = "${local.prefix}-root" })
  }

  ebs_block_device {
    device_name           = "/dev/sdf"
    volume_size           = 10
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
    tags                  = merge(local.common_tags, { Name = "${local.prefix}-data" })
  }

  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    app_role_arn = aws_iam_role.app.arn
    aws_region   = var.aws_region
  })
  user_data_replace_on_change = true

  tags = merge(local.common_tags, { Name = "${local.prefix}-app" })

  depends_on = [aws_route_table_association.public, aws_iam_role_policy_attachment.ssm_core, aws_iam_role_policy.ssm_assume_app]
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:scheduler:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schedule-group/${local.prefix}"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  description        = "One-time termination role scoped to this rehearsal instance"
  tags               = merge(local.common_tags, { Name = "${local.prefix}-scheduler" })
}

data "aws_iam_policy_document" "terminate_instance" {
  statement {
    actions   = ["ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/${aws_instance.app.id}"]
  }
}

resource "aws_iam_role_policy" "terminate_instance" {
  name   = "${local.prefix}-terminate-one-instance"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.terminate_instance.json
}

resource "aws_scheduler_schedule" "terminate" {
  name                         = "${local.prefix}-terminate"
  group_name                   = aws_scheduler_schedule_group.rehearsal.name
  description                  = "Terminate the single rehearsal EC2 instance 12 hours after Terraform resource creation"
  schedule_expression          = "at(${formatdate("YYYY-MM-DD'T'hh:mm:ss", timeadd(time_static.run_created.rfc3339, "12h"))})"
  schedule_expression_timezone = "UTC"
  state                        = "ENABLED"
  action_after_completion      = "DELETE"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:terminateInstances"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ InstanceIds = [aws_instance.app.id] })
  }
}

resource "aws_scheduler_schedule_group" "rehearsal" {
  name = local.prefix
  tags = merge(local.common_tags, { Name = "${local.prefix}-scheduler" })
}
