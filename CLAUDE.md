# Delta Exchange Trading Bot

## Goal
Crypto algo trading bot for Delta Exchange India (perpetual futures).
Hybrid strategy: news-driven + technical analysis.
- Strong positive news + bullish technical setup → long
- Strong negative news + bearish technical setup → short
- No major news → normal technical strategy

## Hard safety rules (NEVER break these)
1. DEMO/TESTNET ONLY until I explicitly say "go live". Never use production API URLs or live keys without my approval.
2. API keys only in .env (chmod 600). Never print, log, or commit keys. .env must be in .gitignore.
3. Never change strategy, risk settings, or live config on your own. Only suggest; I approve.
4. Every order must have a stop-loss.
5. Commit to git after every working change with a clear message.

## Risk rules (editable in config.yaml, defaults below)
- Pairs: BTCUSD, ETHUSD perpetual
- Timeframe: 15m
- Max leverage: 3x
- Risk per trade: 1% of balance
- Max open positions: 2
- Daily loss limit: 3% → bot stops trading for the day
- Kill switch: /stop command halts all trading immediately

## Tech stack
- Python 3.11+, virtualenv in ./venv
- Delta Exchange official REST client + WebSocket for live prices
- SQLite for trades and logs
- Telegram bot for alerts + commands (/status, /positions, /pnl, /stop)
- Runs as a systemd service, separate from Claude Code sessions
- Logs rotated with logrotate

## Daily report
- Cron at 23:55 IST: summarize the day's trades (P&L, win rate, drawdown, best/worst trade), get analysis from Claude, send to Telegram
- Report is suggestions only, never auto-changes the strategy

## Build order
1. Project structure, config, .env template, .gitignore
2. Delta API connection test (demo)
3. Market data + indicators
4. Order placement with stop-loss (demo)
5. Telegram alerts + kill switch
6. Risk manager
7. News module
8. Backtesting
9. Daily report
Work step by step. After each step, explain what you did and wait for my OK before the next.
