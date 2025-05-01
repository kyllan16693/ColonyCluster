# Colony Cluster

## Overview
A distributed simulation of ant colonies demonstrating the power of cluster computing with Raspberry Pi. This project simulates ant behavior across multiple Raspberry Pi nodes, with each node handling a separate colony of ants searching for food resources in a shared environment.

## Hardware Requirements
- 1 Raspberry Pi (main node) with desktop environment
- 7+ Raspberry Pi compute nodes (this project uses 8 Pis total)
- Network switch and Ethernet cables
- Power supply for all Raspberry Pis

## Setup Instructions

### 1. Prepare Raspberry Pi OS
- Install Raspberry Pi OS 64-bit Desktop on the main Pi
- Install Raspberry Pi OS 64-bit Lite on all compute nodes
- Configure basic network settings and enable SSH on all Pis

### 2. Install Ansible
On the main Pi, install Ansible:

```bash
sudo apt-get install ansible
```

### 3. Configure the Cluster
Clone this repository to the main Pi's desktop and run the quick start playbook:

```bash
ansible-playbook playbooks/quick_start.yml
```

This will:
- Configure the main Pi as a router (`router.yml`)
- Update all Pis to the latest software (`update_pis.yml`)
- Set up Python and virtual environments (`setup_python.yml`)
- Verify the status of all nodes (`check_status.yml`)

For detailed information about the router setup, see [setup.md](setup.md).
For information about individual playbooks, see [playbooks README](playbooks/README.md).

## Running the Simulation

Start the ant colony simulation with:

```bash
ansible-playbook playbooks/start-ant-sim.yml
```

Alternatively, use the included start script:

```bash
./start.sh
```

Or simply double-click the start script on the desktop.


## License
See the [LICENSE](LICENSE) file for details.

## References and Related Projects

This project was inspired by and built with help from the following resources:

- [Tiny Titan](https://tinytitan.github.io/) - A classroom supercomputer designed to teach parallel computing concepts
- [geerlingguy/pi-cluster](https://github.com/geerlingguy/pi-cluster) - Raspberry Pi Cluster automation by Jeff Geerling
- [Raspberry Pi Cluster Tutorial](https://www.raspberrypi.com/tutorials/cluster-raspberry-pi-tutorial/) - Official Raspberry Pi cluster tutorial


