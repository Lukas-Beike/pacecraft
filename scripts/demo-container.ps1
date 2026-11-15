<#
Builds the current image and (re)creates a disposable demo container that
seeds itself with the standard synthetic fixture data on every start.
Only fake data is used; providers stay blocked. Password: e2e-fixture-password-1234

  .\scripts\demo-container.ps1                # data lives in the container
  .\scripts\demo-container.ps1 -Persist       # data survives recreation (named volume)
  .\scripts\demo-container.ps1 -SkipBuild -Port 8091
  .\scripts\demo-container.ps1 -Stop            # remove it again (never its volume)
  .\scripts\demo-container.ps1 -HeavyHistory 900  # plus 900 synthetic long sessions (performance checks)
  .\scripts\demo-container.ps1 -HeavyHistory 850 -HeavyWalks 2950 -HeavyYears 6  # production-sized history
  .\scripts\demo-container.ps1 -Image pacecraft:x -Persist  # run that local tag: no build, no retag
                                                  # (the fixture code still comes from this checkout's e2e/)
#>
param(
    [string]$Name = "pacecraft-demo",
    [int]$Port = 8091,
    [switch]$Persist,
    [switch]$SkipBuild,
    [switch]$Stop,
    [ValidateRange(0, 1000)][int]$HeavyHistory = 0,
    # Walks without streams and the years the history spans (#2974); need -HeavyHistory.
    [ValidateRange(0, 5000)][int]$HeavyWalks = 0,
    [ValidateRange(0, 20)][int]$HeavyYears = 0,
    # Another lane's demo may run the shared pacecraft:local tag (#2957). A
    # tag only: never a value docker run would read as a flag.
    [ValidatePattern('^[a-z0-9][a-z0-9._/:@-]*$')][string]$Image = "pacecraft:local"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

# Only disposable demo/fixture containers may be replaced. The production container
# (pacecraft, formerly ai-coach) and every other name are refused before anything is built, stopped or removed.
if ($Name -in @("pacecraft", "ai-coach") -or $Name -notmatch '^(pacecraft-|ai-coach-)?(demo|fixture)') {
    throw "Refusing container name '$Name': use a demo or fixture name such as pacecraft-demo."
}
if (($HeavyWalks -gt 0 -or $HeavyYears -gt 0) -and $HeavyHistory -eq 0) {
    throw "-HeavyWalks and -HeavyYears need -HeavyHistory."
}

# $ErrorActionPreference does not stop on native exit codes in Windows PowerShell 5.1,
# so every native call is followed by an explicit $LASTEXITCODE check.
# Another -Image is run as is: no build into or retag of pacecraft:local, and it
# must exist locally before an existing demo is removed (run never pulls).
$ownImage = $Image -ne "pacecraft:local"
if (-not $SkipBuild -and -not $Stop -and -not $ownImage) {
    docker build -q -t pacecraft:local . | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Image build failed (exit $LASTEXITCODE)." }
}
if ($ownImage -and -not $Stop) {
    docker image inspect $Image | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Image '$Image' is not present locally (exit $LASTEXITCODE)." }
}
$existingContainer = docker ps -aq --filter "name=^$Name$"
if ($LASTEXITCODE -ne 0) { throw "Listing containers failed (exit $LASTEXITCODE)." }
if ($existingContainer) {
    docker stop $Name | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Stopping container '$Name' failed (exit $LASTEXITCODE)." }
    docker rm $Name | Out-Null   # never -v
    if ($LASTEXITCODE -ne 0) { throw "Removing container '$Name' failed (exit $LASTEXITCODE)." }
}
if ($Stop) {
    if ($existingContainer) { Write-Host "Demo removed: $Name" } else { Write-Host "No container named $Name" }
    exit 0
}
$dockerArguments = @("run", "-d", "--pull", "never", "--name", $Name, "-p", "127.0.0.1:${Port}:8090",
    "-e", "FIXTURE_AUTO_SEED=1", "-e", "AI_COACH_DISPOSABLE_FIXTURE=1", "-v", "${PWD}\e2e:/app/e2e:ro")
if ($Persist) { $dockerArguments += @("-v", "${Name}-data:/data") }
if ($HeavyHistory -gt 0) { $dockerArguments += @("-e", "FIXTURE_HEAVY_HISTORY=$HeavyHistory") }
if ($HeavyWalks -gt 0) { $dockerArguments += @("-e", "FIXTURE_HEAVY_WALKS=$HeavyWalks") }
if ($HeavyYears -gt 0) { $dockerArguments += @("-e", "FIXTURE_HEAVY_YEARS=$HeavyYears") }
$dockerArguments += @($Image, "python", "/app/e2e/fixture_runtime.py")
docker @dockerArguments | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Starting container '$Name' failed (exit $LASTEXITCODE)." }

# Bounded readiness wait: at most 60 probes (600 while a heavy history seeds,
# plus one per walk).
# Failed probes and non-200 answers both sleep.
$probes = if ($HeavyHistory -gt 0) { 600 + $HeavyWalks } else { 60 }
$healthy = $false
for ($i = 0; $i -lt $probes -and -not $healthy; $i++) {
    try {
        if ((Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) {
            $healthy = $true
        } else {
            Start-Sleep -Seconds 2
        }
    } catch {
        Start-Sleep -Seconds 2
    }
    # A refused seed (for example another -HeavyHistory on a -Persist volume) ends the container.
    if (-not $healthy -and (docker inspect -f "{{.State.Running}}" $Name) -eq "false") {
        docker logs --tail 5 $Name
        throw "Demo container '$Name' exited during startup; see its last log lines above."
    }
}
if (-not $healthy) { throw "Demo container did not become healthy on port $Port" }
Write-Host "Demo ready: http://127.0.0.1:$Port (password: e2e-fixture-password-1234)"