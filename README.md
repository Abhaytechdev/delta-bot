# Delta Exchange swing bot (testnet)

Knowledge-driven swing trading bot for Delta Exchange India perpetuals (BTCUSD, ETHUSD, 4h).
Rules and safety: see `CLAUDE.md`. Trading knowledge: `knowledge/core.md` (+ `core.yaml`).

## Commands
```sh
./venv/bin/python -m bot.check                 # testnet connection check (read-only)
./venv/bin/python -m bot.status                # balance, positions, today's P&L, events
./venv/bin/python -m bot.stop                  # KILL SWITCH: no new trades (SL/TP stay)
./venv/bin/python -m bot.stop --close-all      # kill switch + close every position
./venv/bin/python -m bot.stop --resume         # clear the kill switch
./venv/bin/python -m bot.backtest --tf 4h      # backtest (3y, last 30% out-of-sample)
./venv/bin/python -m bot.report                # today's report -> reports/
./venv/bin/python -m pytest -q                 # tests
```

## Run 24/7
```sh
sudo sh deploy/install-service.sh              # systemd service, auto-restart
journalctl -u delta-bot -f                     # live logs (also logs/bot.log)
```
Cron (already installed for user `trader`): daily report 23:55 IST, log rotation at midnight UTC.
