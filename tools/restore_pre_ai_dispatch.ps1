param(
    [switch]$WhatIf
)

$workspacePath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backupPath = Join-Path $workspacePath 'backups\pre_ai_dispatch_20260902'

$filesToRestore = @(
    @{ Source = 'server.py'; Target = 'server.py' },
    @{ Source = 'requirements.txt'; Target = 'requirements.txt' },
    @{ Source = 'README.md'; Target = 'README.md' },
    @{ Source = 'index.html'; Target = 'web\index.html' },
    @{ Source = 'app.js'; Target = 'web\app.js' },
    @{ Source = 'style.css'; Target = 'web\style.css' }
)

foreach ($fileMapping in $filesToRestore) {
    $sourcePath = Join-Path $backupPath $fileMapping.Source
    $targetPath = Join-Path $workspacePath $fileMapping.Target
    if (-not (Test-Path -LiteralPath $sourcePath)) {
        throw "Missing rollback file: $sourcePath"
    }
    Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force -WhatIf:$WhatIf
}

$aiModulePath = Join-Path $workspacePath 'ai_dispatcher.py'
if (Test-Path -LiteralPath $aiModulePath) {
    Remove-Item -LiteralPath $aiModulePath -Force -WhatIf:$WhatIf
}

if ($WhatIf) {
    Write-Host 'Rollback dry run complete. No files were changed.'
} else {
    Write-Host 'AI task assistant changes were rolled back successfully.'
}
