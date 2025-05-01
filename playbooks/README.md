# Raspberry Pi Cluster Ansible Configuration

This directory contains Ansible configuration files and playbooks for managing a Raspberry Pi cluster with 8 nodes.

## Network Topology

1. Control node (`pi1.local`) - Connected to the internet and the internal network
2. Compute nodes (`pi2.local` through `pi8.local`) - Connected only to the internal network via a switch
3. All nodes have username `pi` and password `icer`

## Files

- `hosts.ini`: Inventory file listing all Pi nodes with appropriate grouping
- `ansible.cfg`: Ansible configuration file
- `router.yml`: Playbook to set up pi1 as a router for the compute nodes
- `setup_proxy.yml`: Playbook to set up pi1 as a proxy for other nodes (alternative to router)
- `update_pis.yml`: Playbook to update all Pi nodes using the proxy method
- `update_pis_direct.yml`: Playbook to update all Pi nodes directly through the router
- `check_status.yml`: Playbook to check the status of all Pi nodes
- `test_router.yml`: Playbook to test the router setup
- `setup_ssh_keys.sh`: Script to set up SSH keys for passwordless authentication
- `main.yml`: Main playbook that sets up the router and updates all nodes

## Initial Setup

### 1. Set up SSH Keys (Recommended)

For passwordless authentication, run:

```bash
./setup_ssh_keys.sh
```

This will generate an SSH key on pi1 and copy it to all other Pi nodes.

### 2. Set up Router for Internet Access

Since only pi1 has internet access, you need to set up pi1 as a router for the compute nodes:

```bash
ansible-playbook router.yml
```

This configures pi1 as a router with NAT and DNS forwarding, allowing all compute nodes to access the internet through pi1.

To test the router setup:

```bash
ansible-playbook test_router.yml
```

### 3. Alternative: Set up Proxy for Internet Access

If you prefer to use a proxy instead of a router:

```bash
ansible-playbook setup_proxy.yml
```

This installs and configures apt-cacher-ng on pi1 and configures the compute nodes to use it.

## Running Playbooks

### Update All Pi Nodes (Using Router)

```bash
ansible-playbook update_pis_direct.yml
```

This will update all Pi nodes directly through the router.

### Update All Pi Nodes (Using Proxy)

```bash
ansible-playbook update_pis.yml
```

This will:
- Update pi1 directly using its internet connection
- Update pi2-pi8 through the proxy on pi1

### Check Status of All Pi Nodes

```bash
ansible-playbook check_status.yml
```

### Run Everything with One Command

```bash
ansible-playbook main.yml
```

This will:
1. Set up pi1 as a router
2. Test the router setup
3. Update all Pi nodes

### Test Connectivity

To test if Ansible can connect to all nodes:

```bash
ansible pi_cluster -m ping
```

To test only compute nodes:

```bash
ansible pi_compute -m ping
```

## Notes

- The first time you run a playbook, you may be prompted to accept SSH host keys
- If you've set up SSH keys using the provided script, you won't need to enter passwords
- If you haven't set up SSH keys, Ansible will use the password specified in `hosts.ini`
- The router setup is the recommended approach for providing internet access to compute nodes 