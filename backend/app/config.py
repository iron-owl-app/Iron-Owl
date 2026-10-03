from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

# Packaged installs (the Windows launcher) keep everything that belongs to the user outside
# the code folder: FINTRACK_HOME holds data\, logs\, the Edge profile and this settings file.
HOME_SETTINGS_FILE = "fintrack.env"
# A control secret shorter than this is refused (the launcher makes 43-character ones).
MIN_CONTROL_SECRET = 32
# Longest support contact shown in the UI and the launcher's message box.
SUPPORT_CONTACT_MAX = 60


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def fintrack_home() -> Path | None:
    """``FINTRACK_HOME`` from the real environment (never from a settings file: it says
    where the settings file is). Unset for the owner's git checkout."""
    return _env_path("FINTRACK_HOME")


def settings_file() -> Path:
    """``FINTRACK_HOME/fintrack.env`` in a packaged install, otherwise the repo's env file."""
    home = fintrack_home()
    return home / HOME_SETTINGS_FILE if home else REPO_ROOT / ".env"


def _default_data_dir(values: dict[str, Any]) -> Path:
    home = values.get("fintrack_home")
    return Path(home) / "data" if home else REPO_ROOT / "data"


def _default_frontend_dist() -> Path:
    # FINTRACK_WEB: the launcher points this at versions\<v>\web (the built SPA).
    return _env_path("FINTRACK_WEB") or REPO_ROOT / "frontend" / "dist"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    plaid_client_id: str = ""
    plaid_secret: SecretStr = SecretStr("")
    plaid_env: Literal["sandbox", "production"] = "sandbox"

    auto_lock_minutes: int = Field(default=15, ge=1, le=24 * 60)
    # Who to call about a lost password or recovery sheet, shown on the unlock screen before
    # login (e.g. "Sam"). Empty: the app says "the person who set up FinTrack".
    # Longer values are cut to SUPPORT_CONTACT_MAX (never refused: the owner's own settings
    # file must not stop FinTrack from starting).
    support_contact: str = ""
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # Packaged installs (see load_settings). Declared before data_dir, whose default is
    # FINTRACK_HOME/data when it is set.
    fintrack_home: Path | None = None
    # The install root (…\Programs\FinTrack: state.json, versions\, runtimes\). Set by the
    # launcher; unset means "not an installed copy" (the updater stays off).
    fintrack_install_root: Path | None = None
    # The code checkout the app runs from (tests inject a fake one).
    repo_root: Path = REPO_ROOT
    # Optional UPDATE_SOURCE=file|github|off: overrides where this installed copy gets
    # updates (the build's release.json decides otherwise; see updates/source.py). Anything
    # else turns updates off; it never stops the app from starting.
    update_source: str = ""

    data_dir: Path = Field(default_factory=_default_data_dir)
    frontend_dist: Path = Field(default_factory=_default_frontend_dist)

    # Heartbeat self-exit (the launcher passes 180): once no FinTrack window has sent
    # POST /api/app/alive for this long while the vault is locked, the server exits.
    # 0 = never (owner's copy).
    exit_when_unused_seconds: int = Field(default=0, ge=0, le=7 * 24 * 3600)
    # Per-launch secret from the launcher: proves /api/health answers come from the server it
    # started, and authorizes POST /api/app/shutdown. Empty = both features off.
    control_secret: SecretStr = SecretStr("")

    # DEBUG enables /docs, /redoc and /openapi.json.
    debug: bool = False
    # DEV additionally allows the Vite dev server origin (localhost:5173).
    dev: bool = False
    dev_port: int = 5173

    @field_validator("support_contact", mode="before")
    @classmethod
    def _clean_contact(cls, value: Any) -> Any:
        # Shown in UI copy and a Windows message box: printable characters only, trimmed,
        # at most SUPPORT_CONTACT_MAX of them.
        if isinstance(value, str):
            text = "".join(ch for ch in value if ch.isprintable()).strip()
            return text[:SUPPORT_CONTACT_MAX].strip()
        return value

    @field_validator("control_secret")
    @classmethod
    def _check_control_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and len(raw) < MIN_CONTROL_SECRET:
            raise ValueError(f"must be at least {MIN_CONTROL_SECRET} characters")
        return value

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.plaid_secret.get_secret_value())

    @property
    def control_enabled(self) -> bool:
        return bool(self.control_secret.get_secret_value())

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fintrack.db"

    @property
    def keyfile_path(self) -> Path:
        return self.data_dir / "keyfile.json"

    def _ports(self) -> list[int]:
        ports = [self.port]
        if self.dev and self.dev_port != self.port:
            ports.append(self.dev_port)
        return ports

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"{name}:{p}" for p in self._ports() for name in ("127.0.0.1", "localhost")}

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self.allowed_hosts}


def load_settings() -> Settings:
    """Settings for this process. FINTRACK_HOME (real environment only) selects the settings
    file ``FINTRACK_HOME/fintrack.env`` and the default data folder ``FINTRACK_HOME/data``;
    without it everything stays in the repo layout, exactly as before."""
    # Passed explicitly so a FINTRACK_HOME line inside a settings file can never disagree
    # with the file that was actually read.
    return Settings(_env_file=settings_file(), fintrack_home=fintrack_home())


def validation_summary(exc: ValidationError) -> str:
    """Which settings are wrong and how ("control_secret: value_error"), for logs.

    Only field names and pydantic error types: never the values (a settings error can be
    about CONTROL_SECRET or a Plaid key, and the message would otherwise quote it)."""
    parts = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        field = ".".join(str(p) for p in err.get("loc", ())) or "(settings)"
        parts.append(f"{field}: {err.get('type', 'invalid')}")
    return ", ".join(parts) or "invalid settings"


@lru_cache
def get_settings() -> Settings:
    return load_settings()
