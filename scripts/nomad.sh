#!/bin/bash
# NØMAÐ Bash Helper Functions
# Source this file: source /etc/nomad/nomad.sh
# Or add to ~/.bashrc: source /path/to/nomad.sh

# Colors
_NC='\033[0m'
_BOLD='\033[1m'
_CYAN='\033[0;36m'
_GREEN='\033[0;32m'
_YELLOW='\033[0;33m'

# Quick status overview
nstatus() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}nstatus${_NC} - Show NØMAÐ status overview"
        echo ""
        echo "Usage: nstatus"
        echo ""
        echo "Displays filesystem usage, queue state, and collection stats."
        return 0
    fi
    nomad status "$@"
}

# Disk analysis with derivatives
ndisk() {
    if [[ -z "$1" || "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}ndisk${_NC} - Analyze filesystem trends"
        echo ""
        echo "Usage: ndisk <path> [hours]"
        echo ""
        echo "Arguments:"
        echo "  path   Filesystem path to analyze (required)"
        echo "  hours  Hours of history to analyze (default: 24)"
        echo ""
        echo "Examples:"
        echo "  ndisk /localscratch"
        echo "  ndisk /home 48"
        return 0
    fi
    local path="$1"
    local hours="${2:-24}"
    nomad analyze --path "$path" --hours "$hours"
}

# View alerts
nalerts() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}nalerts${_NC} - View and manage alerts"
        echo ""
        echo "Usage: nalerts [options]"
        echo ""
        echo "Options:"
        echo "  -u, --unresolved  Show only unresolved alerts"
        echo "  -s, --severity    Filter by severity (info/warning/critical)"
        echo ""
        echo "Examples:"
        echo "  nalerts"
        echo "  nalerts --unresolved"
        echo "  nalerts --severity critical"
        return 0
    fi
    nomad alerts "$@"
}

# Run collection once
ncollect() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}ncollect${_NC} - Run data collection"
        echo ""
        echo "Usage: ncollect [options]"
        echo ""
        echo "Options:"
        echo "  --once       Run once and exit (default)"
        echo "  --interval N Run continuously every N seconds"
        echo "  -C NAME      Run specific collector (disk, slurm)"
        echo ""
        echo "Examples:"
        echo "  ncollect"
        echo "  ncollect -C disk"
        echo "  ncollect --interval 60"
        return 0
    fi
    if [[ -z "$1" ]]; then
        nomad collect --once
    else
        nomad collect "$@"
    fi
}

# Watch mode - live updates
nwatch() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}nwatch${_NC} - Watch NØMAÐ status (live updates)"
        echo ""
        echo "Usage: nwatch [seconds]"
        echo ""
        echo "Arguments:"
        echo "  seconds  Update interval (default: 5)"
        echo ""
        echo "Press Ctrl+C to stop."
        echo ""
        echo "Examples:"
        echo "  nwatch"
        echo "  nwatch 10"
        return 0
    fi
    local interval="${1:-5}"
    watch -n "$interval" nomad status
}

# System check
nsyscheck() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}nsyscheck${_NC} - Check system requirements"
        echo ""
        echo "Usage: nsyscheck"
        echo ""
        echo "Validates SLURM setup, database, config, and filesystems."
        echo "Shows errors and warnings with recommended fixes."
        return 0
    fi
    nomad syscheck "$@"
}

# Job I/O monitor
nmonitor() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}nmonitor${_NC} - Monitor running jobs for I/O"
        echo ""
        echo "Usage: nmonitor [options]"
        echo ""
        echo "Options:"
        echo "  --once        Run once and exit"
        echo "  -i, --interval N  Sample interval in seconds (default: 30)"
        echo "  --nfs-paths   Paths to classify as NFS"
        echo "  --local-paths Paths to classify as local"
        echo ""
        echo "Examples:"
        echo "  nmonitor --once"
        echo "  nmonitor -i 60"
        echo "  nmonitor --nfs-paths /home /scratch"
        return 0
    fi
    nomad monitor "$@"
}

# Similarity analysis
nsimilarity() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}nsimilarity${_NC} - Analyze job similarity and clustering"
        echo ""
        echo "Usage: nsimilarity [options]"
        echo ""
        echo "Options:"
        echo "  --min-samples N    Min I/O samples per job (default: 3)"
        echo "  --find-similar ID  Find jobs similar to this job ID"
        echo "  --export FILE      Export JSON for 3D visualization"
        echo ""
        echo "Examples:"
        echo "  nsimilarity"
        echo "  nsimilarity --find-similar 627"
        echo "  nsimilarity --export viz-data.json"
        return 0
    fi
    nomad similarity "$@"
}

# Tail collection log
nlog() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}nlog${_NC} - Tail NØMAÐ collection log"
        echo ""
        echo "Usage: nlog [logfile]"
        echo ""
        echo "Arguments:"
        echo "  logfile  Path to log file (default: /tmp/nomad-collect.log)"
        echo ""
        echo "Examples:"
        echo "  nlog"
        echo "  nlog /var/log/nomad/collect.log"
        return 0
    fi
    local logfile="${1:-/tmp/nomad-collect.log}"
    if [[ -f "$logfile" ]]; then
        tail -f "$logfile"
    else
        echo "Log file not found: $logfile"
        echo "Start collection first: ncollect --interval 60 &"
        return 1
    fi
}

# Quick job info from sacct
njobs() {
    if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" ]]; then
        echo -e "${_BOLD}njobs${_NC} - Show recent job history"
        echo ""
        echo "Usage: njobs [options]"
        echo ""
        echo "Options:"
        echo "  -n NUM      Number of jobs to show (default: 20)"
        echo "  -u USER     Filter by user"
        echo "  -s STATE    Filter by state (COMPLETED, FAILED, etc.)"
        echo "  -j JOBID    Show specific job"
        echo ""
        echo "Examples:"
        echo "  njobs"
        echo "  njobs -n 50"
        echo "  njobs -u jtonini"
        echo "  njobs -s FAILED"
        echo "  njobs -j 12345"
        return 0
    fi
    
    local num=20
    local user=""
    local state=""
    local jobid=""
    
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n) num="$2"; shift 2 ;;
            -u) user="$2"; shift 2 ;;
            -s) state="$2"; shift 2 ;;
            -j) jobid="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    
    local cmd="sacct --format=JobID,JobName%20,User,Partition,State,ExitCode,Elapsed,MaxRSS,MaxDiskWrite -X"
    
    if [[ -n "$jobid" ]]; then
        cmd="$cmd -j $jobid"
    else
        cmd="$cmd -n $num"
        [[ -n "$user" ]] && cmd="$cmd -u $user"
        [[ -n "$state" ]] && cmd="$cmd -s $state"
    fi
    
    eval "$cmd"
}

# Show NØMAÐ help
nhelp() {
    echo -e "${_BOLD}${_CYAN}NØMAÐ Helper Functions${_NC}"
    echo ""
    echo -e "${_GREEN}Status & Monitoring:${_NC}"
    echo "  nstatus     - Show NØMAÐ status overview"
    echo "  nwatch [s]  - Watch status (live updates every s seconds)"
    echo "  nmonitor    - Monitor running jobs for I/O patterns"
    echo "  nlog        - Tail collection log"
    echo ""
    echo -e "${_GREEN}Analysis:${_NC}"
    echo "  ndisk PATH  - Analyze filesystem trends"
    echo "  njobs       - Show recent job history"
    echo "  nsimilarity - Analyze job similarity and clustering"
    echo "  nalerts     - View alerts"
    echo ""
    echo -e "${_GREEN}Operations:${_NC}"
    echo "  ncollect    - Run data collection"
    echo "  nsyscheck   - Check system requirements"
    echo ""
    echo "Run any command with 'help' for details:"
    echo "  ndisk help"
    echo "  njobs help"
}

# Print load message
echo -e "${_CYAN}NØMAÐ helpers loaded. Type 'nhelp' for commands.${_NC}"
