"""Tests for pypi-lockdown."""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import zipfile
from pathlib import Path as _Path
from typing import TYPE_CHECKING

import pytest
import tomlkit

from pypi_lockdown._build_standalone import _extract_wheels
from pypi_lockdown.configure import (
    _ensure_userinfo,
    _strip_userinfo,
    _write_pip_config,
    _write_pyproject_poetry,
    _write_pyproject_uv,
    _write_uv_config,
    configure,
    detect_index_url,
)
from pypi_lockdown.standalone import (
    _installed_packages,
    _is_pure_python,
    _process_site_packages,
    _resolve_bootstrap_allowlist,
    _runtime_deps,
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


# ---------------------------------------------------------------------------
# Process site-packages discovery & allowlist
# ---------------------------------------------------------------------------


class TestProcessSitePackages:
    def test_finds_current_process_site_packages(self) -> None:
        sp = _process_site_packages()
        # We're running inside an env that has keyring installed
        assert sp is not None
        assert sp.is_dir()
        assert any(sp.glob("keyring-*.dist-info"))


class TestIsPurePython:
    def _make_dist_info(
        self, site_packages: Path, name: str, version: str, tag: str
    ) -> None:
        di = site_packages / f"{name}-{version}.dist-info"
        di.mkdir(parents=True)
        (di / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
        (di / "WHEEL").write_text(f"Wheel-Version: 1.0\nTag: {tag}\n")

    def test_pure_python(self, tmp_path: Path) -> None:
        self._make_dist_info(tmp_path, "mypkg", "1.0", "py3-none-any")
        assert _is_pure_python(tmp_path, "mypkg") is True

    def test_c_extension(self, tmp_path: Path) -> None:
        self._make_dist_info(
            tmp_path, "mypkg", "1.0", "cp312-cp312-manylinux_2_34_x86_64"
        )
        assert _is_pure_python(tmp_path, "mypkg") is False

    def test_abi3(self, tmp_path: Path) -> None:
        self._make_dist_info(
            tmp_path, "mypkg", "1.0", "cp311-abi3-manylinux_2_34_x86_64"
        )
        assert _is_pure_python(tmp_path, "mypkg") is True


class TestRuntimeDeps:
    def test_extracts_deps(self, tmp_path: Path) -> None:
        di = tmp_path / "mypkg-1.0.dist-info"
        di.mkdir()
        (di / "METADATA").write_text(
            "Name: mypkg\nVersion: 1.0\n"
            "Requires-Dist: requests>=2.20\n"
            "Requires-Dist: keyring>=23.0\n"
            "Requires-Dist: pytest; extra == 'dev'\n"
        )
        deps = _runtime_deps(tmp_path, "mypkg")
        assert "requests" in deps
        assert "keyring" in deps
        assert "pytest" not in deps


class TestResolveBootstrapAllowlist:
    def _make_pkg(
        self,
        site_packages: Path,
        name: str,
        version: str,
        deps: list[str] | None = None,
        *,
        tag: str = "py3-none-any",
    ) -> None:
        norm = name.lower().replace("-", "_")
        di = site_packages / f"{norm}-{version}.dist-info"
        di.mkdir(parents=True)
        meta = f"Name: {name}\nVersion: {version}\n"
        for d in deps or []:
            meta += f"Requires-Dist: {d}\n"
        (di / "METADATA").write_text(meta)
        (di / "WHEEL").write_text(f"Wheel-Version: 1.0\nTag: {tag}\n")
        pkg_dir = site_packages / norm
        pkg_dir.mkdir(exist_ok=True)
        (pkg_dir / "__init__.py").write_text(f"__version__ = '{version}'\n")

    def test_resolves_transitive_deps(self, tmp_path: Path) -> None:
        self._make_pkg(tmp_path, "keyring", "25.6.0", ["jaraco.classes"])
        self._make_pkg(
            tmp_path,
            "artifacts-keyring-nofuss",
            "0.8.0",
            ["keyring>=23.0", "requests>=2.20"],
        )
        self._make_pkg(tmp_path, "jaraco.classes", "3.4.0")
        self._make_pkg(tmp_path, "requests", "2.32.0")

        allowed = _resolve_bootstrap_allowlist(tmp_path)
        assert "keyring" in allowed
        assert "artifacts_keyring_nofuss" in allowed
        assert "jaraco.classes" in allowed or "jaraco_classes" in allowed
        assert "requests" in allowed

    def test_excludes_pypi_lockdown(self, tmp_path: Path) -> None:
        self._make_pkg(
            tmp_path,
            "artifacts-keyring-nofuss",
            "0.8.0",
            ["keyring>=23.0"],
        )
        self._make_pkg(tmp_path, "keyring", "25.6.0")
        self._make_pkg(tmp_path, "pypi-lockdown", "0.9.0")

        allowed = _resolve_bootstrap_allowlist(tmp_path)
        assert "pypi_lockdown" not in allowed

    def test_skips_c_extensions(self, tmp_path: Path) -> None:
        self._make_pkg(
            tmp_path,
            "artifacts-keyring-nofuss",
            "0.8.0",
            ["cryptography>=2.5"],
        )
        self._make_pkg(tmp_path, "keyring", "25.6.0")
        self._make_pkg(
            tmp_path,
            "cryptography",
            "43.0.0",
            tag="cp312-cp312-manylinux_2_34_x86_64",
        )

        allowed = _resolve_bootstrap_allowlist(tmp_path)
        assert "cryptography" not in allowed


class TestBootstrapFromProcess:
    """Test bootstrap_keyring in process (non-shiv) mode."""

    def _make_site_packages(
        self,
        base: Path,
        packages: dict[str, str],
    ) -> Path:
        sp = base / "site-packages"
        sp.mkdir(parents=True)
        for name, version in packages.items():
            di = sp / f"{name}-{version}.dist-info"
            di.mkdir()
            (di / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
            (di / "WHEEL").write_text("Wheel-Version: 1.0\nTag: py3-none-any\n")
            (di / "top_level.txt").write_text(f"{name}\n")
            pkg_dir = sp / name
            pkg_dir.mkdir(exist_ok=True)
            (pkg_dir / "__init__.py").write_text(f"__version__ = '{version}'\n")
        return sp

    _SHIV = "pypi_lockdown.standalone._shiv_site_packages"
    _PROC = "pypi_lockdown.standalone._process_site_packages"
    _TGT = "pypi_lockdown.standalone._target_site_packages"

    def test_same_env_skips(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When source and target are the same dir, nothing is copied."""
        sp = self._make_site_packages(
            tmp_path / "env",
            {"keyring": "25.6.0"},
        )

        monkeypatch.setattr(self._SHIV, lambda: None)
        monkeypatch.setattr(self._PROC, lambda: sp)
        monkeypatch.setattr(self._TGT, lambda _p: sp)

        result = bootstrap_keyring(tmp_path / "env")
        assert result is False

    def test_copies_allowlisted_packages(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = self._make_site_packages(
            tmp_path / "src",
            {"keyring": "25.6.0", "artifacts_keyring_nofuss": "0.8.0"},
        )
        # Add requires so allowlist resolves
        di = src / "artifacts_keyring_nofuss-0.8.0.dist-info"
        (di / "METADATA").write_text(
            "Name: artifacts-keyring-nofuss\nVersion: 0.8.0\n"
            "Requires-Dist: keyring>=23.0\n"
        )
        dst = self._make_site_packages(tmp_path / "dst", {})

        monkeypatch.setattr(self._SHIV, lambda: None)
        monkeypatch.setattr(self._PROC, lambda: src)
        monkeypatch.setattr(self._TGT, lambda _p: dst)

        result = bootstrap_keyring(tmp_path / "env")
        assert result is True
        out = capsys.readouterr().out
        assert "Installed" in out
        assert (dst / "keyring" / "__init__.py").exists()
        assert (dst / "artifacts_keyring_nofuss" / "__init__.py").exists()


# ---------------------------------------------------------------------------
# End-to-end: pipx install → configure → keyring in target venv
# ---------------------------------------------------------------------------


def _install_dummy_backend(site_packages: Path) -> None:
    """Drop a minimal keyring backend into *site_packages*.

    Priority 1 — below ArtifactsKeyringBackend (9.9), so it only
    handles URLs that the real backend declines.
    """
    pkg_dir = site_packages / "dummy_keyring_backend"
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "__init__.py").write_text(
        "import keyring.backend\n"
        "\n"
        "class DummyBackend(keyring.backend.KeyringBackend):\n"
        "    priority = 1\n"
        "\n"
        "    def get_password(self, service, username):\n"
        "        return 'dummy-secret-token'\n"
        "\n"
        "    def set_password(self, service, username, password):\n"
        "        raise NotImplementedError\n"
        "\n"
        "    def delete_password(self, service, username):\n"
        "        raise NotImplementedError\n"
    )
    di = site_packages / "dummy_keyring_backend-0.0.1.dist-info"
    di.mkdir(exist_ok=True)
    (di / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: dummy-keyring-backend\nVersion: 0.0.1\n"
    )
    (di / "entry_points.txt").write_text(
        "[keyring.backends]\ndummy = dummy_keyring_backend\n"
    )
    (di / "top_level.txt").write_text("dummy_keyring_backend\n")


@pytest.mark.slow
class TestPipxEndToEnd:
    """Full integration test: pipx-install pypi-lockdown, then configure a venv."""

    @staticmethod
    def _run(
        cmd: list[str],
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )

    def test_pipx_bootstrap_into_venv(self, tmp_path: Path) -> None:
        import venv  # noqa: PLC0415

        # --- 1. Create an isolated pipx home ---
        pipx_home = tmp_path / "pipx_home"
        pipx_bin = tmp_path / "pipx_bin"
        pipx_home.mkdir()
        pipx_bin.mkdir()

        pkg_root = _Path(__file__).resolve().parent.parent

        env = {
            **os.environ,
            "PIPX_HOME": str(pipx_home),
            "PIPX_BIN_DIR": str(pipx_bin),
        }
        # Remove any VIRTUAL_ENV so pipx uses its own
        env.pop("VIRTUAL_ENV", None)
        env.pop("CONDA_PREFIX", None)

        # --- 2. pipx install pypi-lockdown from the local checkout ---
        r = self._run(
            ["pipx", "install", str(pkg_root), "--force"],
            env=env,
        )
        assert r.returncode == 0, (
            f"pipx install failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )

        pypi_lockdown_bin = pipx_bin / (
            "pypi-lockdown.exe" if sys.platform == "win32" else "pypi-lockdown"
        )
        assert pypi_lockdown_bin.exists(), (
            f"pypi-lockdown not in {pipx_bin}: {list(pipx_bin.iterdir())}"
        )

        # --- 3. Create a target venv (the "user" env) ---
        user_venv = tmp_path / "user_venv"
        venv.create(str(user_venv), with_pip=False)

        # --- 4. Run pypi-lockdown configure with VIRTUAL_ENV ---
        configure_env = {
            **env,
            "VIRTUAL_ENV": str(user_venv),
        }
        feed_url = (
            "https://pkgs.dev.azure.com/pypi-lockdown"
            "/pypi-lockdown/_packaging/public@Local/pypi/simple/"
        )
        r = self._run(
            [str(pypi_lockdown_bin), "configure", feed_url],
            env=configure_env,
        )
        assert r.returncode == 0, (
            f"configure failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )

        # --- 5. Verify pip.conf was written ---
        pip_conf = user_venv / ("pip.ini" if sys.platform == "win32" else "pip.conf")
        assert pip_conf.exists(), "pip.conf not written"
        cfg = configparser.ConfigParser()
        cfg.read(pip_conf)
        assert cfg.get("global", "index-url") == feed_url

        # --- 6. Verify keyring packages were bootstrapped ---
        # Find the site-packages in the user venv (platform-dependent)
        if sys.platform == "win32":
            sp_candidates = list((user_venv / "Lib").glob("site-packages"))
        else:
            sp_candidates = list((user_venv / "lib").glob("python*/site-packages"))
        assert sp_candidates, "No site-packages in user venv"
        user_sp = sp_candidates[0]

        keyring_installed = any(user_sp.glob("keyring-*.dist-info"))
        nofuss_installed = any(user_sp.glob("artifacts_keyring_nofuss-*.dist-info"))
        assert keyring_installed, (
            f"keyring not bootstrapped into {user_sp}. "
            f"Contents: {[p.name for p in user_sp.iterdir()]}"
        )
        assert nofuss_installed, (
            f"artifacts-keyring-nofuss not bootstrapped into {user_sp}. "
            f"Contents: {[p.name for p in user_sp.iterdir()]}"
        )

        # --- 7. Verify keyring is actually importable in target venv ---
        user_python = (
            user_venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
        )
        r = self._run(
            [
                str(user_python),
                "-c",
                "import keyring; "
                "import artifacts_keyring_nofuss; "
                "from importlib.metadata import version; "
                "print('keyring', version('keyring')); "
                "print('nofuss', version('artifacts-keyring-nofuss'))",
            ]
        )
        assert r.returncode == 0, (
            f"import failed in target venv:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert "keyring" in r.stdout
        assert "nofuss" in r.stdout

        # --- 8. Verify the keyring backend is discoverable ---
        r = self._run(
            [
                str(user_python),
                "-c",
                "from keyring.backend import get_all_keyring; "
                "names = [type(k).__name__ for k in get_all_keyring()]; "
                "print(names); "
                "assert 'ArtifactsKeyringBackend' in names, "
                "'backend not found in ' + str(names)",
            ]
        )
        assert r.returncode == 0, (
            f"keyring backend not discoverable:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )

        # --- 9. Install a dummy low-priority backend, verify chain ---
        # Directly instantiate backends instead of `keyring get` to
        # avoid querying the system keyring and to distinguish
        # "declined" (returned None) from "crashed" (raised).
        _install_dummy_backend(user_sp)

        test_url = "https://not-ado.example.com/simple/"
        r = self._run(
            [
                str(user_python),
                "-c",
                "from artifacts_keyring_nofuss._backend import "
                "ArtifactsKeyringBackend; "
                "b = ArtifactsKeyringBackend(); "
                f"result = b.get_credential('{test_url}', None); "
                "assert result is None, "
                f"'expected None, got ' + repr(result); "
                "print('DECLINED')",
            ]
        )
        assert r.returncode == 0, (
            f"ArtifactsKeyringBackend did not cleanly decline:\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert "DECLINED" in r.stdout

        # Now verify the dummy backend (priority 1) does return a token
        r = self._run(
            [
                str(user_python),
                "-c",
                "from dummy_keyring_backend import DummyBackend; "
                "b = DummyBackend(); "
                f"pw = b.get_password('{test_url}', 'testuser'); "
                "print(pw)",
            ]
        )
        assert r.returncode == 0, (
            f"Dummy backend failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.stdout.strip() == "dummy-secret-token"
