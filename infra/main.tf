locals {
  name = "${var.project}-${var.environment}"

  tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    },
    var.tags,
  )
}

# ---------------------------------------------------------------------------
# Security Group — the database's firewall.
#
# Inbound: PostgreSQL (5432) ONLY from the application's security group(s).
# No CIDR ranges, nothing from 0.0.0.0/0. If your app SG isn't in the list,
# it can't even open a socket to the database.
# ---------------------------------------------------------------------------
resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "Allow Postgres from the application security groups only"
  vpc_id      = var.vpc_id
  tags        = merge(local.tags, { Name = "${local.name}-db" })
}

resource "aws_security_group_rule" "db_ingress_from_app" {
  for_each = toset(var.allowed_app_security_group_ids)

  type                     = "ingress"
  description              = "Postgres from app SG ${each.value}"
  security_group_id        = aws_security_group.db.id
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = each.value
}

# ---------------------------------------------------------------------------
# Subnet group — tells RDS which (private) subnets it may place the DB in.
# ---------------------------------------------------------------------------
resource "aws_db_subnet_group" "this" {
  name       = "${local.name}-db"
  subnet_ids = var.db_subnet_ids
  tags       = merge(local.tags, { Name = "${local.name}-db" })
}

# ---------------------------------------------------------------------------
# Parameter group — force every connection to use TLS.
# rds.force_ssl = 1 makes the server reject any non-SSL connection.
# ---------------------------------------------------------------------------
resource "aws_db_parameter_group" "this" {
  name        = "${local.name}-pg"
  family      = var.parameter_group_family
  description = "Force SSL for ${local.name}"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# The database instance.
#
# Security posture:
#   - manage_master_user_password: RDS generates the password and stores it in
#     AWS Secrets Manager (with rotation). The password is NEVER in Terraform
#     state, this repo, or any .env file.
#   - storage_encrypted: encrypted at rest with the AWS-managed KMS key.
#   - publicly_accessible = false: no public IP; reachable only inside the VPC.
#   - iam_database_authentication_enabled: lets the app connect with short-lived
#     IAM tokens later (no password at all) — see infra/README.md.
# ---------------------------------------------------------------------------
resource "aws_db_instance" "this" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  db_name  = var.db_name
  username = var.db_username

  # RDS generates + stores + rotates the master password in Secrets Manager.
  manage_master_user_password = true

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  publicly_accessible                 = false
  db_subnet_group_name                = aws_db_subnet_group.this.name
  vpc_security_group_ids              = [aws_security_group.db.id]
  parameter_group_name                = aws_db_parameter_group.this.name
  iam_database_authentication_enabled = true

  multi_az                = var.multi_az
  backup_retention_period = var.backup_retention_days
  deletion_protection     = var.deletion_protection
  auto_minor_version_upgrade = true

  # Keep a final snapshot when the instance is deleted.
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-final"

  tags = merge(local.tags, { Name = local.name })
}
