function Get-UiBuildDirectory {
    param([string]$ProjectRoot, [string]$Override)
    $directory = $Override
    if (-not $directory) {
        $file = Join-Path $ProjectRoot '.data\center\ui-build.local.json'
        if (-not (Test-Path -LiteralPath $file)) { $file = Join-Path $ProjectRoot 'config\ui-build.json' }
        $directory = [string](Get-Content -LiteralPath $file -Raw | ConvertFrom-Json).dist_dir
    }
    if (-not $directory -or [IO.Path]::IsPathRooted($directory) -or $directory -notmatch '^\.next(?:-[a-zA-Z0-9_-]+)?$') {
        throw 'Invalid UI build directory: expected a .next-* folder in the project root.'
    }
    return $directory
}
