"""Load and validate the bot config plus secrets from .env.

Which config is used: the BOT_CONFIG environment variable (path), else config.yaml (testnet).
Live (real money) mode is allowed only when ALL of these hold: the config says `environment: live`,
its URLs are exactly the Delta India production hosts, and DELTA_ALLOW_LIVE=yes is set in the
environment (the live systemd unit sets it). Anything else is refused.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

ALLOWED_ENVIRONMENTS = {"testnet", "live"}
TESTNET_HOST_MARKERS = ("testnet",)
LIVE_URLS = {"https://api.india.delta.exchange", "wss://socket.india.delta.exchange"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Secrets:
    delta_api_key: str
    delta_api_secret: str
    anthropic_api_key: str
    news_api_key: str

    def __repr__(self) -> str:
        # Never expose secret values in logs or tracebacks.
        return "Secrets(<redacted>)"


def active_config_path() -> Path:
    p = os.environ.get("BOT_CONFIG")
    return (ROOT / p if p and not Path(p).is_absolute() else Path(p)) if p else ROOT / "config.yaml"


def is_live_config(path: Path | str | None = None) -> bool:
    with open(path or active_config_path()) as f:
        return (yaml.safe_load(f) or {}).get("exchange", {}).get("environment") == "live"


def load_config(path: Path | str | None = None) -> dict:
    with open(path or active_config_path()) as f:
        cfg = yaml.safe_load(f)
    _validate(cfg)
    return cfg


def load_secrets(env_path: Path | str = ROOT / ".env", live: bool | None = None) -> Secrets:
    """Delta keys come from DELTA_LIVE_* in live mode, DELTA_* (testnet) otherwise."""
    env_path = Path(env_path)
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            raise ConfigError(f"{env_path} permissions are {oct(mode)}; run: chmod 600 {env_path}")
        load_dotenv(env_path)
    if live is None:
        live = is_live_config()
    prefix = "DELTA_LIVE_" if live else "DELTA_"
    key, secret = os.getenv(prefix + "API_KEY", ""), os.getenv(prefix + "API_SECRET", "")
    if live and not (key and secret):
        raise ConfigError("live mode needs DELTA_LIVE_API_KEY and DELTA_LIVE_API_SECRET in .env")
    return Secrets(
        delta_api_key=key,
        delta_api_secret=secret,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        news_api_key=os.getenv("NEWS_API_KEY", ""),
    )


def check_endpoint(environment: str, url: str) -> None:
    """Refuse any endpoint that doesn't belong to the declared environment."""
    if environment == "testnet":
        if not any(m in url for m in TESTNET_HOST_MARKERS):
            raise ConfigError(f"not a testnet URL: {url}")
    elif environment == "live":
        if url not in LIVE_URLS:
            raise ConfigError(f"not a Delta India production URL: {url}")
        if os.environ.get("DELTA_ALLOW_LIVE") != "yes":
            raise ConfigError("live mode needs DELTA_ALLOW_LIVE=yes in the environment")
    else:
        raise ConfigError(f"environment '{environment}' not allowed")


def _validate(cfg: dict) -> None:
    ex = cfg["exchange"]
    if ex["environment"] not in ALLOWED_ENVIRONMENTS:
        raise ConfigError(f"environment '{ex['environment']}' not allowed")
    for key in ("rest_url", "ws_url"):
        check_endpoint(ex["environment"], ex[key])

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
    if risk["require_take_profit"] is not True:
        raise ConfigError("risk.require_take_profit must be true")

    if not cfg["trading"]["pairs"]:
        raise ConfigError("trading.pairs must not be empty")
