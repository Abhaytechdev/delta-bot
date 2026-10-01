"""Load and validate config.yaml plus secrets from .env."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Safety rule 1: only testnet hosts are permitted until the owner approves going live.
ALLOWED_ENVIRONMENTS = {"testnet"}
TESTNET_HOST_MARKERS = ("testnet",)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Secrets:
    delta_api_key: str
    delta_api_secret: str
    telegram_bot_token: str
    telegram_chat_id: str
    anthropic_api_key: str
    news_api_key: str

    def __repr__(self) -> str:
        # Never expose secret values in logs or tracebacks.
        return "Secrets(<redacted>)"


def load_config(path: Path | str = ROOT / "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    _validate(cfg)
    return cfg


def load_secrets(env_path: Path | str = ROOT / ".env") -> Secrets:
    env_path = Path(env_path)
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            raise ConfigError(f"{env_path} permissions are {oct(mode)}; run: chmod 600 {env_path}")
        load_dotenv(env_path)
    return Secrets(
        delta_api_key=os.getenv("DELTA_API_KEY", ""),
        delta_api_secret=os.getenv("DELTA_API_SECRET", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        news_api_key=os.getenv("NEWS_API_KEY", ""),
    )


def _validate(cfg: dict) -> None:
    ex = cfg["exchange"]
    if ex["environment"] not in ALLOWED_ENVIRONMENTS:
        raise ConfigError(f"environment '{ex['environment']}' not allowed; testnet only")
    for key in ("rest_url", "ws_url"):
        if not any(m in ex[key] for m in TESTNET_HOST_MARKERS):
            raise ConfigError(f"exchange.{key} is not a testnet URL: {ex[key]}")

    risk = cfg["risk"]
    if not 0 < risk["max_leverage"] <= 3:
        raise ConfigError("risk.max_leverage must be in (0, 3]")
    if not 0 < risk["risk_per_trade_pct"] <= 5:
        raise ConfigError("risk.risk_per_trade_pct must be in (0, 5]")
    if not 1 <= risk["max_open_positions"] <= 10:
        raise ConfigError("risk.max_open_positions must be in [1, 10]")
    if not 0 < risk["daily_loss_limit_pct"] <= 10:
        raise ConfigError("risk.daily_loss_limit_pct must be in (0, 10]")
    if risk["require_stop_loss"] is not True:
        raise ConfigError("risk.require_stop_loss must be true")

    if not cfg["trading"]["pairs"]:
        raise ConfigError("trading.pairs must not be empty")
