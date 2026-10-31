#!/usr/bin/env bash
_CS_DEFAULT_URL="https://raw.githubusercontent.com/PresWiFi/hangar/main/proxmox"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")
# Copyright (c) 2021-2026 community-scripts ORG
# Author: PresWiFi
# License: MIT | https://github.com/community-scripts/DevScripts/raw/main/LICENSE
# Source: https://github.com/PresWiFi/hangar

APP="Hangar"
var_tags="${var_tags:-ai;dev-tools}"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-4096}"
var_disk="${var_disk:-16}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
#var_arm64="${var_arm64:-no}" # unset = ask the user; set yes/no only when verified
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/hangar ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  msg_info "Updating Claude Code"
  $STD npm install -g @anthropic-ai/claude-code@latest
  msg_ok "Updated Claude Code"

  if check_for_gh_release "hangar" "PresWiFi/hangar"; then
    msg_info "Stopping Hangar"
    systemctl stop hangar
    msg_ok "Stopped Hangar"

    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "hangar" "PresWiFi/hangar" "tarball"

    msg_info "Installing Hangar Dependencies"
    cd /opt/hangar
    $STD uv venv /opt/hangar/.venv
    $STD uv pip install -p /opt/hangar/.venv/bin/python -r requirements.txt
    msg_ok "Installed Hangar Dependencies"

    msg_info "Starting Hangar"
    systemctl start hangar
    msg_ok "Started Hangar"
    msg_ok "Updated successfully!"
  fi
  exit
}

start
build_container
description

msg_ok "Completed Successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Access it using the following URL:${CL}"
echo -e "${GATEWAY}${BGN}http://${IP}:7878${CL}"
echo -e "${INFO}${YW}Enter the setup code from Hangar's log on the first visit. Show it with:${CL}"
echo -e "${TAB}${BGN}pct exec ${CTID} -- journalctl -u hangar -o cat | grep -A1 'enter this code'${CL}"
