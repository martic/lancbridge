#!/bin/bash
# setup_wifi.sh — configure the Pi's Wi-Fi to auto-connect to the camera's hotspot.
# Usage: sudo ./setup_wifi.sh "<SSID>" "<password>"
#   or interactive: sudo ./setup_wifi.sh
#
# Uses NetworkManager (Raspberry Pi OS Bookworm and later).
# The connection is saved system-wide, marked autoconnect, so the Pi brings
# the link up whenever the camera's network is visible, and retries forever.

set -e

SSID="$1"
PASS="$2"
IFACE="${IFACE:-wlan0}"
CON_NAME="camera-ap"

if [ -z "$SSID" ]; then
    echo "Scanning for nearby networks..."
    nmcli device wifi rescan ifname "$IFACE" 2>/dev/null || true
    sleep 3
    nmcli -f SSID,SIGNAL,SECURITY device wifi list ifname "$IFACE"
    echo
    read -rp "SSID to connect to (camera's hotspot name): " SSID
fi
if [ -z "$PASS" ]; then
    read -rsp "Password (input hidden): " PASS; echo
fi

# Remove any old connection with the same name
nmcli connection delete "$CON_NAME" 2>/dev/null || true

# Create the saved connection: autoconnect + retry forever + weak-signal tolerance
nmcli connection add type wifi ifname "$IFACE" con-name "$CON_NAME" \
    ssid "$SSID" \
    802-11-wireless-security.psk "$PASS" \
    802-11-wireless-security.key-mgmt wpa-psk \
    connection.autoconnect yes \
    connection.autoconnect-retries 0 \
    wifi.powersave 2

# Bring it up now
echo "Connecting..."
if nmcli connection up "$CON_NAME"; then
    IP=$(nmcli -f IP4.ADDRESS connection show "$CON_NAME" | awk '{print $2}')
    echo
    echo "=== CONNECTED ==="
    echo "Interface: $IFACE  IP: $IP  SSID: $SSID"
    echo
    echo "The Pi will now auto-connect to the camera's Wi-Fi whenever it is on"
    echo "and in range — including after reboot or the camera power-cycling."
    echo
    echo "Next step: run the discovery tool —"
    echo "  sudo python3 /opt/lancbridge/camapi.py discover"
    echo "or probe a guessed camera IP (usually 192.168.5.1 or 10.0.0.1):"
    echo "  sudo python3 /opt/lancbridge/camapi.py probe http://<camera-ip>:10000/"
else
    echo "Connection failed. Check SSID/password, and that the camera's Wi-Fi is on."
    exit 1
fi
