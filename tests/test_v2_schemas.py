from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from runner_app.resources import load_schema


@pytest.mark.parametrize("name", ["mailbox", "project", "environment", "approval", "result"])
def test_packaged_contract_is_byte_exact_design_copy(name):
    root = Path(__file__).resolve().parents[1]
    assert (root / f"schemas/v2-{name}.schema.json").read_bytes() == (
        root / f"design/github-native-v2/contracts/{name}.schema.json").read_bytes()
    schema = load_schema(f"v2-{name}")
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].startswith(f"urn:cdr:{name}:")


def test_v1_schema_loader_remains_available():
    assert load_schema("command")["properties"]["dispatch"]["enum"] == ["DRAFT", "READY"]
