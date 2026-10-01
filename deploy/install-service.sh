#!/bin/sh
# Install and start the bot as a systemd service. Run once: sudo sh deploy/install-service.sh
set -e
cp /home/trader/delta-bot/deploy/delta-bot.service /etc/systemd/system/delta-bot.service
systemctl daemon-reload
systemctl enable --now delta-bot
systemctl status delta-bot --no-pager | head -5
