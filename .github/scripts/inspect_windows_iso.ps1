param(
    [Parameter(Mandatory = $true)]
    [string]$ISOPath
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$resolved = (Resolve-Path -LiteralPath $ISOPath).Path
$mounted = $false
try {
    $disk = Get-DiskImage -ImagePath $resolved -ErrorAction SilentlyContinue
    if ($disk -and $disk.Attached) {
        throw "ISO is already mounted; refusing to alter an existing mount."
    }
    $disk = Mount-DiskImage -ImagePath $resolved -StorageType ISO -Access ReadOnly -PassThru
    $mounted = $true
    $volume = $disk | Get-Volume | Where-Object { $_.DriveLetter } | Select-Object -First 1
    if (-not $volume) {
        throw "Mounted ISO has no drive letter."
    }
    $root = "$($volume.DriveLetter):\"
    $imagePath = Join-Path $root "sources\install.wim"
    if (-not (Test-Path -LiteralPath $imagePath)) {
        $imagePath = Join-Path $root "sources\install.esd"
    }
    if (-not (Test-Path -LiteralPath $imagePath)) {
        throw "ISO does not contain install.wim or install.esd."
    }
    $images = @(Get-WindowsImage -ImagePath $imagePath)
    $image = Get-WindowsImage -ImagePath $imagePath -Index 1
    [PSCustomObject]@{
        build = $image.Version.ToString()
        revision = $image.SPBuild
        architecture = $image.Architecture.ToString()
        languages = @($image.Languages | ForEach-Object { $_.ToString() })
        image_count = $images.Count
    } | ConvertTo-Json -Compress
}
finally {
    if ($mounted) {
        Dismount-DiskImage -ImagePath $resolved | Out-Null
    }
}
