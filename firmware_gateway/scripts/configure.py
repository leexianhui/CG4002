"""Keep key material out of compiler arguments, source files, and build output."""
import os
import re
from pathlib import Path

Import("env")
if env.subst("$PIOENV") != "native_test":
    target = Path(env.subst("$BUILD_DIR")) / "private"
    target.mkdir(parents=True, exist_ok=True)
    # Only hubs use BLE. The other nodes do not need the relay key.
    if "hub" in env.subst("$PIOENV"):
        key = os.environ.get("CG_GATEWAY_PSK_HEX", "")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", key):
            raise RuntimeError("Set CG_GATEWAY_PSK_HEX to the relay's 32-hex-digit PSK")
        header = target / "gateway_secret.h"
        content = '#define CG_GATEWAY_PSK_HEX "' + key + '"\n'
        if not header.exists() or header.read_text() != content:
            header.write_text(content)
    env.Append(CPPPATH=[str(target)])
