param([Parameter(Mandatory = $true)][string]$Compiler)

$ErrorActionPreference = 'Stop'
$testSource = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'action_quality_compile_test.cpp'))
$firstBlock = $null
foreach ($player in @(1, 2)) {
    $sketch = Join-Path $PSScriptRoot "../2 players/p${player}_right_wrist/p${player}_right_wrist.ino"
    $source = [IO.File]::ReadAllText($sketch)
    $match = [regex]::Match($source, '(?s)// ===== ACTION QUALITY BEGIN =====(.*?)// ===== ACTION QUALITY END =====')
    if (!$match.Success) { throw "Missing inline action checks in player $player sketch" }
    $block = $match.Groups[1].Value
    if ($null -ne $firstBlock -and $block -cne $firstBlock) { throw 'Player action checks differ' }
    $firstBlock = $block
    $translationUnit = "#include <stdint.h>`n" + $block + "`n" + $testSource
    $translationUnit | & $Compiler -x c++ -std=c++17 -fsyntax-only -
    if ($LASTEXITCODE -ne 0) { throw "Player $player action quality regression checks failed" }
    Write-Output "Player $player inline action quality regression checks passed."
    $selection = [regex]::Match($source, '(?s)// ===== STRIKE SELECTION BEGIN =====(.*?)// ===== STRIKE SELECTION END =====')
    if (!$selection.Success) { throw "Missing strike selection block in player $player sketch" }
    $selectionCode = $selection.Groups[1].Value.Replace('TemplateMatch chooseStrikeTemplate(', 'constexpr TemplateMatch chooseStrikeTemplate(')
    $selectionTests = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'strike_selection_compile_test.cpp'))
    $selectionTests.Replace('// INSERT STRIKE SELECTION HERE', $selectionCode) | & $Compiler -x c++ -std=c++17 -fsyntax-only -
    if ($LASTEXITCODE -ne 0) { throw "Player $player strike selection regression checks failed" }
    Write-Output "Player $player strike selection regression checks passed."
}
