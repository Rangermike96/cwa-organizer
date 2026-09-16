"""Configuration loading: packaged defaults.toml + the user's config.toml + secrets.toml."""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULTS_FILE = PACKAGE_DIR / "defaults.toml"


class ConfigError(Exception):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            # Alias/synonym tables are merged key-by-key too, so a user file
            # can add entries without repeating the defaults.
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


@dataclass
class Config:
    data: dict
    project_dir: Path
    config_file: Path | None

    def __getitem__(self, section: str) -> dict:
        return self.data[section]

    # Convenience paths -----------------------------------------------------
    @property
    def library(self) -> Path:
        return Path(os.path.expanduser(self.data["library"]["path"]))

    @property
    def metadata_db(self) -> Path:
        return self.library / "metadata.db"

    @property
    def backups_dir(self) -> Path:
        return self.project_dir / "backups"

    @property
    def runs_dir(self) -> Path:
        return self.project_dir / "runs"

    @property
    def state_dir(self) -> Path:
        return self.project_dir / "state"

    @property
    def review_dir(self) -> Path:
        return self.project_dir / "review"

    @property
    def aliases_file(self) -> Path:
        return self.project_dir / "aliases.toml"

    @property
    def hardcover_token(self) -> str:
        env = os.environ.get("HARDCOVER_API_KEY", "").strip()
        if env:
            return env
        tok = str(self.data.get("secrets", {}).get("hardcover_api_key", "")).strip()
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        return tok

    def label(self, name: str) -> str:
        return self.data["labels"][name]

    def column(self, name: str) -> str:
        """Calibre field name for one of our custom columns, e.g. '#booktype'."""
        return "#" + self.data["columns"][name].lstrip("#")

    def aliases(self) -> dict:
        """User-approved aliases from aliases.toml: {'authors': {...}, 'series': {...}, 'publishers': {...}}."""
        out = {"authors": {}, "series": {}, "publishers": {}}
        if self.aliases_file.exists():
            raw = _load_toml(self.aliases_file)
            for section in out:
                tbl = raw.get(section, {})
                if not isinstance(tbl, dict):
                    raise ConfigError(f"{self.aliases_file}: [{section}] must be a table")
                for k, v in tbl.items():
                    if not isinstance(v, str) or not v.strip():
                        raise ConfigError(f"{self.aliases_file}: [{section}] \"{k}\" must map to a non-empty string")
                    out[section][k] = v.strip()
        return out


def load_config(config_path: str | os.PathLike | None = None, project_dir: Path | None = None) -> Config:
    project_dir = Path(project_dir) if project_dir else PROJECT_DIR
    data = _load_toml(DEFAULTS_FILE)
    cfg_file = Path(config_path) if config_path else project_dir / "config.toml"
    if cfg_file.exists():
        data = _deep_merge(data, _load_toml(cfg_file))
    elif config_path:
        raise ConfigError(f"Config file not found: {cfg_file}")
    else:
        cfg_file = None
    secrets_file = project_dir / "secrets.toml"
    data["secrets"] = _load_toml(secrets_file) if secrets_file.exists() else {}
    _validate(data)
    return Config(data=data, project_dir=project_dir, config_file=cfg_file)


def _validate(d: dict) -> None:
    def need(cond, msg):
        if not cond:
            raise ConfigError(msg)

    need(isinstance(d["library"].get("path"), str) and d["library"]["path"], "library.path must be set")
    f = d["fetch"]
    for k in ("sleep_base", "sleep_jitter", "max_retries", "timeout_seconds", "max_title_variants"):
        need(isinstance(f[k], int) and f[k] >= 0, f"fetch.{k} must be a non-negative integer")
    need(d["titles"]["part_index_style"] in ("decimal", "skip"), "titles.part_index_style must be 'decimal' or 'skip'")
    need(d["files"]["check_level"] in ("fast", "deep"), "files.check_level must be 'fast' or 'deep'")
    need(isinstance(d["calibre"]["flush_every"], int) and d["calibre"]["flush_every"] >= 1, "calibre.flush_every must be >= 1")
    labels = [d["labels"][k] for k in ("light_novel", "manga", "other")]
    need(len({x.casefold() for x in labels}) == 3, "labels.light_novel/manga/other must be different")
    for sec in ("booktype", "status"):
        lab = d["columns"][sec].lstrip("#")
        need(lab.isidentifier() and lab == lab.lower(), f"columns.{sec} must be a lowercase identifier (letters, digits, _)")
