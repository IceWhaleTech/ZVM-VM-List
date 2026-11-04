param(
    [Parameter(Mandatory = $true)]
    [string]$FidoPath,
    [Parameter(Mandatory = $true)]
    [string]$RequestBase64
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$batchRequest = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($RequestBase64)) | ConvertFrom-Json
$batchLanguages = @($batchRequest.languages)
if ($batchLanguages.Count -eq 0) { throw "No Windows languages requested." }
$resolvedFidoPath = (Resolve-Path -LiteralPath $FidoPath).Path
$batchResults = @{}

# Dot-sourcing the pinned Fido CLI retains its SKU data and architecture-specific
# sessions. Reinitializing that data for every language can be rejected by Sentinel.
$batchFirstUrl = . $resolvedFidoPath -Win "11" -Rel $batchRequest.version -Ed "Home/Pro/Edu" `
    -Lang $batchLanguages[0].pattern -Arch "^x64$" -PlatformArch "x64" -GetUrl
if (-not ($batchFirstUrl -is [string]) -or -not $batchFirstUrl.StartsWith("https://")) {
    throw "Fido did not return the first Microsoft ISO URL."
}
$batchResults[$batchLanguages[0].code] = $batchFirstUrl

foreach ($batchLanguage in ($batchLanguages | Select-Object -Skip 1)) {
    $batchMatches = @($languages | Where-Object { $_.Name -match $batchLanguage.pattern })
    if ($batchMatches.Count -ne 1) { throw "Microsoft language selection is not unique: $($batchLanguage.code)" }
    $batchOptions = @(Get-Windows-Download-Links $winVersionId $winReleaseId $winEditionId $batchMatches[0])
    $batchX64 = @($batchOptions | Where-Object { $_.Arch -eq "x64" })
    if ($batchX64.Count -ne 1) { throw "Fido did not return one x64 ISO for $($batchLanguage.code)" }
    $batchResults[$batchLanguage.code] = $batchX64[0].Url
}

$batchResults | ConvertTo-Json -Compress
