"""Release guard checks only publishable files, without disclosing secret values."""

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/check_public.py"
spec = importlib.util.spec_from_file_location("kudos_public_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def repo(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_gitignored_private_material_is_not_a_release_candidate(tmp_path):
    root = repo(tmp_path)
    (root / ".gitignore").write_text("config.toml\nartifacts/\n")
    (root / "config.toml").write_text("private values")
    (root / "artifacts").mkdir()
    (root / "artifacts/local.json").write_text("private")
    (root / "README.md").write_text("# Public source\n")
    errors, count = gate.check(root)
    assert errors == [] and count == 2


def test_tracked_ignored_secrets_still_fail(tmp_path):
    root = repo(tmp_path)
    (root / ".gitignore").write_text(".env\n")
    (root / ".env").write_text("PRIVATE_TEST=not-a-real-secret\n")
    subprocess.run(["git", "-C", str(root), "add", "-f", ".env"], check=True)
    errors, _ = gate.check(root)
    assert any(".env: private artifact type" in error for error in errors)


def test_generic_examples_are_allowed_but_private_paths_and_deny_values_fail(tmp_path):
    root = repo(tmp_path)
    (root / "example.md").write_text("/home/demo/project\n")
    (root / "bad.md").write_text("/home/" + "private-owner/file\nPRIVATE_SENTINEL\n")
    errors, _ = gate.check(root, ("PRIVATE_SENTINEL",))
    assert any("bad.md: private home path" in error for error in errors)
    assert any("bad.md: private deny-list match" in error for error in errors)
    assert all("PRIVATE_SENTINEL" not in error for error in errors)
    assert all("example.md" not in error for error in errors)


def test_doc_image_links_must_exist(tmp_path):
    root = repo(tmp_path)
    (root / "README.md").write_text("![Demo](preview.png)\n[Guide](guide.md)\n")
    errors, _ = gate.check(root)
    assert len(errors) == 2
    (root / "preview.png").write_bytes(b"synthetic non-image sentinel")
    (root / "guide.md").write_text("# Guide")
    assert gate.check(root)[0] == []


def test_symlink_sources_are_rejected(tmp_path):
    root = repo(tmp_path)
    (root / "source.md").write_text("# Source")
    (root / "linked.md").symlink_to(root / "source.md")
    errors, _ = gate.check(root)
    assert any("linked.md: symlink" in error for error in errors)
