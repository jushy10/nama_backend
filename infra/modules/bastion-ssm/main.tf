# A tiny SSM-managed bastion: the secure jump host for reaching the *private*
# RDS database from a laptop. It opens NO inbound ports and has NO SSH key —
# access is brokered entirely by AWS Systems Manager (Session Manager), which the
# preinstalled SSM agent dials out to. You port-forward a local port through it:
#
#   aws ssm start-session --target <instance-id> \
#     --document-name AWS-StartPortForwardingSessionToRemoteHost \
#     --parameters '{"host":["<rds-address>"],"portNumber":["5432"],"localPortNumber":["5432"]}'
#
# then point any Postgres client at localhost:5432. The database stays private;
# nothing about it becomes publicly reachable.

# Amazon Linux 2023 (arm64). The SSM agent ships preinstalled, so the instance is
# Session-Manager-ready the moment it finishes booting. most_recent resolves to a
# patched image, but only at *create* time — the instance's lifecycle block below
# ignores later AMI releases so routine applies don't churn the bastion.
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

# The bastion's identity. Exactly one managed policy —
# AmazonSSMManagedInstanceCore — the minimum the SSM agent needs to register the
# instance and broker sessions. No other permissions.
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name_prefix        = "${var.name}-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "this" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.this.name
  tags        = var.tags
}

# The bastion's own security group: no ingress whatsoever (Session Manager needs
# none), all egress (to reach the SSM endpoints and the database). Permission to
# actually hit Postgres comes from also attaching the database's app SG to the
# instance via var.extra_security_group_ids.
resource "aws_security_group" "this" {
  name_prefix = "${var.name}-"
  # ASCII only: EC2 rejects non-ASCII characters in a security-group description.
  description = "SSM bastion for ${var.name} - no inbound, egress only"
  vpc_id      = var.vpc_id

  egress {
    description = "All outbound (SSM endpoints + database)"
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

resource "aws_instance" "this" {
  ami                  = data.aws_ami.al2023.id
  instance_type        = var.instance_type
  iam_instance_profile = aws_iam_instance_profile.this.name

  subnet_id = var.subnet_id

  # Own SG (egress only) + the database's app SG (which is what the db SG accepts
  # 5432 from). Together: the bastion can reach the DB; nothing can reach the
  # bastion.
  vpc_security_group_ids = concat([aws_security_group.this.id], var.extra_security_group_ids)

  # Default-VPC subnets are public; a public IP lets the SSM agent reach the
  # Systems Manager endpoints over the IGW (no NAT, no VPC endpoints needed).
  # This is NOT an exposure — the security group opens zero inbound ports.
  associate_public_ip_address = true

  # No key_name on purpose: access is SSM-only. Force IMDSv2 and clamp the
  # metadata hop limit so the instance role's credentials can't be reached from a
  # container/SSRF pivot.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # A 1 GiB swapfile. The t4g.nano has only 512 MB RAM, and on a freshly-created
  # instance the first-boot burst — cloud-init, a dnf refresh, and the SSM agent
  # self-updating all at once — briefly exceeds it, so the OOM killer reaps the SSM
  # agent and the box lands in SSM "ConnectionLost" until someone reboots it. Swap
  # absorbs that spike. cloud-init's mounts module creates the file, runs mkswap /
  # swapon, and adds the fstab entry, and it runs at the config stage (~6s into
  # boot) — before the agent self-update spike — so swap is live when the pressure
  # hits. user_data_replace_on_change recreates the instance so the change takes
  # effect on a fresh boot; the bastion is stateless, so nothing is lost.
  user_data_replace_on_change = true
  user_data                   = <<-EOT
    #cloud-config
    swap:
      filename: /swapfile
      size: 1073741824
      maxsize: 1073741824
  EOT

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
    encrypted   = true
  }

  # Pin the running instance to the AMI it was created with. most_recent means a
  # new AL2023 release makes every subsequent apply — however unrelated — plan
  # "must be replaced", churning the bastion and re-tripping the t4g.nano
  # first-boot OOM (see the swapfile note above). ignore_changes only suppresses
  # the in-place diff: a deliberate refresh (terraform taint, or toggling
  # bastion_enabled off and on) still creates from config and picks up the
  # then-latest image, so that's the patch path.
  #
  # associate_public_ip_address is ignored for the same reason: a parked
  # (stopped) bastion releases its public IP, so state refreshes to false and
  # every plan shows false -> true replacement drift. The config value still
  # applies on (re)create, which is when it matters.
  lifecycle {
    ignore_changes = [ami, associate_public_ip_address]
  }

  tags = merge(var.tags, { Name = var.name })
}

# Idle auto-stop: a safety net so a manual start that's forgotten doesn't run up a
# bill. When the box sits at/below auto_stop_cpu_threshold_percent CPU for
# auto_stop_idle_minutes, this alarm fires the built-in EC2 "stop" action — no
# Lambda and no IAM role, since arn:aws:automate:<region>:ec2:stop is a native EC2
# alarm action. A running bastion carrying real tunnel traffic keeps CPU above the
# threshold, so it only trips on a genuinely idle box.
#
# treat_missing_data = notBreaching: a stopped box publishes no CPU metric, so
# "missing" must read as not-idle — otherwise the alarm would sit in ALARM against
# an already-stopped instance. On start, boot CPU is high, so the window naturally
# begins counting from when the box goes quiet.
data "aws_region" "current" {}

resource "aws_cloudwatch_metric_alarm" "idle_stop" {
  count = var.auto_stop_idle_minutes > 0 ? 1 : 0

  alarm_name        = "${var.name}-idle-stop"
  alarm_description = "Stop ${var.name} after ${var.auto_stop_idle_minutes} min at/below ${var.auto_stop_cpu_threshold_percent}% CPU (a forgotten/idle bastion)."

  namespace   = "AWS/EC2"
  metric_name = "CPUUtilization"
  statistic   = "Average"
  dimensions  = { InstanceId = aws_instance.this.id }

  period              = 300
  evaluation_periods  = ceil(var.auto_stop_idle_minutes / 5)
  datapoints_to_alarm = ceil(var.auto_stop_idle_minutes / 5)
  threshold           = var.auto_stop_cpu_threshold_percent
  comparison_operator = "LessThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = ["arn:aws:automate:${data.aws_region.current.name}:ec2:stop"]

  tags = var.tags
}
