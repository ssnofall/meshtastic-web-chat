#!/bin/bash
# Raspberry Pi Dual-Mode Network Setup Script
# This configures the Pi to:
# 1. Connect to existing WiFi/Ethernet (internet side)
# 2. Broadcast its own WiFi AP (meshtastic side)
# 3. Bridge traffic between them
# 4. Run your Meshtastic web app on boot (port 80)

set -e

echo "=================================="
echo "Meshtastic Gateway Pi Setup"
echo "=================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (sudo)"
    exit 1
fi

# Configuration variables
AP_SSID="${AP_SSID:-Meshtastic-Public-Access}"
AP_PASSWORD="${AP_PASSWORD:-meshtastic}"
AP_CHANNEL="${AP_CHANNEL:-6}"
WEB_PORT="${WEB_PORT:-5000}"
INSTALL_DIR="/opt/meshtastic-web"

echo "Configuration:"
echo "  AP SSID: $AP_SSID"
echo "  AP Password: $AP_PASSWORD"
echo "  AP Channel: $AP_CHANNEL"
echo "  Web Port: $WEB_PORT"
echo ""

# Update system
echo "[1/8] Updating system packages..."
apt-get update
apt-get upgrade -y

# Install required packages
echo "[2/8] Installing required packages..."
apt-get install -y \
    hostapd \
    dnsmasq \
    iptables \
    iptables-persistent \
    python3 \
    python3-pip \
    python3-venv \
    git \
    avahi-daemon \
    avahi-utils

# Stop services while configuring
echo "[3/8] Stopping services for configuration..."
systemctl stop hostapd
systemctl stop dnsmasq

# Configure network interfaces
echo "[4/8] Configuring network interfaces..."

# Backup existing config
cp /etc/dhcpcd.conf /etc/dhcpcd.conf.backup

# Configure dhcpcd for dual interface
cat >> /etc/dhcpcd.conf << 'EOF'

# Meshtastic Gateway Configuration
# wlan0 = Access Point (static IP)
interface wlan0
    static ip_address=192.168.50.1/24
    nohook wpa_supplicant

# wlan1 or eth0 will get IP from existing network via DHCP
EOF

# Configure hostapd (Access Point)
echo "[5/8] Configuring hostapd (WiFi AP)..."

cat > /etc/hostapd/hostapd.conf << EOF
# Interface configuration
interface=wlan0
driver=nl80211

# WiFi configuration
ssid=$AP_SSID
hw_mode=g
channel=$AP_CHANNEL
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0

# Security configuration
wpa=2
wpa_passphrase=$AP_PASSWORD
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP

# Performance tuning
country_code=US
ieee80211n=1
ieee80211d=1
ht_capab=[HT40][SHORT-GI-20][DSSS_CCK-40]
EOF

# Point hostapd to config file
cat > /etc/default/hostapd << EOF
DAEMON_CONF="/etc/hostapd/hostapd.conf"
EOF

# Configure dnsmasq (DHCP/DNS)
echo "[6/8] Configuring dnsmasq (DHCP/DNS)..."

# Backup original
mv /etc/dnsmasq.conf /etc/dnsmasq.conf.backup

cat > /etc/dnsmasq.conf << EOF
# Interface configuration
interface=wlan0
dhcp-range=192.168.50.10,192.168.50.250,255.255.255.0,24h

# DNS configuration
domain=meshtastic.local
address=/meshtastic.local/192.168.50.1
address=/gateway.meshtastic.local/192.168.50.1

# DHCP options
dhcp-option=3,192.168.50.1  # Gateway
dhcp-option=6,192.168.50.1  # DNS Server

# Logging
log-queries
log-dhcp
EOF

# Configure IP forwarding and NAT
echo "[7/8] Configuring IP forwarding and NAT..."

# Enable IP forwarding
sed -i 's/#net.ipv4.ip_forward=1/net.ipv4.ip_forward=1/' /etc/sysctl.conf
sysctl -w net.ipv4.ip_forward=1

# Setup iptables rules for NAT
cat > /etc/iptables-setup.sh << 'EOF'
#!/bin/bash
# Dynamic iptables setup - detects internet interface

# Find the interface with internet access (not wlan0)
INTERNET_IF=$(ip route | grep default | awk '{print $5}' | grep -v wlan0 | head -n 1)

if [ -z "$INTERNET_IF" ]; then
    echo "No internet interface found, trying common interfaces..."
    if ip link show eth0 &> /dev/null; then
        INTERNET_IF="eth0"
    elif ip link show wlan1 &> /dev/null; then
        INTERNET_IF="wlan1"
    else
        echo "Warning: No internet interface detected"
        exit 1
    fi
fi

echo "Setting up NAT with internet interface: $INTERNET_IF"

# Flush existing rules
iptables -F
iptables -t nat -F

# NAT rules
iptables -t nat -A POSTROUTING -o $INTERNET_IF -j MASQUERADE
iptables -A FORWARD -i $INTERNET_IF -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -A FORWARD -i wlan0 -o $INTERNET_IF -j ACCEPT

# Save rules
iptables-save > /etc/iptables/rules.v4
EOF

chmod +x /etc/iptables-setup.sh
/etc/iptables-setup.sh

# Configure Avahi for mDNS
echo "[8/8] Configuring mDNS (Avahi)..."

cat > /etc/avahi/services/meshtastic.service << EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Meshtastic Gateway on %h</name>
  <service>
    <type>_http._tcp</type>
    <port>$WEB_PORT</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
EOF

# Create systemd service for iptables setup on boot
cat > /etc/systemd/system/iptables-setup.service << EOF
[Unit]
Description=Setup iptables for Meshtastic Gateway
After=network.target
Before=hostapd.service

[Service]
Type=oneshot
ExecStart=/etc/iptables-setup.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# Create application directory
echo "Creating application directory..."
mkdir -p $INSTALL_DIR
chown -R pi:pi $INSTALL_DIR

# Create systemd service for Meshtastic web app
cat > /etc/systemd/system/meshtastic-web.service << EOF
[Unit]
Description=Meshtastic Web Interface
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$INSTALL_DIR/venv/bin"
Environment="FLASK_RUN_PORT=$WEB_PORT"
Environment="FLASK_RUN_HOST=0.0.0.0"
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/app.py
Restart=always
RestartSec=10

# Capabilities to bind to port 80 without being root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

# Enable services
echo "Enabling services..."
systemctl daemon-reload
systemctl unmask hostapd
systemctl enable hostapd
systemctl enable dnsmasq
systemctl enable avahi-daemon
systemctl enable iptables-setup
systemctl enable meshtastic-web

# Create installation instructions
cat > $INSTALL_DIR/INSTALL.txt << EOF
Meshtastic Gateway Setup Complete!

- Web Interface (from AP): http://192.168.50.1:5000 or http://meshtastic.local:5000
- Web Interface (from main network): http://<pi-ip-address>:5000

The Pi will:
- Broadcast WiFi AP: $AP_SSID
- Connect to your existing network via Ethernet or WiFi
- Be accessible from both networks
- Forward traffic between networks (NAT)
- Serve your Flask app directly on port $WEB_PORT

Configuration Files:
- Hostapd: /etc/hostapd/hostapd.conf
- Dnsmasq: /etc/dnsmasq.conf
- Web Service: /etc/systemd/system/meshtastic-web.service

Service Commands:
- Start: sudo systemctl start meshtastic-web
- Stop: sudo systemctl stop meshtastic-web
- Restart: sudo systemctl restart meshtastic-web
- Status: sudo systemctl status meshtastic-web
- Logs: sudo journalctl -u meshtastic-web -f

Network Commands:
- View AP clients: sudo iw dev wlan0 station dump
- Check DHCP leases: cat /var/lib/misc/dnsmasq.leases
- Check routing: ip route
- Check NAT: sudo iptables -t nat -L -v
EOF

cat $INSTALL_DIR/INSTALL.txt

echo ""
echo "=================================="
echo "Setup Complete!"
echo "=================================="
echo ""
echo "Your Flask app will serve directly on port $WEB_PORT"
echo ""
echo "To connect to existing WiFi network (optional):"
echo "  sudo raspi-config"
echo "  -> System Options -> Wireless LAN"
echo ""
echo "Or run the WiFi setup script: sudo ./connect-wifi.sh"
echo ""
echo "Read $INSTALL_DIR/INSTALL.txt for next steps"
echo ""
echo "Reboot required: sudo reboot"
