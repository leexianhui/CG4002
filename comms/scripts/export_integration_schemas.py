"""Export only the additive online-boundary schemas; run from comms/."""

import json

from cg_comms.config import find_repo_root
from cg_comms.messages.models import StateSnapshot, VisualizerReady

if __name__ == "__main__":
    destination = find_repo_root() / "shared" / "schemas"
    for model, name in ((StateSnapshot, "state_snapshot"), (VisualizerReady, "visualizer_ready")):
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["properties"]["version"] = {"const": 1}
        schema["required"] = list(dict.fromkeys(["type", "version", *schema["required"]]))
        (destination / f"{name}.schema.json").write_text(
            json.dumps(schema, indent=2) + "\n", encoding="utf-8")
