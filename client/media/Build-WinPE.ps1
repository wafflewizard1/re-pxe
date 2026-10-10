# Produces file artifacts only. It never formats USB media or physical disks.
param(
    [string]$Client = 'C:\re-pxe\artifacts\re-pxe.exe',
    [string]$Tests = 'C:\re-pxe\artifacts\repxe_core_tests.exe',
    [string]$WorkDirectory = 'C:\re-pxe\WinPE-current',
    [string]$Output = 'C:\re-pxe\re-pxe-current-amd64.iso',
    [string]$DriverDirectory = '',
    [string]$TrustedServerCertificate = '',
    [string]$TrustedServerCertificateSha256 = '',
    [switch]$BootEx,
    [switch]$LabValidation,
    [string]$LabStartupScript = '',
    [switch]$LabPowerShell
)
$ErrorActionPreference = 'Stop'
if ($LabValidation -and $LabStartupScript) { throw 'Choose one lab startup mode' }
$kit = 'C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit'
$pe = "$kit\Windows Preinstallation Environment"
$dism = "$kit\Deployment Tools\amd64\DISM\dism.exe"
foreach ($path in @($Client, $Tests, $WorkDirectory, $Output)) {
    if ($path -notmatch '^[A-Za-z]:\\[A-Za-z0-9 _\\.\-]+$') { throw "Unsupported build path: $path" }
}
foreach ($file in @($Client, $Tests, $dism, "$pe\copype.cmd", "$pe\MakeWinPEMedia.cmd")) {
    if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required file missing: $file" }
}
if ((Test-Path $WorkDirectory) -or (Test-Path $Output)) { throw 'Build directory/output already exists; choose new paths' }
if ([IO.Path]::GetExtension($Output) -ne '.iso') { throw 'Output must be an ISO file' }
$certFingerprint='';$certThumbprint='';$trustedCert=$null
if ([bool]$TrustedServerCertificate -ne [bool]$TrustedServerCertificateSha256) { throw 'Certificate path and reviewed SHA-256 must be supplied together' }
if ($TrustedServerCertificate) {
    if ($TrustedServerCertificateSha256 -notmatch '^[0-9a-fA-F]{64}$' -or !(Test-Path -LiteralPath $TrustedServerCertificate -PathType Leaf)) { throw 'Reviewed public CA file and SHA-256 required' }
    $certFingerprint=(Get-FileHash -LiteralPath $TrustedServerCertificate).Hash.ToLowerInvariant()
    if ($certFingerprint -ne $TrustedServerCertificateSha256.ToLowerInvariant()) { throw 'Trusted CA SHA-256 differs from reviewed fingerprint' }
    $bytes=[IO.File]::ReadAllBytes($TrustedServerCertificate)
    if ($bytes.Length -gt 65536) { throw 'CA input exceeds 64 KiB' }
    $text=[Text.Encoding]::ASCII.GetString($bytes)
    if ($text -match '-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+)\s*-----END CERTIFICATE-----') { $bytes=[Convert]::FromBase64String($Matches[1]) }
    $trustedCert=[Security.Cryptography.X509Certificates.X509Certificate2]::new($bytes)
    $basic=@($trustedCert.Extensions | Where-Object {$_.Oid.Value -eq '2.5.29.19'})
    if ($trustedCert.HasPrivateKey -or $basic.Count -ne 1 -or !([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($basic[0],$basic[0].Critical)).CertificateAuthority -or $trustedCert.Subject -ne $trustedCert.Issuer -or $trustedCert.NotBefore.ToUniversalTime() -gt [DateTime]::UtcNow -or $trustedCert.NotAfter.ToUniversalTime() -le [DateTime]::UtcNow) { throw 'A current public root CA certificate is required' }
    $certThumbprint=$trustedCert.Thumbprint
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class repxeCertificateBlob {
    [DllImport("crypt32.dll", SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CertSerializeCertificateStoreElement(IntPtr context, uint flags, byte[] element, ref uint length);
}
'@
}
$parent = Split-Path $WorkDirectory -Parent
New-Item -ItemType Directory -Path $parent, (Split-Path $Output -Parent) -Force | Out-Null
$batch = Join-Path $parent ('build-' + [Guid]::NewGuid().ToString('N') + '.cmd')
function Run-Dism([string[]]$Arguments) {
    & $dism @Arguments
    if ($LASTEXITCODE -ne 0) { throw "DISM failed: $LASTEXITCODE" }
}
try {
    @("@echo off", "call `"$kit\Deployment Tools\DandISetEnv.bat`"", "call `"$pe\copype.cmd`" amd64 `"$WorkDirectory`"", 'exit /b %errorlevel%') | Set-Content $batch -Encoding ASCII
    & cmd.exe /d /c $batch
    if ($LASTEXITCODE -ne 0) { throw "CopyPE failed: $LASTEXITCODE" }
    $boot = "$WorkDirectory\media\sources\boot.wim"
    $mount = "$WorkDirectory\mount"
    $mounted = $false
    try {
        Run-Dism @('/Mount-Image', "/ImageFile:$boot", '/Index:1', "/MountDir:$mount")
        $mounted = $true
        New-Item -ItemType Directory -Path "$mount\re-pxe" -Force | Out-Null
        Copy-Item $Client "$mount\re-pxe\re-pxe.exe"
        Copy-Item $Tests "$mount\re-pxe\repxe_core_tests.exe"
        Run-Dism @("/Image:$mount", '/Set-ScratchSpace:512')
        if ($LabPowerShell) {
            foreach ($package in @('WinPE-WMI','WinPE-NetFX','WinPE-Scripting','WinPE-PowerShell')) {
                Run-Dism @("/Image:$mount", '/Add-Package', "/PackagePath:$pe\amd64\WinPE_OCs\$package.cab")
                Run-Dism @("/Image:$mount", '/Add-Package', "/PackagePath:$pe\amd64\WinPE_OCs\en-us\$($package)_en-us.cab")
            }
        }
        if ($DriverDirectory) { Run-Dism @("/Image:$mount", '/Add-Driver', "/Driver:$DriverDirectory", '/Recurse') }
        $startup = @('@echo off', 'wpeinit', 'mkdir %SystemDrive%\re-pxe\work')
        if ($LabValidation -or $LabStartupScript) {
            $startup += '%SystemDrive%\re-pxe\re-pxe.exe inventory > %SystemDrive%\re-pxe\work\inventory.json'
            $startup += 'type %SystemDrive%\re-pxe\work\inventory.json'
        } else { $startup += 'echo Starting re-pxe...' }
        if ($LabValidation) {
            Copy-Item (Join-Path $PSScriptRoot 'Validate-WinPE.cmd') "$mount\re-pxe\Validate-WinPE.cmd"
            $fixture = "$pe\amd64\en-us\winpe.wim"
            Copy-Item $fixture "$WorkDirectory\media\re-pxe-fixture.wim"
            $config = @{schemaVersion=1; image='X:\re-pxe\work\fixture.wim'; sha256=(Get-FileHash $fixture).Hash.ToLowerInvariant(); targetIdentity='serial:REPXE-LAB-001'; scratchDirectory='X:\re-pxe\work'; jobDirectory='X:\re-pxe\work'}
            [IO.File]::WriteAllText("$mount\re-pxe\plan-config.json", ($config | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
            $startup += 'call %SystemDrive%\re-pxe\Validate-WinPE.cmd'
        }
        if ($LabStartupScript) {
            Copy-Item -LiteralPath $LabStartupScript "$mount\re-pxe\Start-Lab.cmd"
            $startup += 'call %SystemDrive%\re-pxe\Start-Lab.cmd'
        }
        if (!$LabValidation -and !$LabStartupScript) {
            $startup += 'if exist %SystemRoot%\System32\re-pxe-boot.json ( %SystemDrive%\re-pxe\re-pxe.exe pxe --boot %SystemRoot%\System32\re-pxe-boot.json ) else ( %SystemDrive%\re-pxe\re-pxe.exe ui )'
        }
        $startup += @('echo re-pxe CLI is in %SystemDrive%\re-pxe. Use re-pxe.exe --help.', 'cd /d %SystemDrive%\re-pxe')
        $startup | Set-Content "$mount\Windows\System32\startnet.cmd" -Encoding ASCII
        if ($trustedCert) {
            [uint32]$length=0
            if (![repxeCertificateBlob]::CertSerializeCertificateStoreElement($trustedCert.Handle,0,$null,[ref]$length)) { throw 'Measure public certificate registry representation failed' }
            $blob=[byte[]]::new($length)
            if (![repxeCertificateBlob]::CertSerializeCertificateStoreElement($trustedCert.Handle,0,$blob,[ref]$length)) { throw 'Serialize public certificate failed' }
            # Only the copied WinPE SOFTWARE hive is edited; the workstation's root store is untouched.
            $hive='repxeWinPE_'+[Guid]::NewGuid().ToString('N')
            & reg.exe load "HKLM\$hive" "$mount\Windows\System32\config\SOFTWARE"
            if ($LASTEXITCODE) { throw 'Load copied WinPE certificate hive failed' }
            $offline=$null;$key=$null
            try {
                $offline=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($hive,$true)
                $key=$offline.CreateSubKey("Microsoft\SystemCertificates\ROOT\Certificates\$certThumbprint")
                $key.SetValue('Blob',$blob,[Microsoft.Win32.RegistryValueKind]::Binary);$key.Flush()
                Copy-Item -LiteralPath $TrustedServerCertificate "$mount\re-pxe\server-root.crt"
            } finally {
                if ($key) {$key.Dispose()};if ($offline) {$offline.Dispose()}
                [GC]::Collect();[GC]::WaitForPendingFinalizers()
                & reg.exe unload "HKLM\$hive"
                if ($LASTEXITCODE) { throw 'Unload copied WinPE certificate hive failed' }
            }
            $trustedCert.Dispose()
        }
        Run-Dism @('/Unmount-Image', "/MountDir:$mount", '/Commit')
        $mounted = $false
    } finally {
        if ($mounted) { & $dism /Unmount-Image "/MountDir:$mount" /Discard }
    }
    $bootOption = if ($BootEx) {' /bootex'} else {''}
    @('@echo off', "call `"$kit\Deployment Tools\DandISetEnv.bat`"", "call `"$pe\MakeWinPEMedia.cmd`" /ISO `"$WorkDirectory`" `"$Output`"$bootOption", 'exit /b %errorlevel%') | Set-Content $batch -Encoding ASCII
    & cmd.exe /d /c $batch
    if ($LASTEXITCODE -ne 0) { throw "ISO generation failed: $LASTEXITCODE" }
    $manifest = @{schemaVersion=1; architecture='amd64'; clientVersion=(& $Client version); clientSha256=(Get-FileHash $Client).Hash.ToLowerInvariant(); isoSha256=(Get-FileHash $Output).Hash.ToLowerInvariant(); bootWimSha256=(Get-FileHash $boot).Hash.ToLowerInvariant(); bootEx=[bool]$BootEx; labValidation=[bool]$LabValidation; labPowerShell=[bool]$LabPowerShell; labStartup=[bool]$LabStartupScript; trustedServerCertificateSha256=$certFingerprint; trustedServerCertificateThumbprint=$certThumbprint; builtUtc=[DateTime]::UtcNow.ToString('o')}
    [IO.File]::WriteAllText("$Output.json", ($manifest | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Write-Output "Created $Output and $Output.json. Boot WIM: $boot"
} finally { Remove-Item $batch -ErrorAction SilentlyContinue }
