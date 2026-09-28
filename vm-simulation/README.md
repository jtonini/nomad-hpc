# NØMAÐ VM Simulation Environment

Test and develop NØMAÐ without access to a real HPC cluster.

**Author:** Joao Tonini (jtonini@richmond.edu)

## Cluster Configurations

| Config | Nodes | Cores | GPUs | RAM | Storage | Use Case |
|--------|-------|-------|------|-----|---------|----------|
| **minimal** | 1 | 64 | 0 | 512 GB | 10 TB | Quick testing, CI/CD |
| **small** | 18 | 1,184 | 32 | 7.5 TB | 100 TB | Development, demos |
| **large** | 300 | 16,000 | 400 | 156 TB | 10 PB | Stress testing, papers |

### Node Types

| Type | Cores | RAM | GPUs | Local NVMe |
|------|-------|-----|------|------------|
| cpu-standard | 64 | 512 GB | 0 | 1 TB |
| cpu-highmem | 64 | 1.5 TB | 0 | 1 TB |
| gpu | 32 | 256 GB | 4x A100-80GB | 2 TB |

### Small Cluster (18 nodes)
```
 8 cpu-standard  (64c, 512 GB)
 2 cpu-highmem   (64c, 1.5 TB)
 8 gpu           (32c, 256 GB, 4x A100-80GB)
```

### Large Cluster (300 nodes)
```
150 cpu-standard (64c, 512 GB)
 50 cpu-highmem  (64c, 1.5 TB)
100 gpu          (32c, 256 GB, 4x A100-80GB)
```

## Quick Start

### Option 1: Use Sample Data (No VM Required)

```bash
cd vm-simulation/sample-data
python3 -c "
import json
with open('overnight-metrics.json') as f:
    data = json.load(f)
print(f'Loaded {len(data)} samples')
print(f'First: {data[0]}')
"
```

### Option 2: Run Metrics Simulator

```bash
cd vm-simulation/simulation

# Show cluster summary
python3 metrics-generator.py --config large --summary

# Generate sample data
python3 metrics-generator.py --config small --duration 3600 --realtime

# Generate datasets for all configs
python3 metrics-generator.py --config minimal --generate-samples
python3 metrics-generator.py --config small --generate-samples
python3 metrics-generator.py --config large --generate-samples
```

### Option 3: Full VM Simulation

```bash
# Install libvirt and vagrant (RHEL/Rocky/Alma)
sudo dnf install -y libvirt libvirt-devel qemu-kvm
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt $USER
newgrp libvirt

# Install Vagrant
sudo dnf config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
sudo dnf install -y vagrant
vagrant plugin install vagrant-libvirt

# SELinux setup (RHEL/Rocky - required!)
mkdir -p ~/libvirt/images
sudo virsh pool-define-as vm-pool dir --target /home/$USER/libvirt/images
sudo virsh pool-start vm-pool
sudo virsh pool-autostart vm-pool
sudo semanage fcontext -a -t virt_image_t "/home/$USER/libvirt/images(/.*)?"
sudo restorecon -Rv ~/libvirt/images
chmod 711 /home/$USER

# Start VM
cd vm-simulation
vagrant up --provider=libvirt
vagrant ssh
```

## Job Generator

Generate synthetic HPC jobs with realistic I/O patterns.

```bash
# Preview jobs
python3 jobs/job-generator.py --config small --list-profiles

# Submit 10 jobs (in VM)
python3 jobs/job-generator.py --config minimal --once 10

# Dry run (preview without submitting)
python3 jobs/job-generator.py --config large --once 20 --dry-run

# Continuous generation
python3 jobs/job-generator.py --config small --rate 30  # 30 jobs/hour
```

### Job Profiles

Profiles vary by cluster size. Example for **small** cluster:

| Profile | Partition | Weight | Resources | Description |
|---------|-----------|--------|-----------|-------------|
| compute_small | standard | 25% | 4-16c, 8-64GB | Basic compute |
| compute_medium | standard | 15% | 16-32c, 64-256GB | Medium jobs |
| compute_large | standard | 8% | 32-64c, 128-400GB | Large jobs |
| highmem_genomics | highmem | 6% | 16-32c, 512GB-1TB | Assembly |
| gpu_training | gpu | 12% | 8-16c, 1 GPU | ML training |
| gpu_multi | gpu | 6% | 16-32c, 4 GPUs | Multi-GPU |
| bad_io_nfs_heavy | standard | 8% | 8-32c | BAD: Heavy NFS |

## SLURM Configurations

Switch between cluster sizes:

```bash
# In VM
sudo cp /vagrant/slurm/slurm-small.conf /etc/slurm/slurm.conf
sudo systemctl restart slurmctld
sinfo
```

Available configs:
- `slurm-minimal.conf` - Single node (matches VM)
- `slurm-small.conf` - 18 nodes (simulated)
- `slurm-large.conf` - 300 nodes (simulated)

## Directory Structure

```
vm-simulation/
|-- Vagrantfile              # VM definition
|-- README.md                # This file
|-- nomad-test.toml         # NØMAÐ config for testing
|
|-- slurm/                   # SLURM configurations
|   |-- slurm-minimal.conf   # 1 node
|   |-- slurm-small.conf     # 18 nodes
|   |-- slurm-large.conf     # 300 nodes
|   |-- cgroup.conf          # Resource limits
|   +-- gres.conf            # GPU resources
|
|-- jobs/                    # Job generation
|   +-- job-generator.py     # Synthetic job generator
|
|-- simulation/              # Metrics simulation
|   +-- metrics-generator.py # Generate fake metrics
|
+-- sample-data/             # Pre-collected data
    |-- README.md
    |-- overnight-metrics.json
    +-- *.json               # Generated samples
```

## Testing NØMAÐ

### Collect Metrics (in VM)

```bash
python3 -c "
from nomad.collectors.disk import DiskCollector
from datetime import datetime
import toml, json, time

config = toml.load('vm-simulation/nomad-test.toml')
collector = DiskCollector(config['collectors']['disk'], db_path='/tmp/test.db')

for i in range(10):
    metrics = collector.collect()
    print(json.dumps({
        'timestamp': datetime.now().isoformat(),
        'localscratch_pct': metrics[2]['used_percent']
    }))
    time.sleep(5)
"
```

### Analyze Derivatives

```bash
python3 -c "
from nomad.analysis.derivatives import DerivativeAnalyzer
from datetime import datetime

analyzer = DerivativeAnalyzer(window_size=10)

# Simulate disk filling
for i in range(20):
    pct = min(100, i * 5)  # 0%, 5%, 10%, ...
    analyzer.add_point(datetime.now(), pct)
    vel = analyzer.first_derivative() or 0
    acc = analyzer.second_derivative() or 0
    print(f'{pct:3}%  velocity={vel:+.2f}  acceleration={acc:+.3f}')
"
```

## Troubleshooting

### SLURM nodes stuck in DOWN state

```bash
sudo scontrol update nodename=ALL state=idle
```

### Jobs not starting

```bash
sudo systemctl status slurmctld slurmd
sudo tail -f /var/log/slurm/slurmctld.log
```

### libvirt permission denied (RHEL/Rocky with SELinux)

```bash
sudo semanage fcontext -a -t virt_image_t "/home/$USER/libvirt/images(/.*)?"
sudo restorecon -Rv ~/libvirt/images
chmod 711 /home/$USER
```

### Host disk full

Create a storage pool on a disk with space:

```bash
mkdir -p ~/libvirt/images
sudo virsh pool-define-as vm-pool dir --target /home/$USER/libvirt/images
sudo virsh pool-start vm-pool
sudo virsh pool-autostart vm-pool
# Add to Vagrantfile: lv.storage_pool_name = "vm-pool"
```

### VM keeps pausing

Check memory and reduce if needed:

```bash
free -h
# In Vagrantfile: lv.memory = 4096
```

### Jobs request too many resources

The minimal VM only has 4 cores. Use `--config minimal` with job generator:

```bash
python3 jobs/job-generator.py --config minimal --once 5
```
