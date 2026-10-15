param([string]$UnityEditor = 'C:/Program Files/Unity/Hub/Editor/6000.6.2f1/Editor')
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$taskOutput = Join-Path $repo 'output/visualizer-boundary-check'
New-Item -ItemType Directory -Force -Path $taskOutput | Out-Null
$data = Join-Path $UnityEditor 'Data'
$sdk = Get-ChildItem (Join-Path $data 'DotNetSdk/sdk') -Directory | Select-Object -First 1
$compiler = Join-Path $sdk.FullName 'Roslyn/bincore/csc.dll'
$monoApi = Join-Path $data 'MonoBleedingEdge/lib/mono/4.8-api'
$newtonsoft = Join-Path $data 'Managed/Newtonsoft.Json.dll'
$nunit = Join-Path $data 'Resources/PackageManager/BuiltInPackages/com.unity.ext.nunit/net472/unity-custom/nunit.framework.dll'
$core = Join-Path $data 'Managed/UnityEngine/UnityEngine.CoreModule.dll'
$runner = Join-Path $taskOutput 'BoundaryRunner.cs'
@'
using System;
using CG4002.Tests;
class BoundaryRunner {
    static int Main() {
        var test = new EngineWireCodecTests();
        int count = 0;
        foreach (var method in typeof(EngineWireCodecTests).GetMethods()) {
            if (method.GetCustomAttributes(typeof(NUnit.Framework.TestAttribute), false).Length == 0) continue;
            try { method.Invoke(test, null); count++; }
            catch (Exception e) { Console.Error.WriteLine(method.Name + ": " + (e.InnerException ?? e)); return 1; }
        }
        Console.WriteLine(count + " visualizer wire-boundary tests passed."); return 0;
    }
}
'@ | Set-Content -LiteralPath $runner -Encoding utf8
$source = Join-Path $repo 'visualizer/Assets/CG4002'
$exe = Join-Path $taskOutput 'BoundaryTests.exe'
$argsList = @('/nologo', '/noconfig', '/nostdlib+', '/langversion:9', '/target:exe', "/out:$exe")
$references = @('mscorlib.dll', 'System.dll', 'System.Core.dll') | ForEach-Object { Join-Path $monoApi $_ }
$references += @((Join-Path $monoApi 'Facades/netstandard.dll'), $newtonsoft, $nunit, $core)
$argsList += $references | ForEach-Object { "/reference:$_" }
$argsList += @((Join-Path $source 'Scripts/Runtime/Combat/CombatTypes.cs'),
    (Join-Path $source 'Scripts/Runtime/State/Messages.cs'),
    (Join-Path $source 'Scripts/Runtime/Net/EngineWireCodec.cs'),
    (Join-Path $source 'Tests/PlayMode/EngineWireCodecTests.cs'), $runner)
& (Join-Path $data 'DotNetSdk/dotnet.exe') $compiler @argsList
if ($LASTEXITCODE -ne 0) { throw 'Boundary compilation failed' }
foreach ($reference in @($newtonsoft, $nunit, $core,
    (Join-Path $data 'Managed/UnityEngine/UnityEngine.ScriptingModule.dll'))) {
    Copy-Item -LiteralPath $reference -Destination $taskOutput -Force
}
& (Join-Path $data 'MonoBleedingEdge/bin/mono.exe') $exe
if ($LASTEXITCODE -ne 0) { throw 'Boundary tests failed' }
Write-Host 'This checks the actual wire adapter with Unity types. It is not a Unity PlayMode or phone test.'
