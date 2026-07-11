#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Start / stop / connect the nama-dev SSM bastion for the private RDS tunnel.

.DESCRIPTION
    The bastion is parked (stopped) by default to save ~$6/mo - see
    infra/environments/dev (bastion_desired_state). It keeps its instance id and
    disk across stop/start, so turning it on is instant: no terraform, no
    recreate (which would re-trigger the first-boot OOM the t4g.nano hits).

    A manually started box stays up until the next `terraform apply` reconciles
    it back to "stopped". Run `down` when you're done to stop paying sooner. As a
    backstop, a CloudWatch alarm also auto-stops the box after ~15 min of near-idle
    CPU, so a start you forget to stop won't run up a bill (an open-but-idle tunnel
    is stopped too - just reconnect).

    Requires the AWS CLI (v2) and the Session Manager plugin:
      https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html

.PARAMETER Command
    up       Start the box and wait until its SSM agent is Online.
    down     Stop the box (back to ~$0.64/mo disk-only).
    connect  Start (if needed), then open the DB tunnel: localhost -> RDS:5432.
    status   Show the EC2 power state and SSM registration. (default)

.EXAMPLE
    ./infra/bastion.ps1 connect
    # ... query the DB via localhost:5432, Ctrl-C to close, then:
    ./infra/bastion.ps1 down
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'connect', 'status')]
    [string]$Command = 'status',

    [int]$LocalPort = 5432
)

$ErrorActionPreference = 'Stop'
if (-not $env:AWS_REGION -and -not $env:AWS_DEFAULT_REGION) { $env:AWS_REGION = 'us-east-1' }

$Tag = 'nama-dev-bastion'

function Get-BastionId {
    $id = (aws ec2 describe-instances `
            --filters "Name=tag:Name,Values=$Tag" "Name=instance-state-name,Values=pending,running,stopping,stopped" `
            --query 'Reservations[].Instances[].InstanceId' --output text)
    if ($LASTEXITCODE -ne 0) { throw "aws ec2 describe-instances failed (check your AWS credentials/region)." }
    $id = ($id | Out-String).Trim()
    if (-not $id) { throw "No bastion instance tagged '$Tag'. Is bastion_enabled = true and applied?" }
    return $id
}

function Get-BastionState {
    param([string]$Id)
    (aws ec2 describe-instances --instance-ids $Id `
            --query 'Reservations[].Instances[].State.Name' --output text | Out-String).Trim()
}

function Get-SsmPing {
    param([string]$Id)
    $p = (aws ssm describe-instance-information `
            --filters "Key=InstanceIds,Values=$Id" `
            --query 'InstanceInformationList[].PingStatus | [0]' --output text | Out-String).Trim()
    if (-not $p -or $p -eq 'None') { return '(not registered)' }
    return $p
}

function Get-DbHost {
    $h = (aws rds describe-db-instances `
            --query "DBInstances[?starts_with(DBInstanceIdentifier,'nama-dev')].Endpoint.Address | [0]" `
            --output text | Out-String).Trim()
    if (-not $h -or $h -eq 'None') { throw "Could not resolve the nama-dev RDS address." }
    return $h
}

function Wait-SsmOnline {
    param([string]$Id)
    Write-Host "Waiting for the SSM agent to come online" -NoNewline
    for ($i = 0; $i -lt 48; $i++) {
        if ((Get-SsmPing $Id) -eq 'Online') { Write-Host " online."; return }
        Start-Sleep -Seconds 5
        Write-Host '.' -NoNewline
    }
    Write-Host ''
    throw "SSM agent did not register within ~4 min. Check './infra/bastion.ps1 status'."
}

function Confirm-Up {
    param([string]$Id)
    $state = Get-BastionState $Id
    if ($state -eq 'stopping') { Write-Host "Bastion is stopping; waiting..."; aws ec2 wait instance-stopped --instance-ids $Id; $state = 'stopped' }
    if ($state -eq 'running') {
        if ((Get-SsmPing $Id) -ne 'Online') { Wait-SsmOnline $Id }
        return
    }
    Write-Host "Starting bastion $Id ..."
    aws ec2 start-instances --instance-ids $Id | Out-Null
    aws ec2 wait instance-running --instance-ids $Id
    Wait-SsmOnline $Id
}

$id = Get-BastionId

switch ($Command) {
    'status' {
        Write-Host "Bastion : $id"
        Write-Host "EC2     : $(Get-BastionState $id)"
        Write-Host "SSM     : $(Get-SsmPing $id)"
    }

    'up' {
        Confirm-Up $id
        Write-Host "Bastion is up and SSM-ready ($id)."
        Write-Host "Tunnel with './infra/bastion.ps1 connect'; stop it with './infra/bastion.ps1 down'."
    }

    'down' {
        Write-Host "Stopping bastion $id ..."
        aws ec2 stop-instances --instance-ids $id | Out-Null
        Write-Host 'Stop requested - back to disk-only (~$0.64/mo) once fully stopped.'
    }

    'connect' {
        Confirm-Up $id
        $dbHost = Get-DbHost

        # Pass the port-forward parameters via a temp file (file://) rather than an
        # inline JSON arg - Windows PowerShell mangles embedded quotes when handing
        # a JSON string to a native exe, and file:// sidesteps that entirely.
        $params = '{"host":["' + $dbHost + '"],"portNumber":["5432"],"localPortNumber":["' + $LocalPort + '"]}'
        $tmp = [System.IO.Path]::GetTempFileName()
        Set-Content -Path $tmp -Value $params -Encoding ascii

        Write-Host ''
        Write-Host "Tunnel : localhost:$LocalPort  ->  ${dbHost}:5432"
        Write-Host "Client : point psql/DBeaver/TablePlus at host=localhost port=$LocalPort (sslmode=require)."
        Write-Host "Creds  : aws ssm get-parameter --name /nama/dev/database-url --with-decryption --query Parameter.Value --output text"
        Write-Host "Close  : Ctrl-C here, then './infra/bastion.ps1 down' to stop paying."
        Write-Host ''
        try {
            aws ssm start-session --target $id `
                --document-name AWS-StartPortForwardingSessionToRemoteHost `
                --parameters "file://$tmp"
        }
        finally {
            Remove-Item -Path $tmp -ErrorAction SilentlyContinue
        }
    }
}
