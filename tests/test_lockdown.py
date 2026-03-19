"""Tests for pypi-lockdown."""

from __future__ import annotations

import configparser
import zipfile
from typing import TYPE_CHECKING

import pytest

from pypi_lockdown._build_standalone import _extract_wheels
from pypi_lockdown.configure import _write_pip_config, _write_uv_config, configure

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
        assert 'url = "https://example.com/simple/"' in content
        assert "default = true" in content


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
