param(
    [Parameter(Mandatory = $true)][string]$PowercfgPath,
    [Parameter(Mandatory = $true)][string]$ReportPath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
# Native completion supplies protected, new evidence paths in the Audit installation.
if ((Test-Path -LiteralPath $ReportPath) -or (Test-Path -LiteralPath $OutputPath)) {
    throw 'Battery evidence already exists; preserve the previous report.'
}
& $PowercfgPath /batteryreport /output $ReportPath /xml
if ($LASTEXITCODE -ne 0) { throw "powercfg battery report failed: $LASTEXITCODE" }
if ((Get-Item -LiteralPath $ReportPath).Length -gt 16MB) { throw 'Battery report exceeds the evidence bound.' }
$settings = New-Object System.Xml.XmlReaderSettings
$settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
$settings.XmlResolver = $null
$reader = [System.Xml.XmlReader]::Create($ReportPath, $settings)
$document = New-Object System.Xml.XmlDocument
$document.XmlResolver = $null
try { $document.Load($reader) } finally { $reader.Close() }
function Field($node, $name) {
    $value = $node.SelectSingleNode("*[local-name()='$name']")
    if ($null -eq $value) { return '' }
    return $value.InnerText.Trim()
}
function Number($node, $name, $allowZero) {
    [long]$value = 0
    if (![long]::TryParse((Field $node $name), [ref]$value)) { return $null }
    if ($value -lt 0 -or (!$allowZero -and $value -eq 0) -or $value -ge 4294967295) { return $null }
    return $value
}
$nodes = $document.SelectNodes("/*[local-name()='BatteryReport']/*[local-name()='Batteries']/*[local-name()='Battery']")
if ($nodes.Count -gt 16) { throw 'Battery count exceeds the inventory bound.' }
$batteries = @($nodes | ForEach-Object {
    $design = Number $_ 'DesignCapacity' $false
    $full = Number $_ 'FullChargeCapacity' $false
    $health = $null
    if ($null -ne $design -and $null -ne $full) {
        $health = [math]::Round(([double]$full / [double]$design) * 100, 2, [MidpointRounding]::AwayFromZero)
    }
    [PSCustomObject]@{
        name = Field $_ 'Id'
        manufacturer = Field $_ 'Manufacturer'
        serial = Field $_ 'SerialNumber'
        design_capacity_mwh = $design
        full_charge_capacity_mwh = $full
        health_percent = $health
        cycle_count = Number $_ 'CycleCount' $true
        source = 'powercfg-batteryreport'
    }
})
$status = 'unavailable'
if (@($batteries | Where-Object { $null -ne $_.health_percent }).Count -gt 0) { $status = 'available' }
$result = [PSCustomObject]@{ source = 'powercfg-batteryreport'; status = $status; batteries = $batteries }
$json = $result | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($OutputPath, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "Battery health collection: $status ($($batteries.Count) batteries)"
