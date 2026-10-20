# Updates only an existing protected re-pxe installation. Does not deploy Windows.
param(
    [Parameter(Mandatory=$true)][string]$ClientPath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedSha256
)
$ErrorActionPreference='Stop'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
if (!(New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {throw 'Run this update in an elevated PowerShell window'}
$root=Join-Path ([IO.Path]::GetPathRoot($env:SystemRoot)) 'ProgramData\re-pxe'
$exe=Join-Path $root 're-pxe.exe';$statePath=Join-Path $root 'completion.json'
foreach ($path in @($root,$exe,$statePath,$ClientPath)) {
    $item=Get-Item -LiteralPath $path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Reparse paths cannot be used for this update'}
}
foreach ($path in @((Split-Path $root),$root,$exe,$statePath)) {
    foreach ($rule in (Get-Acl -LiteralPath $path).Access) {
        $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        $writes=[Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Modify -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
        # ProgramData itself has normal application-create permissions; protected
        # installation files must only be writable by SYSTEM and Administrators.
        if ($path -ne (Split-Path $root) -and $rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $writes) -and $sid -notin @('S-1-5-18','S-1-5-32-544')) {throw 'Installed re-pxe permissions are not protected'}
    }
}
$locks=@();$backup=$null;$changed=$false
try {
    foreach ($name in @('Global\repxeDriverConsole','Global\repxeCompletion','Global\repxeDriverUpdate')) {
        $mutex=New-Object Threading.Mutex($false,$name);$owned=$false
        try {$owned=$mutex.WaitOne(0)} catch [Threading.AbandonedMutexException] {$owned=$true}
        if (!$owned) {$mutex.Dispose();throw 'Close the re-pxe console and wait for running operations before updating'}
        $locks+=,$mutex
    }
    $state=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.schemaVersion -ne 1 -or $state.driverConsole -ne $true -or ($state.stage -eq 'complete' -and $state.deliveryStatus -ne 'pending')) {throw 'A pending provisioned technician installation or sealed pending report is required'}
    if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ne $state.clientSha256) {throw 'Installed executable does not match its protected binding'}
    # Windows servicing may replace the kernel after provisioning. The new
    # client's completion-status validates any changed kernel's Microsoft
    # signature and all original disk/partition bindings before accepting this
    # update. A failed validation restores both the old executable and state.
    if ((Get-FileHash -LiteralPath $ClientPath -Algorithm SHA256).Hash -ne $ExpectedSha256) {throw 'Replacement executable does not match the reviewed release SHA-256'}
    $imageState=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State').ImageState
    if ($imageState -notin @('IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT','IMAGE_STATE_GENERALIZE_RESEAL_TO_AUDIT','IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE')) {throw 'The provisioned Audit/OOBE checkpoint is required'}
    $queue=Join-Path $root 'delivery-queue.json'
    if ((Test-Path -LiteralPath $queue) -and (Get-Content -LiteralPath $queue -Raw | ConvertFrom-Json).status -eq 'delivered') {throw 'An earlier immutable report was already accepted; preserve it and request operator recovery instead of replacing it'}
    $backup=Join-Path $root ('client-update-'+[Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $backup | Out-Null
    Copy-Item -LiteralPath $exe -Destination (Join-Path $backup 're-pxe.exe')
    Copy-Item -LiteralPath $statePath -Destination (Join-Path $backup 'completion.json')
    $candidate=Join-Path $root ('candidate-'+[Guid]::NewGuid().ToString('N')+'.exe')
    Copy-Item -LiteralPath $ClientPath -Destination $candidate
    (Get-Item -LiteralPath $candidate).IsReadOnly=$false
    if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -ne $ExpectedSha256) {throw 'Copied replacement hash differs'}
    $changed=$true
    Copy-Item -LiteralPath $candidate -Destination $exe -Force
    Remove-Item -LiteralPath $candidate -Force
    $update=@{utc=[DateTime]::UtcNow.ToString('o');previousSha256=$state.clientSha256;sha256=$ExpectedSha256.ToLowerInvariant();backup=$backup}
    $state | Add-Member -MemberType NoteProperty -Name clientUpdates -Value (@($state.clientUpdates)+@($update) | Where-Object {$_}) -Force
    $state.clientSha256=$ExpectedSha256.ToLowerInvariant()
    $state.toolVersion=(& $exe version)
    if ($LASTEXITCODE) {throw 'Replacement executable version check failed'}
    [IO.File]::WriteAllText($statePath,($state | ConvertTo-Json -Depth 64),[Text.UTF8Encoding]::new($false))
    # Native validation checks the actual disk, GPT, Windows partition, kernel,
    # executable and ACL bindings before the update is accepted. No sealing occurs.
    $status=& $exe completion-status
    if ($LASTEXITCODE) {throw 'Native installation validation failed; restoring the previous client'}
    $status | Out-File -LiteralPath (Join-Path $backup 'validated-status.json') -Encoding utf8
    if ($state.stage -eq 'complete') {Write-Output 'Installed client updated. Windows is already sealed. Run C:\ProgramData\re-pxe\re-pxe.exe deliver-report to retry the original final report.'}
    else {Write-Output 'Installed client updated. Open C:\ProgramData\re-pxe\re-pxe.exe driver-console, then choose Finish setup.'}
} catch {
    if ($changed -and $backup) {
        Copy-Item -LiteralPath (Join-Path $backup 're-pxe.exe') -Destination $exe -Force
        Copy-Item -LiteralPath (Join-Path $backup 'completion.json') -Destination $statePath -Force
    }
    throw
} finally {
    foreach ($mutex in $locks) {$mutex.ReleaseMutex();$mutex.Dispose()}
}
