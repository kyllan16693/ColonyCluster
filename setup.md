aspberry Pi Cluster Setup - Steps Completed to Date
Here's a summary of the steps we have successfully completed to build your Raspberry Pi cluster:
	1.	Initial Setup - Head Node (pi1):
	▪	Installed Raspberry Pi OS Lite (64-bit) on an SD card for the head node (pi1).
	▪	Configured hostname (pi-cluster), username (pi), password, WiFi, and enabled SSH during OS customization using Raspberry Pi Imager.
	▪	Booted the head node (pi1).
	▪	Connected to the head node via SSH.
	2.	Static IP for eth0 on Head Node (pi1):
	▪	Configured the onboard Ethernet interface (eth0) on pi1 with a static IP address 192.168.3.101/24 for the internal cluster network using nmcli.
	▪	Verified the configuration using nmcli to ensure eth0 has the static IP and wlan0 is connected to WiFi.
	3.	DHCP Server Installation and Configuration on pi1:
	▪	Installed the isc-dhcp-server package on pi1.
	▪	Configured /etc/dhcp/dhcpd.conf to act as a DHCP server for the 192.168.3.0/24 network, assigning IPs from 192.168.3.102 onwards, and setting a static IP for the head node itself (192.168.3.101).
	▪	Configured /etc/default/isc-dhcp-server to listen on eth0.
	▪	Modified /etc/hosts to include entries for cluster hostname and 192.168.3.101.
	▪	Initially encountered an error when starting isc-dhcp-server, but resolved it (likely by rebooting and re-checking the configuration).
	▪	Verified that the isc-dhcp-server service is running using systemctl status isc-dhcp-server.service.
	4.	Static DHCP Leases for Compute Nodes (pi2 to pi8):
	▪	Examined /var/lib/dhcp/dhcpd.leases to see the initially assigned (dynamic) IP addresses and MAC addresses of the compute nodes (pi2 to pi8).
	▪	Edited /etc/dhcp/dhcpd.conf to add host entries for each compute node (pi2 to pi8), assigning static IP addresses in the range 192.168.3.102 to 192.168.3.108 and corresponding hostnames (pi2 to pi8) based on their MAC addresses.
	▪	Restarted the isc-dhcp-server service on pi1.
	▪	Rebooted the compute nodes (pi2 to pi8).
	▪	Verified in /var/lib/dhcp/dhcpd.leases that the compute nodes received the configured static IP addresses.
	▪	Confirmed basic network connectivity by pinging compute nodes from the head node using both IP addresses and hostnames (if /etc/hosts was updated).
	5.	enabled ssh password authentication:
    sudo nano /etc/ssh/sshd_config
