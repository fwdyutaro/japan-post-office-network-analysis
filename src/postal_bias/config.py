from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AcquisitionConfig:
    """Explicit defaults; values may be overridden by the CLI or a config file."""

    base_dir: Path = field(default_factory=lambda: Path.cwd())
    raw_dir: Path | None = None
    manifest_path: Path | None = None
    min_interval_seconds: float = 10.0
    max_retries: int = 2
    max_bytes: int = 50 * 1024 * 1024
    user_agent: str = "postal-bias-research/0.1 (contact: configure-contact)"
    contact: str | None = None

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.raw_dir = Path(self.raw_dir or self.base_dir / "data" / "raw")
        self.manifest_path = Path(self.manifest_path or self.base_dir / "data" / "manifest" / "manifest.sqlite3")

    @classmethod
    def from_file(cls, path: str | Path) -> "AcquisitionConfig":
        """Read a tiny optional ``KEY=VALUE`` configuration file."""
        values: dict[str, object] = {}
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key in {"min_interval_seconds"}:
                values[key] = float(value)
            elif key in {"max_retries", "max_bytes"}:
                values[key] = int(value)
            elif key in {"base_dir", "raw_dir", "manifest_path"}:
                values[key] = Path(value)
            elif key in {"user_agent", "contact"}:
                values[key] = value
        return cls(**values)
