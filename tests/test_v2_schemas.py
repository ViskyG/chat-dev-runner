import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from runner_app.resources import load_schema


@pytest.mark.parametrize("name", ["mailbox", "project", "environment", "approval", "result"])
def test_packaged_contract_preserves_original_design_with_additive_handoff(name):
    root = Path(__file__).resolve().parents[1]
    schema = load_schema(f"v2-{name}")
    original = root / f"design/github-native-v2/contracts/{name}.schema.json"
    if name == "mailbox":
        baseline = json.loads(original.read_bytes())
        unchanged = copy.deepcopy(schema)
        unchanged["title"] = baseline["title"]
        request = unchanged["properties"]["request"]
        assert request["properties"].pop("handoff")["$id"] == "urn:cdr:handoff:1"
        request["properties"]["action"]["enum"].remove("DELIVER_HANDOFF")
        extension = request["allOf"].pop()
        assert extension["if"]["properties"]["action"]["const"] == "DELIVER_HANDOFF"
        for branch in request["allOf"]:
            branch["then"]["not"]["anyOf"].remove({"required": ["handoff"]})
        assert unchanged == baseline
    else:
        assert (root / f"schemas/v2-{name}.schema.json").read_bytes() == original.read_bytes()
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].startswith(f"urn:cdr:{name}:")


def test_v1_schema_loader_remains_available():
    assert load_schema("command")["properties"]["dispatch"]["enum"] == ["DRAFT", "READY"]
