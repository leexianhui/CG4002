"""Prepare an isolated Unity smoke project using the installed editor's local packages.

Never opens or upgrades the imported project's scene/assets/package lock.
"""

import json
import shutil
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[2]
editor = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "C:/Program Files/Unity/Hub/Editor/6000.6.2f1/Editor")
dest = repo / "output" / "unity-integration-validation"
source = repo / "visualizer" / "Assets" / "CG4002"
assets = dest / "Assets"
assets.mkdir(parents=True, exist_ok=True)
shutil.copytree(source / "Scripts" / "Runtime", assets / "Runtime", dirs_exist_ok=True)
(assets / "Editor").mkdir(exist_ok=True)
shutil.copy2(source / "Scripts" / "Editor" / "OnlineIntegrationSetup.cs", assets / "Editor")
tests = assets / "Tests"
tests.mkdir(exist_ok=True)
for name in ("CG4002.Tests.PlayMode.asmdef", "EngineWireCodecTests.cs", "StateEngineTests.cs",
             "OnlineEngineConnectionTests.cs", "PhoneCalibrationTests.cs"):
    shutil.copy2(source / "Tests" / "PlayMode" / name, tests / name)

package_root = editor / "Data" / "Resources" / "PackageManager"
dependencies = {}
for name in ("com.unity.inputsystem", "com.unity.xr.arfoundation", "com.unity.xr.core-utils",
             "com.unity.xr.management", "com.unity.xr.legacyinputhelpers"):
    archive = next((package_root / "Editor").glob(name + "-*.tgz"))
    dependencies[name] = "file:" + archive.as_posix()
for name in ("com.unity.editorcoroutines", "com.unity.ext.nunit", "com.unity.mathematics",
             "com.unity.ugui", "com.unity.test-framework"):
    dependencies[name] = "file:" + (package_root / "BuiltInPackages" / name).as_posix()
# Newtonsoft is not an editor built-in: borrow it from the real project's package cache, which
# exists once visualizer/ has been opened in Unity at least once.
cache = repo / "visualizer" / "Library" / "PackageCache"
newtonsoft = next(cache.glob("com.unity.nuget.newtonsoft-json@*"), None)
if newtonsoft is None:
    sys.exit(f"Open visualizer/ in Unity once first: no Newtonsoft package under {cache}")
dependencies["com.unity.nuget.newtonsoft-json"] = "file:" + newtonsoft.as_posix()
(dest / "Packages").mkdir(exist_ok=True)
(dest / "Packages" / "manifest.json").write_text(
    json.dumps({"dependencies": dependencies}, indent=2), encoding="utf-8")
(dest / "ProjectSettings").mkdir(exist_ok=True)
(dest / "ProjectSettings" / "ProjectVersion.txt").write_text(
    "m_EditorVersion: " + editor.parent.name + "\n", encoding="utf-8")
print(dest)
