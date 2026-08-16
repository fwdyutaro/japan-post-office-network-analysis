"""Portable paths shared by the public article producers.

The original research workspace lived on a private drive. Public scripts use
the repository root by default and can be pointed at a separate checkout with
``POSTAL_BIAS_PROJECT_ROOT``. Additional helpers expose opt-in data, work and
output paths for scripts that call those helpers explicitly.
"""
from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    value = os.environ.get("POSTAL_BIAS_PROJECT_ROOT")
    if value:
        return Path(value).expanduser().resolve()
    # This module is repository/data/work/project_paths.py.
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    value = os.environ.get("POSTAL_BIAS_DATA_DIR")
    return Path(value).expanduser().resolve() if value else project_root() / "data"


def work_dir() -> Path:
    value = os.environ.get("POSTAL_BIAS_WORK_DIR")
    return Path(value).expanduser().resolve() if value else data_dir() / "work"


def output_dir(default: str | Path | None = None) -> Path:
    value = os.environ.get("POSTAL_BIAS_OUTPUT_DIR")
    if value:
        return Path(value).expanduser().resolve()
    return Path(default) if default is not None else work_dir()
