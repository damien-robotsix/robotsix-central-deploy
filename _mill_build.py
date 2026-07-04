"""PEP 517 / PEP 660 build backend — delegates to hatchling when available.

In the mill sandbox there is no outbound network so the real
``hatchling`` build backend cannot be installed.  This module detects
that situation and falls back to a minimal stdlib-only implementation
that is just capable enough for ``pip install -e . --no-deps`` to
succeed.  In all other environments (GitHub CI, Docker builds, local
dev) the real ``hatchling.build`` backend handles the build.
"""

from __future__ import annotations

# -- try the real backend first ---------------------------------------------

try:
    from hatchling.build import (  # type: ignore[import-not-found]
        build_editable as _real_build_editable,
        build_sdist as _real_build_sdist,
        build_wheel as _real_build_wheel,
        get_requires_for_build_editable as _real_get_requires_for_build_editable,
        get_requires_for_build_sdist as _real_get_requires_for_build_sdist,
        get_requires_for_build_wheel as _real_get_requires_for_build_wheel,
        prepare_metadata_for_build_editable as _real_prepare_metadata_for_build_editable,
        prepare_metadata_for_build_wheel as _real_prepare_metadata_for_build_wheel,
    )

    _HAS_HATCHLING = True
except ImportError:
    _HAS_HATCHLING = False

# -- fallback: minimal stdlib-only backend ----------------------------------

import zipfile as _zipfile
from pathlib import Path as _Path

_HERE = _Path(__file__).resolve().parent
_SRC = _HERE / "src"

_NAME = "robotsix-central-deploy"
_VERSION = "0.1.0"

try:
    import tomllib as _tomllib  # type: ignore[import-not-found,unused-ignore]
except ImportError:
    try:
        import tomli as _tomllib  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        _tomllib = None  # type: ignore[assignment]

if _tomllib is not None:
    try:
        _data = _tomllib.loads((_HERE / "pyproject.toml").read_text())
        _project = _data.get("project", {})
        _NAME = _project.get("name", _NAME)
        _VERSION = _project.get("version", _VERSION)
    except Exception:  # noqa: S110 — best-effort; pyproject.toml may be absent/unparseable
        pass

_NAME_NORMALIZED = _NAME.replace("-", "_")


def _find_packages() -> list[str]:
    packages: list[str] = []
    if not _SRC.is_dir():
        return packages
    for child in sorted(_SRC.iterdir()):
        if (
            child.is_dir()
            and not child.name.startswith((".", "_"))
            and (child / "__init__.py").is_file()
        ):
            packages.append(child.name)
    return packages


def _write_dist_info(metadata_dir: _Path, name: str, version: str) -> str:
    dist_info_name = f"{_NAME_NORMALIZED}-{version}.dist-info"
    dist_info = metadata_dir / dist_info_name
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    )
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\n"
        "Generator: mill-fallback 0.1.0\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    # Generate entry_points.txt from [project.scripts] and
    # [project.gui-scripts] in pyproject.toml so that console scripts
    # (e.g. robotsix-lifecycle) exist even without hatchling.
    _entry_points_lines: list[str] = []
    if _tomllib is not None and _project:
        _scripts = _project.get("scripts", {})
        if _scripts:
            _entry_points_lines.append("[console_scripts]")
            for _ep_name, _ep_target in _scripts.items():
                _entry_points_lines.append(f"{_ep_name} = {_ep_target}")
            _entry_points_lines.append("")
        _gui_scripts = _project.get("gui-scripts", {})
        if _gui_scripts:
            _entry_points_lines.append("[gui_scripts]")
            for _ep_name, _ep_target in _gui_scripts.items():
                _entry_points_lines.append(f"{_ep_name} = {_ep_target}")
            _entry_points_lines.append("")
    (dist_info / "entry_points.txt").write_text(
        "\n".join(_entry_points_lines) + "\n"
    )
    return dist_info_name


def _write_record(zf: _zipfile.ZipFile, dist_info_name: str) -> None:
    import hashlib as _hashlib
    lines: list[str] = []
    for name in sorted(zf.namelist()):
        if name.endswith("/") or name == f"{dist_info_name}/RECORD":
            continue
        info = zf.getinfo(name)
        data = zf.read(name)
        sha = _hashlib.sha256(data).hexdigest()
        lines.append(f"{name},sha256={sha},{info.file_size}")
    lines.append(f"{dist_info_name}/RECORD,,")
    zf.writestr(f"{dist_info_name}/RECORD", "\n".join(lines) + "\n")


# -- public hooks -----------------------------------------------------------

if _HAS_HATCHLING:

    def get_requires_for_build_wheel(config_settings=None):
        return _real_get_requires_for_build_wheel(config_settings)

    def get_requires_for_build_editable(config_settings=None):
        return _real_get_requires_for_build_editable(config_settings)

    def get_requires_for_build_sdist(config_settings=None):
        return _real_get_requires_for_build_sdist(config_settings)

    def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
        return _real_prepare_metadata_for_build_wheel(
            metadata_directory, config_settings
        )

    def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
        return _real_prepare_metadata_for_build_editable(
            metadata_directory, config_settings
        )

    def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
        return _real_build_wheel(
            wheel_directory, config_settings, metadata_directory
        )

    def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
        return _real_build_editable(
            wheel_directory, config_settings, metadata_directory
        )

    def build_sdist(sdist_directory, config_settings=None):
        return _real_build_sdist(sdist_directory, config_settings)

else:
    # Minimal stdlib-only implementations.

    def get_requires_for_build_wheel(config_settings=None):
        return []

    def get_requires_for_build_editable(config_settings=None):
        return []

    def get_requires_for_build_sdist(config_settings=None):
        return []

    def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
        _write_dist_info(_Path(metadata_directory), _NAME, _VERSION)
        return f"{_NAME_NORMALIZED}-{_VERSION}.dist-info"

    def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
        return prepare_metadata_for_build_wheel(metadata_directory, config_settings)

    def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
        dist_info_name = f"{_NAME_NORMALIZED}-{_VERSION}.dist-info"
        wheel_name = f"{_NAME_NORMALIZED}-{_VERSION}-py3-none-any.whl"
        wheel_path = _Path(wheel_directory) / wheel_name

        if metadata_directory is not None:
            di = _Path(metadata_directory)
        else:
            di = _Path(wheel_directory) / dist_info_name
            if not di.is_dir():
                _write_dist_info(_Path(wheel_directory), _NAME, _VERSION)

        packages = _find_packages()
        with _zipfile.ZipFile(wheel_path, "w", _zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(di.iterdir()):
                zf.write(f, f"{dist_info_name}/{f.name}")
            for pkg in packages:
                pkg_dir = _SRC / pkg
                for f in sorted(pkg_dir.rglob("*")):
                    if f.is_file() and "__pycache__" not in f.parts:
                        arcname = str(f.relative_to(_SRC))
                        zf.write(f, arcname)
            _write_record(zf, dist_info_name)

        return wheel_name

    def build_editable(
        wheel_directory, config_settings=None, metadata_directory=None
    ):
        dist_info_name = f"{_NAME_NORMALIZED}-{_VERSION}.dist-info"
        wheel_name = f"{_NAME_NORMALIZED}-{_VERSION}-0-py3-none-any.whl"
        wheel_path = _Path(wheel_directory) / wheel_name

        if metadata_directory is not None:
            di = _Path(metadata_directory)
        else:
            di = _Path(wheel_directory) / dist_info_name
            if not di.is_dir():
                _write_dist_info(_Path(wheel_directory), _NAME, _VERSION)

        packages = _find_packages()
        with _zipfile.ZipFile(wheel_path, "w", _zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(di.iterdir()):
                zf.write(f, f"{dist_info_name}/{f.name}")
            zf.writestr(f"{dist_info_name}/_editable.pth", str(_SRC) + "\n")
            for pkg in packages:
                pkg_dir = _SRC / pkg
                for f in sorted(pkg_dir.rglob("*")):
                    if f.is_file() and "__pycache__" not in f.parts:
                        arcname = str(f.relative_to(_SRC))
                        zf.write(f, arcname)
            _write_record(zf, dist_info_name)

        return wheel_name

    def build_sdist(sdist_directory, config_settings=None):
        raise NotImplementedError("sdist not supported by minimal backend")
