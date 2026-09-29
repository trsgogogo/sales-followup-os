"""Static validation; importing into a target n8n instance is a separate check."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = list((root / "workflows" / "n8n").glob("*.json"))
assert len(files) == 4, "Expected four workflow templates"
for path in files:
    flow = json.loads(path.read_text())
    assert flow["active"] is False
    nodes = flow["nodes"]
    names = {n["name"] for n in nodes}
    assert len(names) == len(nodes)
    assert len({n["id"] for n in nodes}) == len(nodes)
    for source, outputs in flow["connections"].items():
        assert source in names
        for branches in outputs.values():
            for branch in branches:
                for edge in branch:
                    assert edge["node"] in names
    for n in nodes:
        assert n["type"].startswith("n8n-nodes-base.")
        assert not n.get("credentials"), "Credentials must be configured in the target instance"
    print("Validated:", path.name)
