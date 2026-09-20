"""Every workflow must parse the way GITHUB parses it, not the way PyYAML does.

🔴 THE BUG THIS CLOSES. `yaml.safe_load` silently keeps the LAST of a duplicate
key. GitHub rejects the file outright. On 2026-09-20 an edit left
`required: false` twice under one input, `safe_load` reported "yaml ok", and
self-heal was DEAD for three hours — every push produced a failed run named
after the file path, which is how GitHub reports a workflow it cannot read.

The cost was not the downtime. It was that the approve-and-build loop shipped
that same afternoon could never have worked, and the check that was supposed to
prove the workflow was fine had reported success. A validator that cannot fail
is not a validator.
"""
import pathlib

import pytest
import yaml

WORKFLOWS = sorted((pathlib.Path(__file__).resolve().parent.parent
                    / ".github" / "workflows").glob("*.yml"))


class _Strict(yaml.SafeLoader):
    """SafeLoader that REFUSES duplicate keys, as GitHub's parser does."""


def _no_duplicate_keys(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.constructor.SafeConstructor.construct_mapping(loader, node, deep)


_Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                        _no_duplicate_keys)


def test_there_are_workflows_to_check():
    """An empty glob would make every test below pass vacuously."""
    assert len(WORKFLOWS) >= 10, WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_duplicate_keys(path):
    yaml.load(path.read_text(), Loader=_Strict)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_declares_a_name(path):
    """GitHub falls back to the FILE PATH as the run name when it cannot read
    the file. A run listed as `.github/workflows/x.yml` is the tell."""
    doc = yaml.load(path.read_text(), Loader=_Strict)
    assert doc.get("name"), f"{path.name} has no name:"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_has_a_trigger_and_a_job(path):
    doc = yaml.load(path.read_text(), Loader=_Strict)
    assert doc.get(True) or doc.get("on"), f"{path.name} has no trigger"
    assert doc.get("jobs"), f"{path.name} has no jobs"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_dispatch_inputs_are_within_githubs_limit(path):
    """GitHub allows at most 10 workflow_dispatch inputs and silently rejects
    the file beyond that."""
    doc = yaml.load(path.read_text(), Loader=_Strict)
    wd = (doc.get(True) or doc.get("on") or {})
    wd = wd.get("workflow_dispatch") if isinstance(wd, dict) else None
    if isinstance(wd, dict) and wd.get("inputs"):
        assert len(wd["inputs"]) <= 10, f"{path.name} declares too many inputs"


def test_the_strict_loader_actually_catches_a_duplicate():
    """A guard that cannot fail is not a guard — and the lenient loader that
    hid this bug would pass this same input."""
    doc = "a:\n  b: 1\n  b: 2\n"
    assert yaml.safe_load(doc) == {"a": {"b": 2}}, "safe_load is still lenient"
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load(doc, Loader=_Strict)
