import copy

import pytest

from bot.config import ROOT, ConfigError, Secrets, _validate, load_config, load_secrets


@pytest.fixture
def cfg():
    return load_config()


def test_default_config_is_valid(cfg):
    assert cfg["exchange"]["environment"] == "testnet"
    assert cfg["trading"]["pairs"][:2] == ["BTCUSD", "ETHUSD"]
    assert cfg["risk"]["max_leverage"] == 3


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("exchange", "environment", "production"),
        ("exchange", "rest_url", "https://api.india.delta.exchange"),
        ("exchange", "ws_url", "wss://socket.india.delta.exchange"),
        ("risk", "max_leverage", 10),
        ("risk", "require_stop_loss", False),
        ("risk", "require_take_profit", False),
    ],
)
def test_unsafe_values_rejected(cfg, section, key, value):
    bad = copy.deepcopy(cfg)
    bad[section][key] = value
    with pytest.raises(ConfigError):
        _validate(bad)


def test_env_with_loose_permissions_rejected(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DELTA_API_KEY=x\n")
    env.chmod(0o644)
    with pytest.raises(ConfigError):
        load_secrets(env)


def test_secrets_repr_is_redacted():
    s = Secrets("k1", "s1", "a1", "n1")
    assert repr(s) == "Secrets(<redacted>)"


def test_env_example_has_no_values():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("="), f"value present in .env.example: {line.split('=')[0]}"


def test_live_needs_exact_production_urls_and_allow_flag(cfg, monkeypatch):
    live = copy.deepcopy(cfg)
    live["exchange"].update(environment="live", rest_url="https://api.india.delta.exchange",
                            ws_url="wss://socket.india.delta.exchange")
    monkeypatch.delenv("DELTA_ALLOW_LIVE", raising=False)
    with pytest.raises(ConfigError):
        _validate(live)  # no allow flag
    monkeypatch.setenv("DELTA_ALLOW_LIVE", "yes")
    _validate(live)  # fully approved
    for bad in ("https://api.delta.exchange", "https://api.india.delta.exchange.evil.com", "https://cdn-ind.testnet.deltaex.org"):
        broken = copy.deepcopy(live)
        broken["exchange"]["rest_url"] = bad
        with pytest.raises(ConfigError):
            _validate(broken)


def test_testnet_config_never_accepts_production_url(cfg, monkeypatch):
    monkeypatch.setenv("DELTA_ALLOW_LIVE", "yes")
    bad = copy.deepcopy(cfg)
    bad["exchange"]["rest_url"] = "https://api.india.delta.exchange"
    with pytest.raises(ConfigError):
        _validate(bad)


def test_live_config_file_keeps_risk_limits():
    import os
    os.environ["DELTA_ALLOW_LIVE"] = "yes"
    try:
        live = load_config(ROOT / "config.live.yaml")
    finally:
        del os.environ["DELTA_ALLOW_LIVE"]
    assert live["exchange"]["environment"] == "live" and live["storage"]["db_path"] == "data/live.db"
    assert live["risk"]["max_leverage"] <= 3 and live["risk"]["risk_per_trade_pct"] <= 1.0
    assert live["risk"]["require_stop_loss"] and live["risk"]["require_take_profit"]
