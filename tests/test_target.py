"""Target value type: canonical key, parse round-trip, and validation."""

from __future__ import annotations

import pytest

from husk.target import Target


def test_repo_and_org_keys():
    assert Target.repo("paulgessinger/husk-test").key == "repo:paulgessinger/husk-test"
    assert Target.org("acts-project").key == "org:acts-project"


def test_key_parse_round_trip():
    for t in (Target.repo("owner/name"), Target.org("acts-project")):
        assert Target.parse(t.key) == t
        assert str(t) == t.key


def test_parse_keeps_slash_in_repo_name():
    # A repo key carries exactly one ':'; the '/' in owner/name must survive.
    t = Target.parse("repo:paulgessinger/husk-test")
    assert t.kind == "repo" and t.name == "paulgessinger/husk-test"


@pytest.mark.parametrize(
    "bad",
    [
        lambda: Target("group", "x"),  # unknown kind
        lambda: Target.repo("no-slash"),  # repo needs owner/name
        lambda: Target.org(""),  # empty name
        lambda: Target.parse("acts-project"),  # missing kind:name separator
    ],
)
def test_validation_rejects_malformed(bad):
    with pytest.raises(ValueError):
        bad()
