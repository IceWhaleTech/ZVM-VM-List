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
# sessions. Use the same English entry point when resuming a non-English batch.
$batchFirstUrl = . $resolvedFidoPath -Win "11" -Rel $batchRequest.version -Ed "Home/Pro/Edu" `
    -Lang "^English$" -Arch "^x64$" -PlatformArch "x64" -GetUrl
if (-not ($batchFirstUrl -is [string]) -or -not $batchFirstUrl.StartsWith("https://")) {
    throw "Fido did not return the first Microsoft ISO URL."
}
if ($batchLanguages | Where-Object { $_.code -eq "en-us" }) {
    $batchResults["en-us"] = $batchFirstUrl
}

foreach ($batchLanguage in ($batchLanguages | Where-Object { $_.code -ne "en-us" })) {
    # Pace the batch rather than issuing a burst of download-link requests.
    Start-Sleep -Seconds 20
    $batchMatches = @($languages | Where-Object { $_.Name -match $batchLanguage.pattern })
    if ($batchMatches.Count -ne 1) { throw "Microsoft language selection is not unique: $($batchLanguage.code)" }
    # The reviewed Fido pin puts the x64 product first (session 0), ARM second.
    # Avoid failing an amd64 publication because an unrequested ARM SKU is denied.
    $batchX64Language = [PSCustomObject]@{
        Data = @($batchMatches[0].Data | Where-Object { $_.SessionIndex -eq 0 })
    }
    if ($batchX64Language.Data.Count -ne 1) { throw "Microsoft did not return one x64 SKU for $($batchLanguage.code)" }
    $batchOptions = @(Get-Windows-Download-Links $winVersionId $winReleaseId $winEditionId $batchX64Language)
    $batchX64 = @($batchOptions | Where-Object { $_.Arch -eq "x64" })
    if ($batchX64.Count -ne 1) { throw "Fido did not return one x64 ISO for $($batchLanguage.code)" }
    $batchResults[$batchLanguage.code] = $batchX64[0].Url
}

$batchResults | ConvertTo-Json -Compress
