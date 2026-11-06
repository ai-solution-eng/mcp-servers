"""Policy: identity -> role -> source paths, and how it fails.

The failure direction is the important part: ABSENT means "not configured"
(no filtering), PRESENT-but-broken means DENY EVERYTHING (never fail open).
"""

from pathlib import Path

import pytest

from skills_mcp import policy as policy_mod

GOOD = """
default:
  paths: [skills]
roles:
  team:
    members: [bryan, alice]
    paths: [skills]
  demo:
    members: [bryan]
    paths: [skills, skills-demo]
"""


def test_parse_reads_default_and_roles() -> None:
    parsed = policy_mod.parse_policy(GOOD)
    assert parsed.default_paths == ("skills",)
    assert {role.name for role in parsed.roles} == {"team", "demo"}


def test_member_match_is_case_insensitive_and_email_aware() -> None:
    parsed = policy_mod.parse_policy(GOOD)
    assert parsed.role_for("bryan").name == "team"          # first matching role wins
    assert parsed.role_for("ALICE").name == "team"
    assert parsed.role_for("bryan@corp").name == "team"     # local part matches
    assert parsed.role_for("bryan@corp") is not None


def test_unknown_identity_gets_default_paths() -> None:
    parsed = policy_mod.parse_policy(GOOD)
    assert parsed.role_for("mallory") is None
    assert parsed.paths_for("mallory") == ("skills",)


def test_anonymous_gets_default_paths() -> None:
    assert policy_mod.parse_policy(GOOD).paths_for(None) == ("skills",)


def test_broken_yaml_denies_everything() -> None:
    with pytest.raises(policy_mod.PolicyError):
        policy_mod.parse_policy("roles: [unclosed")


def test_non_mapping_denies_everything() -> None:
    with pytest.raises(policy_mod.PolicyError):
        policy_mod.parse_policy("- just\n- a list\n")


def test_missing_file_means_no_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLS_POLICY_FILE", str(tmp_path / "nope.yaml"))
    assert policy_mod.current_policy() is None


def test_broken_file_denies_everything(tmp_path: Path, monkeypatch) -> None:
    bad = tmp_path / "access.yaml"
    bad.write_text("roles: {team: {members: [a], paths: nope}}", encoding="utf-8")
    monkeypatch.setenv("SKILLS_POLICY_FILE", str(bad))
    active = policy_mod.current_policy()
    assert active is not None
    assert active.error is not None
    assert active.paths_for("anyone") == ()


def test_file_is_re_read_when_it_changes(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "access.yaml"
    path.write_text("default:\n  paths: [skills]\n", encoding="utf-8")
    monkeypatch.setenv("SKILLS_POLICY_FILE", str(path))
    assert policy_mod.current_policy().default_paths == ("skills",)

    path.write_text("default:\n  paths: [other]\n", encoding="utf-8")
    import os

    os.utime(path, (path.stat().st_atime + 5, path.stat().st_mtime + 5))
    assert policy_mod.current_policy().default_paths == ("other",)
