# Run through tools/windows_remote.py on the authorized test laptop.
param([string]$Root = 'C:\re-pxe')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Path "$Root\installers", "$Root\logs", "$Root\artifacts" -Force | Out-Null
$downloads = @(
    @{Name='adksetup.exe'; Url='https://go.microsoft.com/fwlink/?linkid=2289980'; Feature='OptionId.DeploymentTools'},
    @{Name='adkwinpesetup.exe'; Url='https://go.microsoft.com/fwlink/?linkid=2289981'; Feature='OptionId.WindowsPreinstallationEnvironment'}
)
foreach ($item in $downloads) {
    $file = Join-Path "$Root\installers" $item.Name
    if (!(Test-Path $file)) { Invoke-WebRequest -UseBasicParsing -Uri $item.Url -OutFile $file }
    $signature = Get-AuthenticodeSignature $file
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        throw "Microsoft installer signature validation failed: $($item.Name)"
    }
    Write-Output "Installing $($item.Name) ($((Get-Item $file).VersionInfo.ProductVersion))"
    $info=[Diagnostics.ProcessStartInfo]::new($file,('/quiet /norestart /ceip off /features '+$item.Feature+' /log "'+"$Root\logs\$($item.Name).log"+'"'))
    $info.UseShellExecute=$false
    $process=[Diagnostics.Process]::Start($info)
    # Cache the exit code from the process we started; do not follow MSI service children.
    $process.WaitForExit()
    $code=$process.ExitCode; $process.Dispose()
    if ($code -notin @(0,3010)) { throw "Installer failed with code $code" }
    Write-Output "Completed $($item.Name): $code"
}
$kit = 'C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit'
Get-Item "$kit\Deployment Tools\amd64\DISM\dism.exe", "$kit\Windows Preinstallation Environment\amd64\en-us\winpe.wim" | Select-Object FullName, Length
