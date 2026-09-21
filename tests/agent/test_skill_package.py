import os

import pytest

from agent.prompt_builder import _build_skills_manifest
from agent.skill_package import skill_package_digest


def test_root_level_package_file_invalidates_manifest_and_digest(tmp_path):
    skill = tmp_path / "demo"
    skill.mkdir()
    skill_md = skill / "SKILL.md"
    skill_md.write_text("---\nname: demo\ndescription: demo\n---\n")
    extra = skill / "policy.json"
    extra.write_text('{"v": 1}')

    before_manifest = _build_skills_manifest(tmp_path)
    before_digest = skill_package_digest(skill_md)
    extra.write_text('{"v": 2, "changed": true}')

    assert _build_skills_manifest(tmp_path) != before_manifest
    assert skill_package_digest(skill_md) != before_digest


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_package_directory_symlink_is_hashed_but_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_text("first")
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    skill_md = skill / "SKILL.md"
    skill_md.write_text("---\nname: demo\ndescription: demo\n---\n")
    (skill / "references").symlink_to(outside, target_is_directory=True)

    digest = skill_package_digest(skill_md)
    manifest = _build_skills_manifest(tmp_path / "skills")
    (outside / "payload.txt").write_text("second")

    assert skill_package_digest(skill_md) == digest
    assert _build_skills_manifest(tmp_path / "skills") == manifest