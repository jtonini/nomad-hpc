#!/bin/bash
#
# NØMAÐ Test Cluster - Manual Setup Script
#
# Sets up SLURM on an existing Ubuntu 22.04/24.04 VM.
# Run as root or with sudo.
#
# Usage:
#   sudo ./setup-slurm.sh
#

set -e

# Check for root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)" 
   exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "NØMAÐ Test Cluster Setup"
echo "=========================================="
echo ""

# Update system
echo "[1/7] Updating system..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y

# Install dependencies
echo "[2/7] Installing dependencies..."
apt-get install -y \
    build-essential \
    git \
    python3 \
    python3-pip \
    python3-venv \
    munge \
    slurm-wlm \
    slurm-client \
    vim \
    htop \
    tree

# Configure MUNGE
echo "[3/7] Configuring MUNGE..."
dd if=/dev/urandom bs=1 count=1024 > /etc/munge/munge.key 2>/dev/null
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key

systemctl enable munge
systemctl restart munge

# Create slurm user
echo "[4/7] Creating SLURM user and directories..."
id -u slurm &>/dev/null || useradd -r -s /bin/false slurm

mkdir -p /var/spool/slurm/ctld
mkdir -p /var/spool/slurm/d
mkdir -p /var/log/slurm
mkdir -p /var/run/slurm
mkdir -p /etc/slurm/prolog.d
mkdir -p /etc/slurm/epilog.d

chown -R slurm:slurm /var/spool/slurm
chown -R slurm:slurm /var/log/slurm
chown -R slurm:slurm /var/run/slurm

# Copy SLURM configuration
echo "[5/7] Installing SLURM configuration..."
cp "$SCRIPT_DIR/slurm/slurm-minimal.conf" /etc/slurm/slurm.conf
cp "$SCRIPT_DIR/slurm/cgroup.conf" /etc/slurm/cgroup.conf

chown slurm:slurm /etc/slurm/*.conf
chmod 644 /etc/slurm/*.conf

# Start SLURM services
echo "[6/7] Starting SLURM services..."
systemctl enable slurmctld
systemctl enable slurmd
systemctl restart slurmctld
systemctl restart slurmd

# Wait for SLURM to start
sleep 3

# Set nodes to idle
echo "[7/7] Initializing nodes..."
scontrol update nodename=nomad-test state=idle 2>/dev/null || true

# Create test directories - mimicking real HPC structure
echo "[7/8] Creating filesystem structure..."

# ============================================
# SHARED STORAGE (simulates NFS)
# ============================================

# /home - User home directories (NFS in real cluster)
# Already exists, but set quotas simulation
mkdir -p /home/testuser
mkdir -p /home/testuser/projects

# /scratch - Shared scratch space (NFS, no backup)
mkdir -p /scratch
chmod 1777 /scratch  # Sticky bit like /tmp

# /project - Project storage (NFS, backed up)
mkdir -p /project
chmod 755 /project

# ============================================
# LOCAL STORAGE (simulates local SSD)
# ============================================

# /localscratch - Local SSD on each node (fast I/O)
# In real cluster, this is node-local. Here we simulate with tmpfs.
mkdir -p /localscratch
mount -t tmpfs -o size=2G tmpfs /localscratch 2>/dev/null || true
chmod 1777 /localscratch

# Add to fstab for persistence
grep -q '/localscratch' /etc/fstab || echo 'tmpfs /localscratch tmpfs size=2G 0 0' >> /etc/fstab

# /tmp - System temp (also local)
# Already exists

# ============================================
# SOFTWARE/MODULES (simulates module system)
# ============================================

mkdir -p /opt/apps
mkdir -p /etc/modulefiles

# ============================================
# CREATE TEST USERS
# ============================================

echo "[8/8] Creating test users..."

# Create test users with different usage patterns
for user in testuser alice bob charlie; do
    id -u $user &>/dev/null || useradd -m $user
    mkdir -p /home/$user/projects
    mkdir -p /scratch/$user
    chown -R $user:$user /home/$user /scratch/$user
done

# Create a group for project storage
groupadd -f researchers
usermod -aG researchers testuser
usermod -aG researchers alice
mkdir -p /project/research
chown root:researchers /project/research
chmod 2775 /project/research  # SGID

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "SLURM Status:"
sinfo || echo "(sinfo may take a moment to work)"
echo ""
echo "Quick test:"
echo "  sinfo                        # View cluster status"
echo "  sbatch --wrap='hostname'     # Submit test job"
echo "  squeue                       # View queue"
echo ""
echo "Start job generator:"
echo "  ./start-job-generator.sh"
echo ""
