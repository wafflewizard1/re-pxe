param([Parameter(Mandatory=$true)][string]$InstallationId,
      [Parameter(Mandatory=$true)][string]$ExpectedClientSha256,
      [int]$AgentProcessId=0, [switch]$ValidateOnly)
$ErrorActionPreference='Stop'
if ($InstallationId -notmatch '^[a-f0-9]{16}$' -or $ExpectedClientSha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid cleanup binding' }
$mutex=New-Object Threading.Mutex($false,('Global\repxeCleanup-'+$InstallationId))
if (-not $mutex.WaitOne(0)) { exit 0 }
try {
    $root=Join-Path $env:ProgramData 're-pxe'
    $stage=Join-Path $env:SystemDrive '.re-pxeStage'
    $task='re-pxe-cleanup-'+$InstallationId
    function SafeTree([string]$Path) {
        $item=Get-Item -LiteralPath $Path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Cleanup refuses reparse points' }
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $Path -Force) { SafeTree $child.FullName }
        }
    }
    # Do not follow a redirected ProgramData or volume root, even for inspection.
    foreach ($parent in @(($env:SystemDrive+'\'),$env:ProgramData)) {
        if ((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Cleanup root is redirected' }
    }
    $scriptDirectory=Split-Path -Parent $PSCommandPath
    $authorizedFile=Join-Path $scriptDirectory 'authorized.json'
    $authorized=$null
    if (-not $ValidateOnly) {
        [Environment]::CurrentDirectory=$env:SystemRoot
        if ((Split-Path -Leaf $scriptDirectory) -ne ('re-pxe-cleanup-'+$InstallationId) -or
            (Split-Path -Parent $scriptDirectory) -ne (Join-Path $env:SystemRoot 'Temp')) { throw 'Cleanup must run from its bound protected task folder' }
        if (Test-Path -LiteralPath $authorizedFile) {
            $authorized=Get-Content -LiteralPath $authorizedFile -Raw | ConvertFrom-Json
            if ($authorized.installationId -ne $InstallationId -or $authorized.clientSha256 -ne $ExpectedClientSha256 -or $authorized.root -ne $root) { throw 'Saved cleanup authorization differs' }
        }
    }
    if (Test-Path -LiteralPath $root) { SafeTree $root }
    if (-not $authorized -and (Test-Path -LiteralPath $root)) {
        $state=Get-Content -LiteralPath (Join-Path $root 'completion.json') -Raw | ConvertFrom-Json
        if ($state.id -ne $InstallationId -or $state.clientSha256 -ne $ExpectedClientSha256 -or
            $state.stage -ne 'complete' -or $state.sealedImageState -ne 'IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE' -or
            $state.finishOptions.cleanup_client -ne $true -or $state.deliveryStatus -notin @('delivered','local-only')) { throw 'Cleanup requires the bound sealed, accepted completion' }
        if (Test-Path -LiteralPath (Join-Path $root 'delivery-secret.json')) { throw 'Report credential is still pending' }
        if ($state.deliveryStatus -eq 'delivered') {
            $queue=Get-Content -LiteralPath (Join-Path $root 'delivery-queue.json') -Raw | ConvertFrom-Json
            $hash=(Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $root 'report.json')).Hash.ToLowerInvariant()
            if ($queue.status -ne 'delivered' -or $queue.sha256 -ne $hash -or $queue.acknowledgement.accepted -ne $true -or $queue.acknowledgement.sha256 -ne $hash) { throw 'Cleanup requires durable report acknowledgement' }
            if ($state.finishOptions.save_report_desktop -eq $true) {
                if (-not $queue.customerFiles -or $queue.customerFiles.Count -lt 1) { throw 'Customer report copy is pending' }
                foreach ($pdf in $queue.customerFiles) {
                    if (-not (Test-Path -LiteralPath $pdf.path) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $pdf.path).Hash.ToLowerInvariant() -ne $pdf.sha256) { throw 'Customer report copy failed verification' }
                }
            }
        }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $root 're-pxe.exe')).Hash.ToLowerInvariant() -ne $ExpectedClientSha256) { throw 'Installed client changed before cleanup' }
        $removeStage=$state.pxeStagingCleanup -eq $true
        if ($removeStage -and (Test-Path -LiteralPath $stage)) {
            SafeTree $stage
            $owner=Get-Content -LiteralPath (Join-Path $stage 're-pxe-owner.json') -Raw | ConvertFrom-Json
            if ($owner.installationId -ne $InstallationId -or $owner.clientSha256 -ne $ExpectedClientSha256) { throw 'PXE staging ownership differs' }
        }
        if ($ValidateOnly) { Write-Output 'Cleanup bindings verified'; exit 0 }
        if ($AgentProcessId -gt 0) {
            for ($i=0; $i -lt 60; $i++) {
                $agent=Get-Process -Id $AgentProcessId -ErrorAction SilentlyContinue
                if (-not $agent -or $agent.Path -ne (Join-Path $root 're-pxe.exe')) { break }
                Start-Sleep -Seconds 1
            }
            if ($agent -and $agent.Path -eq (Join-Path $root 're-pxe.exe')) { throw 'Client is still running; cleanup will retry at boot' }
        }
        # Retain a protected durable authorization outside the client tree, so a
        # locked file/power interruption can resume without needing deleted state.
        $authorized=@{installationId=$InstallationId;clientSha256=$ExpectedClientSha256;root=$root;removeStage=$removeStage}
        $temporary=Join-Path $scriptDirectory ('authorization-'+[Guid]::NewGuid().ToString()+'.tmp')
        [IO.File]::WriteAllText($temporary,($authorized | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $authorizedFile
    } elseif ($ValidateOnly) { throw 'No bound completion to validate' }
    if (-not $authorized) { throw 'No verified cleanup authorization' }
    if ($authorized.removeStage -and (Test-Path -LiteralPath $stage)) { SafeTree $stage; Remove-Item -LiteralPath $stage -Recurse -Force }
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
    if (-not $ValidateOnly) {
        & "$env:SystemRoot\System32\schtasks.exe" /Delete /TN $task /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Remove cleanup task failed' }
        $scriptDirectory=Split-Path -Parent $PSCommandPath
        if ((Split-Path -Leaf $scriptDirectory) -eq ('re-pxe-cleanup-'+$InstallationId) -and
            (Split-Path -Parent $scriptDirectory) -eq (Join-Path $env:SystemRoot 'Temp')) {
            foreach ($name in @('cleanup.ps1','run.ps1','authorized.json','last-error.txt')) {
                $file=Join-Path $scriptDirectory $name
                if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
            }
            Remove-Item -LiteralPath $scriptDirectory -Force
        }
    }
} catch {
    if (-not $ValidateOnly -and (Split-Path -Leaf $scriptDirectory) -eq ('re-pxe-cleanup-'+$InstallationId) -and
        (Split-Path -Parent $scriptDirectory) -eq (Join-Path $env:SystemRoot 'Temp') -and (Test-Path -LiteralPath $scriptDirectory)) {
        try {[IO.File]::WriteAllText((Join-Path $scriptDirectory 'last-error.txt'),$_.Exception.Message,[Text.UTF8Encoding]::new($false))} catch {}
    }
    throw
} finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
