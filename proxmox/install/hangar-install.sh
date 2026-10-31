#!/usr/bin/env bash

# Copyright (c) 2021-2026 community-scripts ORG
# Author: PresWiFi
# License: MIT | https://github.com/community-scripts/DevScripts/raw/main/LICENSE
# Source: https://github.com/PresWiFi/hangar

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
$STD apt install -y \
  git \
  tmux \
  zstd \
  ripgrep
msg_ok "Installed Dependencies"

NODE_VERSION="24" setup_nodejs

msg_info "Installing Claude Code"
$STD npm install -g @anthropic-ai/claude-code
msg_ok "Installed Claude Code"

fetch_and_deploy_gh_release "gh" "cli/cli" "binary"
PYTHON_VERSION="3.12" setup_uv
fetch_and_deploy_gh_release "hangar" "PresWiFi/hangar" "tarball"

msg_info "Setting up Hangar"
cd /opt/hangar
$STD uv venv /opt/hangar/.venv
$STD uv pip install -p /opt/hangar/.venv/bin/python -r requirements.txt
mkdir -p /opt/hangar_data /root/claude
cat <<EOF >/opt/hangar_data/hangar.env
HANGAR_WORKSPACES=/root/claude
HANGAR_DATA_DIR=/opt/hangar_data
HANGAR_AUTORESTORE=true
EOF
# Skips Claude Code's first-run screens and its folder-trust prompt for the workspace.
cat <<EOF >/root/.claude.json
{
  "hasCompletedOnboarding": true,
  "theme": "dark",
  "projects": {
    "/root/claude": {
      "hasTrustDialogAccepted": true
    }
  }
}
EOF
msg_ok "Set up Hangar"

msg_info "Creating Service"
cat <<EOF >/etc/systemd/system/hangar.service
[Unit]
Description=Hangar
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/hangar
EnvironmentFile=/opt/hangar_data/hangar.env
# Allow Claude's bypass-permissions mode as root inside the LXC.
Environment=IS_SANDBOX=1
ExecStart=/opt/hangar/.venv/bin/python -m hangar
Restart=on-failure
RestartSec=5
# Sessions live in tmux; leave them running when Hangar restarts or updates.
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
systemctl enable -q --now hangar
msg_ok "Created Service"

motd_ssh
customize
cleanup_lxc
