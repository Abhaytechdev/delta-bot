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
3. Claude may research and change strategy/risk settings for backtesting and testnet, and must explain every such change in the end-of-task summary. Real-money / production settings change only with my approval.
4. Every order must have a stop-loss AND a target (take-profit).
5. Commit to git after every working change with a clear message.
6. After every commit, also run git push.

## Risk rules (editable in config.yaml, defaults below)
- Pairs: BTCUSD, ETHUSD perpetual
- Timeframe: 4h (swing trading; chosen by backtest)
- Max leverage: 3x
- Risk per trade: 1% of balance
- Max open positions: 2
- Stop-loss at market structure (beyond the setup swing); at TP1 (2R) the SL moves to breakeven (no partial booking; 30% partial was tried and backtested worse); the rest trails behind swings (runner target 10R)
- Setups come from knowledge/core.md + core.yaml (market structure, candles, psychology, sessions), scored by confluence
- Daily loss limit: 3% → bot stops trading for the day
- Kill switch: local command `python -m bot.stop` halts all trading immediately

## Tech stack
- Python 3.11+, virtualenv in ./venv
- Delta Exchange official REST client + WebSocket for live prices
- SQLite for trades and logs
- No Telegram. Status/alerts go to log files; commands run locally from the terminal
- Runs as a systemd service, separate from Claude Code sessions
- Logs rotated with logrotate

## Daily report
- Cron at 23:55 IST: summarize the day's trades (P&L, win rate, drawdown, best/worst trade), get analysis from Claude, save to reports/ folder
- Report is suggestions only, never auto-changes the strategy

## Build order
1. Project structure, config, .env template, .gitignore
2. Delta API connection test (demo)
3. Market data + indicators
4. Order placement with stop-loss + target (demo)
5. Kill switch + status commands (local)
6. Risk manager
7. News module
8. Backtesting
9. Daily report
Work autonomously through the steps; don't stop to ask for OK after each one. Commit + push as you go, and give a short summary (in Hinglish) at the end.
