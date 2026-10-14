"""Run real Unity clients against a private mock stack; no real token/boards required.

Run from comms/ after scripts/prepare_unity_validation.py. Uses the isolated project only.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

from cg_comms.config import EngineSettings
from cg_comms.logging_setup import configure_logging
from cg_comms.tools.integration_mock import run_stack


async def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    project = repo / "output" / "unity-integration-validation"
    editor = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "C:/Program Files/Unity/Hub/Editor/6000.6.2f1/Editor/Unity.exe")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = secrets.token_urlsafe(32)
    settings = EngineSettings(_env_file=None, host="127.0.0.1", port=port,
                              use_tls=False, bearer_token=token)
    configure_logging("unity-online-smoke", project / "mock-logs", console_level=40)
    stack = asyncio.create_task(run_stack(settings, "127.0.0.1"))
    result = project / "online-results.xml"
    # Unique result path prevents a stale success from being mistaken for this run.
    result = result.with_name("online-results-" + secrets.token_hex(4) + ".xml")
    env = dict(os.environ, CG_UNITY_TEST_URL=f"ws://127.0.0.1:{port}", CG_UNITY_TEST_TOKEN=token)
    process = None
    try:
        process = subprocess.Popen([
            str(editor), "-batchmode", "-nographics", "-projectPath", str(project),
            "-runTests", "-testPlatform", "PlayMode", "-testResults", str(result),
            "-logFile", str(project / "online-editor.log"),
        ], env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = asyncio.get_running_loop().time() + 240
        while process.poll() is None:
            if stack.done():
                await stack
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("Unity validation exceeded four minutes; see online-editor.log")
            await asyncio.sleep(0.2)
        if process.returncode != 0 or not result.exists():
            raise RuntimeError("Unity validation failed; see online-editor.log")
        tests = ElementTree.parse(result).getroot()
        assert tests.attrib["failed"] == "0", str(result)
        network = [case for case in tests.iter("test-case")
                   if case.attrib.get("methodname") ==
                   "TwoUnityClientsReceiveCombatAndRecoverAfterReconnect"]
        assert len(network) == 1 and network[0].attrib["result"] == "Passed", str(result)
        print(f"Unity: {tests.attrib['passed']} passed; two Unity clients exercised the mock stack.")
        print(result)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait)
        stack.cancel()
        await asyncio.gather(stack, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
