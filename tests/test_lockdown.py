"""Tests for pypi-lockdown."""

from __future__ import annotations

import configparser
import os
import subprocess
from typing import TYPE_CHECKING

import pytest
import tomlkit

from pypi_lockdown.configure import (
    _MARKER,
    _ensure_userinfo,
    _keyring_backend_available,
    _resolve_keyring_cli,
    _strip_userinfo,
    _write_pip_config,
    _write_pyproject_hatch,
    _write_pyproject_poetry,
    _write_pyproject_uv,
    _write_uv_config,
    configure,
    detect_index_url,
    status,
    undo,
)
from pypi_lockdown.verify import _looks_like_auth_failure, verify

if TYPE_CHECKING:
    from pathlib import Path
    from pathlib import Path as _Path


# ---------------------------------------------------------------------------
# HTTPS enforcement
# ---------------------------------------------------------------------------


class TestHTTPSEnforcement:
    def test_rejects_http_url(self) -> None:
        with pytest.raises(SystemExit):
            configure("http://pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/")

    def test_rejects_ftp_url(self) -> None:
        with pytest.raises(SystemExit):
            configure("ftp://example.com/simple/")

    def test_accepts_https_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Remove VIRTUAL_ENV/CONDA_PREFIX so it falls back to user scope
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("CONDA_PREFIX", raising=False)
        # Redirect user config paths to tmp
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        # Should not raise
        configure("https://pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/")

        pip_conf = tmp_path / "pip" / "pip.conf"
        assert pip_conf.exists()
        cfg = configparser.ConfigParser()
        cfg.read(pip_conf)
        assert (
            cfg.get("global", "index-url")
            == "https://pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/"
        )


# ---------------------------------------------------------------------------
# pip config writing
# ---------------------------------------------------------------------------


class TestPipConfigWriting:
    def test_creates_config(self, tmp_path: Path) -> None:
        path = tmp_path / "pip.conf"
        _write_pip_config(path, "https://example.com/simple/")
        assert path.exists()

        cfg = configparser.ConfigParser()
        cfg.read(path)
        assert cfg.get("global", "index-url") == "https://example.com/simple/"

    def test_preserves_existing_sections(self, tmp_path: Path) -> None:
        path = tmp_path / "pip.conf"
        path.write_text("[install]\ntimeout = 60\n")

        _write_pip_config(path, "https://example.com/simple/")

        cfg = configparser.ConfigParser()
        cfg.read(path)
        assert cfg.get("global", "index-url") == "https://example.com/simple/"
        assert cfg.get("install", "timeout") == "60"

    def test_sets_subprocess_provider(self, tmp_path: Path) -> None:
        path = tmp_path / "pip.conf"
        _write_pip_config(path, "https://example.com/simple/")
        cfg = configparser.ConfigParser()
        cfg.read(path)
        assert cfg.get("global", "keyring-provider") == "subprocess"


# ---------------------------------------------------------------------------
# uv config writing
# ---------------------------------------------------------------------------


class TestUvConfigWriting:
    def test_creates_config(self, tmp_path: Path) -> None:
        path = tmp_path / "uv.toml"
        _write_uv_config(path, "https://example.com/simple/")
        assert path.exists()
        content = path.read_text()
        assert 'url = "https://__token__@example.com/simple/"' in content
        assert "default = true" in content
        assert 'keyring-provider = "subprocess"' in content

    def test_preserves_existing_userinfo(self, tmp_path: Path) -> None:
        path = tmp_path / "uv.toml"
        _write_uv_config(path, "https://user@example.com/simple/")
        content = path.read_text()
        assert 'url = "https://user@example.com/simple/"' in content


class TestEnsureUserinfo:
    def test_injects_token(self) -> None:
        assert (
            _ensure_userinfo(
                "https://pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/"
            )
            == "https://__token__@pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/"
        )

    def test_preserves_existing_username(self) -> None:
        url = "https://user@pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/"
        assert _ensure_userinfo(url) == url

    def test_preserves_token_username(self) -> None:
        url = (
            "https://__token__@pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/"
        )
        assert _ensure_userinfo(url) == url

    def test_preserves_port(self) -> None:
        assert (
            _ensure_userinfo("https://example.com:8080/simple/")
            == "https://__token__@example.com:8080/simple/"
        )


# ---------------------------------------------------------------------------
# pyproject.toml writers (uv + poetry)
# ---------------------------------------------------------------------------

_FEED_URL = "https://pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/"
_TOKEN_FEED_URL = (
    "https://__token__@pkgs.dev.azure.com/org/proj/_packaging/feed/pypi/simple/"  # noqa: S105
)


class TestPyprojectUv:
    def test_creates_from_scratch(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'mypkg'\n")

        _write_pyproject_uv(path, _FEED_URL)

        content = path.read_text()
        assert 'keyring-provider = "subprocess"' in content
        assert f'url = "{_TOKEN_FEED_URL}"' in content
        assert "default = true" in content
        # Preserves existing content
        assert "name = 'mypkg'" in content

    def test_upserts_existing_uv_section(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            '[project]\nname = "mypkg"\n\n[tool.uv]\nsome-setting = "keep"\n'
        )

        _write_pyproject_uv(path, _FEED_URL)

        content = path.read_text()
        assert 'some-setting = "keep"' in content
        assert 'keyring-provider = "subprocess"' in content
        assert f'url = "{_TOKEN_FEED_URL}"' in content

    def test_updates_existing_default_index(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[tool.uv]\n\n"
            "[[tool.uv.index]]\n"
            'url = "https://old-feed.example.com/simple/"\n'
            "default = true\n"
        )

        _write_pyproject_uv(path, _FEED_URL)

        doc = tomlkit.parse(path.read_text())
        indexes = doc["tool"]["uv"]["index"]
        assert len(indexes) == 1
        assert indexes[0]["url"] == _TOKEN_FEED_URL


class TestPyprojectPoetry:
    def test_creates_from_scratch(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[project]\nname = 'mypkg'\n")

        _write_pyproject_poetry(path, _FEED_URL)

        doc = tomlkit.parse(path.read_text())
        sources = doc["tool"]["poetry"]["source"]
        assert len(sources) == 2
        assert sources[0]["name"] == "internal"
        assert sources[0]["url"] == _FEED_URL
        assert sources[0]["priority"] == "primary"
        assert sources[1]["name"] == "PyPI"
        assert sources[1]["priority"] == "explicit"

    def test_upserts_existing_internal_source(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[[tool.poetry.source]]\n"
            'name = "internal"\n'
            'url = "https://old.example.com/simple/"\n'
            'priority = "primary"\n'
            "\n"
            "[[tool.poetry.source]]\n"
            'name = "PyPI"\n'
            'priority = "explicit"\n'
        )

        _write_pyproject_poetry(path, _FEED_URL)

        doc = tomlkit.parse(path.read_text())
        sources = doc["tool"]["poetry"]["source"]
        assert len(sources) == 2
        assert sources[0]["url"] == _FEED_URL

    def test_adds_missing_pypi_explicit(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[[tool.poetry.source]]\n"
            'name = "internal"\n'
            'url = "https://old.example.com/simple/"\n'
            'priority = "primary"\n'
        )

        _write_pyproject_poetry(path, _FEED_URL)

        doc = tomlkit.parse(path.read_text())
        sources = doc["tool"]["poetry"]["source"]
        assert len(sources) == 2
        assert sources[1]["name"] == "PyPI"
        assert sources[1]["priority"] == "explicit"


class TestPyprojectHatch:
    def test_writes_env_vars_when_hatch_exists(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[project]\nname = 'mypkg'\n\n"
            "[tool.hatch.envs.default]\n"
            'installer = "uv"\n'
        )

        _write_pyproject_hatch(path, _FEED_URL)

        doc = tomlkit.parse(path.read_text())
        env_vars = doc["tool"]["hatch"]["envs"]["default"]["env-vars"]
        assert env_vars["PIP_INDEX_URL"] == _FEED_URL
        assert env_vars["UV_DEFAULT_INDEX"] == _TOKEN_FEED_URL
        # Preserves existing content
        assert doc["tool"]["hatch"]["envs"]["default"]["installer"] == "uv"

    def test_skips_when_no_hatch_section(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        original = "[project]\nname = 'mypkg'\n"
        path.write_text(original)

        _write_pyproject_hatch(path, _FEED_URL)

        assert path.read_text() == original

    def test_upserts_existing_env_vars(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text(
            "[tool.hatch.envs.default.env-vars]\n"
            'PIP_INDEX_URL = "https://old.example.com/simple/"\n'
            'SOME_OTHER_VAR = "keep"\n'
        )

        _write_pyproject_hatch(path, _FEED_URL)

        doc = tomlkit.parse(path.read_text())
        env_vars = doc["tool"]["hatch"]["envs"]["default"]["env-vars"]
        assert env_vars["PIP_INDEX_URL"] == _FEED_URL
        assert env_vars["UV_DEFAULT_INDEX"] == _TOKEN_FEED_URL
        assert env_vars["SOME_OTHER_VAR"] == "keep"

    def test_creates_envs_default_if_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "pyproject.toml"
        path.write_text("[tool.hatch]\n")

        _write_pyproject_hatch(path, _FEED_URL)

        doc = tomlkit.parse(path.read_text())
        env_vars = doc["tool"]["hatch"]["envs"]["default"]["env-vars"]
        assert env_vars["PIP_INDEX_URL"] == _FEED_URL


class TestConfigureProjectScope:
    def test_skips_when_no_pyproject(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # configure should not error when no pyproject.toml exists
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("CONDA_PREFIX", raising=False)
        configure(_FEED_URL)
        # No pyproject.toml should exist
        assert not (tmp_path / "pyproject.toml").exists()

    def test_writes_when_project_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n")
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("CONDA_PREFIX", raising=False)

        configure(_FEED_URL, project_scope=True)

        content = (tmp_path / "pyproject.toml").read_text()
        assert "tool.uv" in content or "keyring-provider" in content
        assert "tool.poetry" in content or "internal" in content

    def test_skips_project_config_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        original = "[project]\nname = 'mypkg'\n"
        (tmp_path / "pyproject.toml").write_text(original)
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("CONDA_PREFIX", raising=False)

        configure(_FEED_URL)

        # Without --project, pyproject.toml is left untouched
        assert (tmp_path / "pyproject.toml").read_text() == original

    def test_hints_project_config_when_pyproject_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n")
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("CONDA_PREFIX", raising=False)

        configure(_FEED_URL)

        assert "--project" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --ci flag
# ---------------------------------------------------------------------------


class TestCiFlag:
    """Tests for the ci=True (non-interactive) code path."""

    def test_ci_skips_pyproject_modification(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        original = "[project]\nname = 'mypkg'\n"
        (tmp_path / "pyproject.toml").write_text(original)
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("CONDA_PREFIX", raising=False)

        configure(_FEED_URL, ci=True)

        # pyproject.toml must be untouched
        assert (tmp_path / "pyproject.toml").read_text() == original
        # pip and uv configs should still be written
        assert (tmp_path / "pip" / "pip.conf").exists()
        assert (tmp_path / "uv" / "uv.toml").exists()

    def test_ci_skips_poetry_instructions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # No pyproject.toml — normally triggers poetry instructions
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("CONDA_PREFIX", raising=False)

        configure(_FEED_URL, ci=True)

        out = capsys.readouterr().out
        assert "poetry source add" not in out

    def test_ci_writes_pip_and_uv_configs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("CONDA_PREFIX", raising=False)

        configure(_FEED_URL, ci=True)

        pip_conf = tmp_path / "pip" / "pip.conf"
        assert pip_conf.exists()
        cfg = configparser.ConfigParser()
        cfg.read(pip_conf)
        assert cfg.get("global", "index-url") == _FEED_URL

        uv_toml = tmp_path / "uv" / "uv.toml"
        assert uv_toml.exists()
        content = uv_toml.read_text()
        assert f'url = "{_TOKEN_FEED_URL}"' in content


class TestConfigureScope:
    """Configuration is always user-global (subprocess keyring provider)."""

    def test_global_default_writes_user_config_with_subprocess(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An active venv must NOT change anything: config is always global.
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "venv"))
        monkeypatch.delenv("CONDA_PREFIX", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "pypi_lockdown.configure._uv_config_user",
            lambda: tmp_path / "uv" / "uv.toml",
        )
        monkeypatch.setattr(
            "pypi_lockdown.configure._pip_config_user",
            lambda: tmp_path / "pip" / "pip.conf",
        )

        configure(_FEED_URL, ci=True)

        # No venv pip.conf should be written -- only user-global
        assert not (tmp_path / "venv" / "pip.conf").exists()
        pip_conf = tmp_path / "pip" / "pip.conf"
        assert pip_conf.exists()
        cfg = configparser.ConfigParser()
        cfg.read(pip_conf)
        assert cfg.get("global", "index-url") == _FEED_URL
        assert cfg.get("global", "keyring-provider") == "subprocess"


# ---------------------------------------------------------------------------
# Auto-detect feed URL
# ---------------------------------------------------------------------------


class TestDetectIndexUrl:
    def test_returns_none_when_no_pyproject(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert detect_index_url() is None

    def test_detects_uv_default_index(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.uv]\n\n"
            "[[tool.uv.index]]\n"
            f'url = "{_TOKEN_FEED_URL}"\n'
            "default = true\n"
        )
        result = detect_index_url()
        # Should strip __token__@ userinfo
        assert result == _FEED_URL

    def test_detects_poetry_primary_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[[tool.poetry.source]]\n"
            'name = "internal"\n'
            f'url = "{_FEED_URL}"\n'
            'priority = "primary"\n'
        )
        result = detect_index_url()
        assert result == _FEED_URL

    def test_uv_takes_precedence_over_poetry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[[tool.uv.index]]\n"
            'url = "https://uv-feed.example.com/simple/"\n'
            "default = true\n"
            "\n"
            "[[tool.poetry.source]]\n"
            'name = "internal"\n'
            'url = "https://poetry-feed.example.com/simple/"\n'
            'priority = "primary"\n'
        )
        result = detect_index_url()
        assert result == "https://uv-feed.example.com/simple/"

    def test_returns_none_when_no_matching_index(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mypkg'\n")
        assert detect_index_url() is None

    def test_detects_hatch_pip_index_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            f'[tool.hatch.envs.default.env-vars]\nPIP_INDEX_URL = "{_FEED_URL}"\n'
        )
        assert detect_index_url() == _FEED_URL

    def test_detects_hatch_uv_default_index(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.hatch.envs.default.env-vars]\n"
            f'UV_DEFAULT_INDEX = "{_TOKEN_FEED_URL}"\n'
        )
        result = detect_index_url()
        assert result == _FEED_URL

    def test_detects_hatch_legacy_uv_index_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            f'[tool.hatch.envs.default.env-vars]\nUV_INDEX_URL = "{_TOKEN_FEED_URL}"\n'
        )
        result = detect_index_url()
        assert result == _FEED_URL

    def test_uv_takes_precedence_over_hatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[[tool.uv.index]]\n"
            'url = "https://uv-feed.example.com/simple/"\n'
            "default = true\n"
            "\n"
            "[tool.hatch.envs.default.env-vars]\n"
            f'PIP_INDEX_URL = "{_FEED_URL}"\n'
        )
        assert detect_index_url() == "https://uv-feed.example.com/simple/"


class TestStripUserinfo:
    def test_strips_token(self) -> None:
        assert _strip_userinfo(_TOKEN_FEED_URL) == _FEED_URL

    def test_preserves_url_without_userinfo(self) -> None:
        assert _strip_userinfo(_FEED_URL) == _FEED_URL

    def test_strips_custom_username(self) -> None:
        assert (
            _strip_userinfo("https://user@example.com:8080/simple/")
            == "https://example.com:8080/simple/"
        )


class TestKeyringBackendAvailable:
    """Tests for _keyring_backend_available (global keyring CLI probe)."""

    def test_returns_false_when_keyring_not_on_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        assert _keyring_backend_available() is False

    def test_returns_true_when_backend_listed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/keyring")

        def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="keyring.backends.ArtifactsKeyring\n"
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        assert _keyring_backend_available() is True

    def test_returns_false_when_backend_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/keyring")

        def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="keyring.backends.fail.Keyring\n"
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        assert _keyring_backend_available() is False

    def test_returns_false_on_nonzero_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/keyring")

        def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="ArtifactsKeyring\n"
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        assert _keyring_backend_available() is False

    def test_returns_false_when_subprocess_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/keyring")

        def fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            msg = "boom"
            raise OSError(msg)

        monkeypatch.setattr("subprocess.run", fake_run)
        assert _keyring_backend_available() is False


class TestResolveKeyringCli:
    """pip skips a keyring installed alongside pip; _resolve mirrors that."""

    def test_returns_none_when_no_keyring(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name, **_k: None)
        assert _resolve_keyring_cli() is None

    def test_returns_cli_outside_scripts_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A keyring not in the interpreter's scripts dir is used as-is.
        monkeypatch.setattr("shutil.which", lambda _name, **_k: "/usr/bin/keyring")
        assert _resolve_keyring_cli() == "/usr/bin/keyring"

    def test_skips_keyring_in_scripts_dir(
        self,
        tmp_path: _Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Mirror pip: if the first keyring lives in the active env's scripts
        # dir, it must be skipped and PATH re-searched for the next one.
        scripts = tmp_path / "env" / "bin"
        scripts.mkdir(parents=True)
        env_keyring = str(scripts / "keyring")
        system_keyring = "/usr/bin/keyring"

        monkeypatch.setattr("sysconfig.get_path", lambda _name: str(scripts))

        calls: list[str | None] = []

        def fake_which(_name: str, path: str | None = None) -> str:
            calls.append(path)
            # First call (path=None) finds the env keyring; the re-search
            # (path excludes the scripts dir) finds the system one.
            return env_keyring if path is None else system_keyring

        monkeypatch.setattr("shutil.which", fake_which)
        monkeypatch.setenv("PATH", os.pathsep.join([str(scripts), "/usr/bin"]))

        assert _resolve_keyring_cli() == system_keyring
        # A re-search happened with a PATH that no longer includes scripts.
        assert calls[-1] is not None
        assert str(scripts) not in calls[-1].split(os.pathsep)


class TestLooksLikeAuthFailure:
    def test_detects_auth_failures(self) -> None:
        for output in (
            "ERROR: HTTP error 401 Client Error: Unauthorized",
            "403 Forbidden",
            "WARNING: ... Unauthorized for url",
            "Could not fetch URL https://feed/pip/: 401 Client Error",
        ):
            assert _looks_like_auth_failure(output) is True, output

    def test_ignores_non_auth_output(self) -> None:
        for output in (
            "Could not find a version that satisfies the requirement",
            "No matching distribution found for pip",
            "Connection timed out",
            "Found credentials in keyring for feed",
            "",
        ):
            assert _looks_like_auth_failure(output) is False, output


class TestVerify:
    def test_passes_no_input_ignore_installed_and_closes_stdin(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, list[str]] = {}
        stdin_seen: list[object] = []

        def fake_run(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            stdin_seen.append(kwargs.get("stdin"))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="")

        monkeypatch.setattr("pypi_lockdown.verify.subprocess.run", fake_run)

        verify("https://example.com/simple/")

        cmd = captured["cmd"]
        assert "--no-input" in cmd
        assert "--ignore-installed" in cmd
        assert stdin_seen == [subprocess.DEVNULL]

    def test_timeout_exits_nonzero_without_hanging(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_run(
            cmd: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd, 60)

        monkeypatch.setattr("pypi_lockdown.verify.subprocess.run", fake_run)

        with pytest.raises(SystemExit):
            verify("https://example.com/simple/")

    def test_auth_failure_prints_keyring_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def fake_run(
            cmd: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr=(
                    "  Could not fetch URL https://feed/pip/: 401 Client Error: "
                    "Unauthorized\nERROR: No matching distribution found for pip"
                ),
            )

        monkeypatch.setattr("pypi_lockdown.verify.subprocess.run", fake_run)

        with pytest.raises(SystemExit):
            verify("https://example.com/simple/")

        out = capsys.readouterr().out
        assert "Authentication to the feed failed" in out
        assert "authentication problem" in out
        assert "artifacts-keyring-nofuss" in out

    def test_reachable_but_missing_probe_is_ok(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Feed authenticated fine (no 401/403) but doesn't mirror `pip`.
        def fake_run(
            cmd: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="ERROR: No matching distribution found for pip",
            )

        monkeypatch.setattr("pypi_lockdown.verify.subprocess.run", fake_run)

        verify("https://example.com/simple/")  # must NOT raise

        out = capsys.readouterr().out
        assert "OK Feed is reachable and authentication works" in out


# ---------------------------------------------------------------------------
# status / undo
# ---------------------------------------------------------------------------


def _patch_global_paths(
    tmp_path: _Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_Path, _Path]:
    """Point user pip/uv config at temp paths and clear env vars."""
    pip_conf = tmp_path / "pip" / "pip.conf"
    uv_toml = tmp_path / "uv" / "uv.toml"
    monkeypatch.setattr("pypi_lockdown.configure._pip_config_user", lambda: pip_conf)
    monkeypatch.setattr("pypi_lockdown.configure._uv_config_user", lambda: uv_toml)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    return pip_conf, uv_toml


class TestStatus:
    def test_reports_managed_configs(
        self,
        tmp_path: _Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _patch_global_paths(tmp_path, monkeypatch)
        configure(_FEED_URL)
        capsys.readouterr()  # discard configure output

        status()
        out = capsys.readouterr().out
        assert "[managed]" in out
        assert _FEED_URL in out

    def test_reports_unconfigured(
        self,
        tmp_path: _Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _patch_global_paths(tmp_path, monkeypatch)

        status()
        out = capsys.readouterr().out
        assert "(not configured)" in out
        assert "[managed]" not in out


class TestUndo:
    def test_removes_managed_configs(
        self,
        tmp_path: _Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        pip_conf, uv_toml = _patch_global_paths(tmp_path, monkeypatch)
        configure(_FEED_URL)
        assert pip_conf.exists()
        assert uv_toml.exists()

        undo()
        assert not pip_conf.exists()
        assert not uv_toml.exists()

    def test_keeps_unmanaged_pip_settings(
        self,
        tmp_path: _Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        pip_conf, _ = _patch_global_paths(tmp_path, monkeypatch)
        configure(_FEED_URL)

        cfg = configparser.ConfigParser()
        cfg.read(pip_conf)
        cfg.add_section("install")
        cfg.set("install", "user", "true")

        with pip_conf.open("w") as fh:
            fh.write(_MARKER)
            cfg.write(fh)

        undo()
        assert pip_conf.exists()
        result = configparser.ConfigParser()
        result.read(pip_conf)
        assert result.has_section("install")
        assert not result.has_option("global", "index-url")

    def test_ignores_unmanaged_files(
        self,
        tmp_path: _Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        pip_conf, _ = _patch_global_paths(tmp_path, monkeypatch)
        pip_conf.parent.mkdir(parents=True, exist_ok=True)
        original = "[global]\nindex-url = https://example.com/simple/\n"
        pip_conf.write_text(original)

        undo()
        # File lacks the marker, so it must be left untouched.
        assert pip_conf.exists()
        assert pip_conf.read_text() == original

    def test_project_scope_removes_pyproject_config(
        self,
        tmp_path: _Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _patch_global_paths(tmp_path, monkeypatch)
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\n[tool.hatch]\n"
        )
        configure(_FEED_URL, project_scope=True)
        assert detect_index_url() == _FEED_URL

        undo(project_scope=True)
        assert detect_index_url() is None

    def test_default_scope_keeps_pyproject_config(
        self,
        tmp_path: _Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _patch_global_paths(tmp_path, monkeypatch)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        configure(_FEED_URL, project_scope=True)
        assert detect_index_url() == _FEED_URL

        undo()  # no --project
        assert detect_index_url() == _FEED_URL
