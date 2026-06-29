# Настройка Cloud Monitoring для MyBody (roadmap п.2 GCP alerts).
# Не дублирует email-алерт приложения — см. docs/gcp-monitoring.md
#
#   .\scripts\setup_gcp_alerts.ps1 -Project mybody-dev-env -AlertEmail you@gmail.com

param(
    [string]$Project = "mybody-dev-env",
    [string]$Region = "europe-west1",
    [string]$DailyJobName = "mybody-daily-report",
    [string]$WeeklyJobName = "mybody-weekly-report",
    [string]$AlertEmail = ""
)

$ErrorActionPreference = "Stop"

Write-Host "Project:  $Project"
Write-Host "Region:   $Region"
Write-Host "Jobs:     $DailyJobName, $WeeklyJobName"
Write-Host ""

gcloud config set project $Project | Out-Null

# --- Log-based metric: failure alert не ушёл ---
$metricName = "mybody_safety_net"
$metricExists = gcloud logging metrics describe $metricName --project=$Project 2>$null
if (-not $metricExists) {
    Write-Host "Creating log metric $metricName..."
    gcloud logging metrics create $metricName `
        --project=$Project `
        --description="MyBody: pipeline failed and failure alert email was not sent" `
        --log-filter='resource.type="cloud_run_revision" AND textPayload=~"MYBODY_SAFETY_NET"'
} else {
    Write-Host "Log metric $metricName already exists."
}

# --- Notification channel (optional) ---
$channelName = ""
if ($AlertEmail) {
    Write-Host "Creating email notification channel for $AlertEmail..."
    $channelJson = gcloud beta monitoring channels create `
        --display-name="MyBody alerts" `
        --type=email `
        --channel-labels=email_address=$AlertEmail `
        --format="value(name)" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Channel create failed (maybe exists). Listing channels..."
        $channelName = gcloud beta monitoring channels list `
            --filter="displayName='MyBody alerts'" `
            --format="value(name)" `
            --limit=1
    } else {
        $channelName = $channelJson.Trim()
    }
    Write-Host "Channel: $channelName"
}

function New-AlertPolicy {
    param(
        [string]$DisplayName,
        [string]$Filter,
        [string]$Comparison = "COMPARISON_GT",
        [double]$Threshold = 0
    )

    $existing = gcloud monitoring policies list `
        --project=$Project `
        --filter="displayName='$DisplayName'" `
        --format="value(name)" 2>$null
    if ($existing) {
        Write-Host "Policy already exists: $DisplayName"
        return
    }

    $policy = @{
        displayName = $DisplayName
        combiner = "OR"
        conditions = @(
            @{
                displayName = $DisplayName
                conditionThreshold = @{
                    filter = $Filter
                    comparison = $Comparison
                    thresholdValue = $Threshold
                    duration = "0s"
                    aggregations = @(
                        @{
                            alignmentPeriod = "300s"
                            perSeriesAligner = "ALIGN_DELTA"
                        }
                    )
                }
            }
        )
        alertStrategy = @{
            autoClose = "604800s"
        }
    }

    if ($channelName) {
        $policy.notificationChannels = @($channelName)
    }

    $tmp = New-TemporaryFile
    $policy | ConvertTo-Json -Depth 10 | Set-Content -Path $tmp -Encoding UTF8

    Write-Host "Creating policy: $DisplayName"
    gcloud monitoring policies create --policy-from-file=$tmp --project=$Project
    Remove-Item $tmp -Force
}

# Safety net: лог MYBODY_SAFETY_NET
New-AlertPolicy `
    -DisplayName "MyBody — safety net (failure email not sent)" `
    -Filter "resource.type=`"global`" AND metric.type=`"logging.googleapis.com/user/$metricName`"" `
    -Threshold 0

# Scheduler: failed attempts (только когда HTTP != 2xx, т.е. приложение не вернуло 200 после alert)
foreach ($job in @($DailyJobName, $WeeklyJobName)) {
    $title = if ($job -eq $DailyJobName) { "daily" } else { "weekly" }
    New-AlertPolicy `
        -DisplayName "MyBody — Scheduler $title failed" `
        -Filter "resource.type=`"cloud_scheduler_job`" AND resource.labels.location=`"$Region`" AND resource.labels.job_id=`"$job`" AND metric.type=`"cloudscheduler.googleapis.com/job/attempt_failed_count`"" `
        -Threshold 0
}

Write-Host ""
if (-not $channelName) {
    Write-Host "No -AlertEmail: policies created WITHOUT notification channel."
    Write-Host "Add channel in Console: Monitoring -> Alerting -> select policy -> Edit -> Notifications"
} else {
    Write-Host "Done. Alerts will email: $AlertEmail"
}
Write-Host "See docs/gcp-monitoring.md for how this complements PIPELINE_FAILURE_ALERTS."
