# Resolve a valid, current PostgreSQL version (and its matching parameter-group
# family) instead of hardcoding a minor version that might get deprecated.
data "aws_rds_engine_version" "postgres" {
  engine  = "postgres"
  version = var.engine_major
  latest  = true
}

# Master password — generated, never typed by a human. Stored (encrypted) in
# Terraform state and in SSM SecureString below. special=false keeps it free of
# characters that would need URL-encoding in the connection string.
resource "random_password" "master" {
  length  = 32
  special = false
}

# Attach this SG to compute (ECS/EC2/Lambda) that must reach the database.
resource "aws_security_group" "app" {
  name_prefix = "${var.name}-app-"
  description = "Attach to compute that needs to reach ${var.name}"
  vpc_id      = var.vpc_id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

# The database's firewall: Postgres (5432) only from the app SG. No public path.
resource "aws_security_group" "db" {
  name_prefix = "${var.name}-db-"
  description = "PostgreSQL access for ${var.name}"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from the app security group"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_subnet_group" "this" {
  name_prefix = "${var.name}-"
  subnet_ids  = var.subnet_ids
  tags        = var.tags
}

# Force every connection to use TLS.
resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.name}-"
  family      = data.aws_rds_engine_version.postgres.parameter_group_family
  description = "Force SSL for ${var.name}"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "this" {
  identifier_prefix = "${var.name}-"
  engine            = "postgres"
  engine_version    = data.aws_rds_engine_version.postgres.version
  instance_class    = var.instance_class

  db_name  = var.db_name
  username = var.username
  password = random_password.master.result

  allocated_storage = var.allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  # Private: no public IP, reachable only from inside the VPC.
  publicly_accessible    = false
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.this.name

  multi_az                   = var.multi_az
  backup_retention_period    = var.backup_retention_days
  deletion_protection        = var.deletion_protection
  auto_minor_version_upgrade = true
  apply_immediately          = true

  # Query-level monitoring. On by default — free at the 7-day retention the
  # provider defaults to. Managed here so it isn't silently disabled by drift.
  performance_insights_enabled = var.performance_insights_enabled

  # Dev convenience: destroy without being forced to take a final snapshot.
  skip_final_snapshot = true

  lifecycle {
    # RDS may bump the minor version itself; don't fight it on every plan.
    ignore_changes = [engine_version]
  }

  tags = var.tags
}

# The ready-to-use SQLAlchemy URL, encrypted at rest. In AWS, inject this into
# the app as DATABASE_URL (it never needs to appear in code or a .env file).
resource "aws_ssm_parameter" "database_url" {
  name        = var.database_url_ssm_name
  description = "SQLAlchemy DATABASE_URL for ${var.name}"
  type        = "SecureString"
  value       = "postgresql+psycopg://${var.username}:${random_password.master.result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${var.db_name}?sslmode=require"
  tags        = var.tags
}
