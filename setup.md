# Raspberry Pi Cluster Setup Guide

This document provides detailed instructions for setting up the Raspberry Pi cluster for the Colony Cluster project.

## Network Architecture

The cluster consists of:
- 1 head node (pi1) with a desktop environment
- 7 compute nodes (pi2-pi8) running headless
- Internal network: 192.168.3.0/24

## Setup Steps

### 1. Head Node (pi1) Configuration

#### 1.1 Initial Setup
1. Install Raspberry Pi OS Desktop (64-bit) using Raspberry Pi Imager
2. During OS customization:
   - Set hostname to `pi-cluster`
   - Configure username, password, and WiFi credentials
   - Enable SSH access

#### 1.2 Network Configuration
1. Configure the onboard Ethernet interface (eth0) with a static IP:
   ```bash
   sudo nmcli con mod "Wired connection 1" ipv4.addresses 192.168.3.101/24 ipv4.method manual
   ```
2. Verify the configuration:
   ```bash
   nmcli device show eth0
   ```

#### 1.3 DHCP Server Setup
1. Install the DHCP server:
   ```bash
   sudo apt-get install isc-dhcp-server
   ```
2. Configure DHCP server in `/etc/dhcp/dhcpd.conf`:
   ```
   subnet 192.168.3.0 netmask 255.255.255.0 {
     range 192.168.3.102 192.168.3.200;
     option routers 192.168.3.101;
     option domain-name-servers 8.8.8.8, 8.8.4.4;
   }
   ```
3. Configure the DHCP server to listen on eth0 in `/etc/default/isc-dhcp-server`:
   ```
   INTERFACESv4="eth0"
   ```
4. Update `/etc/hosts` with the cluster hostname and IP
5. Restart the DHCP server:
   ```bash
   sudo systemctl restart isc-dhcp-server.service
   ```

### 2. Compute Nodes (pi2-pi8) Setup

#### 2.1 Initial Setup
1. Install Raspberry Pi OS Lite (64-bit) on all compute nodes
2. Connect compute nodes to the network switch
3. Power on the nodes and they should receive IP addresses from the head node

#### 2.2 Static IP Assignment
1. Identify the MAC addresses of all compute nodes:
   ```bash
   sudo cat /var/lib/dhcp/dhcpd.leases
   ```
2. Configure static IP assignments in `/etc/dhcp/dhcpd.conf`:
   ```
   host pi2 {
     hardware ethernet AA:BB:CC:DD:EE:FF;  # Replace with actual MAC
     fixed-address 192.168.3.102;
   }
   # Repeat for pi3-pi8 with appropriate IPs
   ```
3. Restart the DHCP server and reboot compute nodes
4. Verify IP assignments:
   ```bash
   ping -c 1 192.168.3.102
   ```

### 3. SSH Configuration

Enable password authentication (if needed):
```bash
sudo nano /etc/ssh/sshd_config
# Set PasswordAuthentication yes
sudo systemctl restart sshd
```

## Next Steps

After completing this setup, you can use the Ansible playbooks to configure and manage the cluster:
```bash
ansible-playbook playbooks/quick_start.yml
```

For running the ant colony simulation, see the instructions in the [main README](README.md).
