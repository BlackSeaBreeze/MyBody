# Однократная настройка GCS bucket для отчётов MyBody.
# Запуск из корня репозитория (нужен gcloud auth и права Owner/Storage Admin):
#
#   .\scripts\setup_gcs_bucket.ps1
#   .\scripts\setup_gcs_bucket.ps1 -Project mybody-dev-env -Bucket mybody-dev-env-reports

param(
    [string]$Project = "mybody-dev-env",
    [string]$Region = "europe-west1",
    [string]$Bucket = "mybody-dev-env-reports"
)

$ErrorActionPreference = "Stop"

$projectNumber = gcloud projects describe $Project --format="value(projectNumber)"
$runSa = "${projectNumber}-compute@developer.gserviceaccount.com"

Write-Host "Project:     $Project ($projectNumber)"
Write-Host "Bucket:      gs://$Bucket"
Write-Host "Cloud Run SA: $runSa"
Write-Host ""

$exists = gcloud storage buckets describe "gs://$Bucket" --project=$Project 2>$null
if (-not $exists) {
    Write-Host "Creating bucket..."
    gcloud storage buckets create "gs://$Bucket" `
        --project=$Project `
        --location=$Region `
        --uniform-bucket-level-access
} else {
    Write-Host "Bucket already exists."
}

Write-Host "Granting objectAdmin to Cloud Run SA..."
gcloud storage buckets add-iam-policy-binding "gs://$Bucket" `
    --project=$Project `
    --member="serviceAccount:$runSa" `
    --role="roles/storage.objectAdmin"

Write-Host ""
Write-Host "Done. Set in Cloud Run deploy:"
Write-Host "  GCS_REPORTS_BUCKET=$Bucket"
Write-Host "  outcomes/vb-YYYYMMDD-HHMM.html"
Write-Host "  archive/vb-YYYYMMDD-HHMM.md"
