#!/bin/sh
# Install and start the LIVE bot service. Run once: sudo sh deploy/install-live-service.sh
set -e
cp /home/trader/delta-bot/deploy/delta-bot-live.service /etc/systemd/system/delta-bot-live.service
systemctl daemon-reload
systemctl enable --now delta-bot-live
systemctl status delta-bot-live --no-pager | head -5
