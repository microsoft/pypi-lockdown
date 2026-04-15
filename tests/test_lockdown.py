"""Tests for pypi-lockdown."""

from __future__ import annotations

import configparser
import zipfile
from typing import TYPE_CHECKING

import pytest
import tomlkit

from pypi_lockdown._build_standalone import _extract_wheels
from pypi_lockdown.configure import (
    _ensure_userinfo,
    _write_pip_config,
    _write_pyproject_poetry,
    _write_pyproject_uv,
    _write_uv_config,
    configure,
)
from pypi_lockdown.standalone import (
    _installed_packages,
    bootstrap_keyring,
)

if TYPE_CHECKING:
    from pathlib import Path


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
# Zip-slip protection
# ---------------------------------------------------------------------------


class TestZipSlipProtection:
    def _make_wheel(self, path: Path, entries: dict[str, bytes]) -> None:
        """Create a .whl file (which is just a zip) with the given entries."""
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)

    def test_normal_wheel_extracts(self, tmp_path: Path) -> None:
        wheel_dir = tmp_path / "wheels"
        staging = tmp_path / "staging"
        wheel_dir.mkdir()

        self._make_wheel(
            wheel_dir / "pkg-1.0-py3-none-any.whl",
            {"pkg/__init__.py": b"# ok", "pkg/module.py": b"# ok"},
        )

        _extract_wheels(wheel_dir, staging)
        assert (staging / "pkg" / "__init__.py").exists()

    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        wheel_dir = tmp_path / "wheels"
        staging = tmp_path / "staging"
        wheel_dir.mkdir()

        self._make_wheel(
            wheel_dir / "evil-1.0-py3-none-any.whl",
            {"../../etc/evil.conf": b"malicious"},
        )

        with pytest.raises(ValueError, match="path traversal"):
            _extract_wheels(wheel_dir, staging)


# ---------------------------------------------------------------------------
# Bootstrap: version-aware skip logic
# ---------------------------------------------------------------------------


class TestBootstrapKeyring:
    """Test _installed_packages and bootstrap_keyring skip/warn behaviour."""

    def _make_site_packages(
        self,
        base: Path,
        packages: dict[str, str],
    ) -> Path:
        """Create a fake site-packages with .dist-info dirs and stub modules."""
        sp = base / "site-packages"
        sp.mkdir(parents=True)
        for name, version in packages.items():
            di = sp / f"{name}-{version}.dist-info"
            di.mkdir()
            (di / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
            pkg_dir = sp / name
            pkg_dir.mkdir(exist_ok=True)
            (pkg_dir / "__init__.py").write_text(f"__version__ = '{version}'\n")
        return sp

    def test_installed_packages_parses_dist_info(self, tmp_path: Path) -> None:
        sp = self._make_site_packages(tmp_path, {"keyring": "25.0.0"})
        result = _installed_packages(sp)
        assert result == {"keyring": "25.0.0"}

    def test_skips_same_version(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = self._make_site_packages(tmp_path / "src", {"keyring": "25.6.0"})
        dst = self._make_site_packages(tmp_path / "dst", {"keyring": "25.6.0"})

        monkeypatch.setattr(
            "pypi_lockdown.standalone._shiv_site_packages",
            lambda: src,
        )
        monkeypatch.setattr(
            "pypi_lockdown.standalone._target_site_packages",
            lambda _p: dst,
        )

        result = bootstrap_keyring(tmp_path / "env")
        assert result is False  # nothing new installed
        out = capsys.readouterr().out
        assert "Already installed" in out
        assert "keyring-25.6.0" in out

    def test_warns_on_version_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = self._make_site_packages(tmp_path / "src", {"keyring": "25.6.0"})
        dst = self._make_site_packages(tmp_path / "dst", {"keyring": "25.0.0"})

        monkeypatch.setattr(
            "pypi_lockdown.standalone._shiv_site_packages",
            lambda: src,
        )
        monkeypatch.setattr(
            "pypi_lockdown.standalone._target_site_packages",
            lambda _p: dst,
        )

        result = bootstrap_keyring(tmp_path / "env")
        assert result is False  # skipped, not installed
        out = capsys.readouterr().out
        assert "Skipped" in out
        assert "installed 25.0.0" in out
        assert "bundled 25.6.0" in out

    def test_installs_missing_package(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = self._make_site_packages(tmp_path / "src", {"keyring": "25.6.0"})
        dst = tmp_path / "dst" / "site-packages"
        dst.mkdir(parents=True)  # empty target

        monkeypatch.setattr(
            "pypi_lockdown.standalone._shiv_site_packages",
            lambda: src,
        )
        monkeypatch.setattr(
            "pypi_lockdown.standalone._target_site_packages",
            lambda _p: dst,
        )

        result = bootstrap_keyring(tmp_path / "env")
        assert result is True
        out = capsys.readouterr().out
        assert "Installed" in out
        assert (dst / "keyring" / "__init__.py").exists()


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


class TestConfigurePyprojectPrompt:
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

    def test_writes_when_confirmed(
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
        monkeypatch.setattr(
            "pypi_lockdown.configure._prompt_yes_no",
            lambda _prompt: True,
        )

        configure(_FEED_URL)

        content = (tmp_path / "pyproject.toml").read_text()
        assert "tool.uv" in content or "keyring-provider" in content
        assert "tool.poetry" in content or "internal" in content

    def test_skips_when_declined(
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
        monkeypatch.setattr(
            "pypi_lockdown.configure._prompt_yes_no",
            lambda _prompt: False,
        )

        configure(_FEED_URL)

        assert (tmp_path / "pyproject.toml").read_text() == original
