"""Install-environment venv site-packages must be recognized as Hermes-owned
PYTHONPATH entries and stripped from child processes (aider/llm crash bug,
fleet 261028)."""
from pathlib import Path
import pytest


def _mkvenv(base: Path, pyver: str = "python3.14") -> Path:
    venv = base / "venv"
    sp = venv / "lib" / pyver / "site-packages"
    sp.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/local/bin\n")
    return sp


def test_install_env_site_packages_stripped(tmp_path, monkeypatch):
    import tools.environments.local as local
    import tools.environments.local_pythonpath as lp

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    fake_installs = tmp_path / "installs"
    env_sp = _mkvenv(fake_installs / "abc123" / "environments" / "0d93")
    # not the running interpreter's own venv
    monkeypatch.setattr(local, "_in_venv", False)
    monkeypatch.setattr(local, "_hermes_site_packages", [])
    monkeypatch.setattr(local, "_hermes_repo_root_aliases", (repo_root,))
    monkeypatch.setattr(lp, "_validated_runtime_venv", lambda env: None)
    import pm.environments as pme
    monkeypatch.setattr(pme, "installs_root", lambda: fake_installs)

    env = {"PYTHONPATH": str(env_sp)}
    lp._strip_hermes_owned_pythonpath(env)
    assert "PYTHONPATH" not in env, f"install-env site-packages leaked: {env}"
