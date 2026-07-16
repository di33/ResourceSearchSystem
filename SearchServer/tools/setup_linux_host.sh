#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data/ResourceLibrary/swapfile}"
SWAP_FILE="${SWAP_FILE:-swapfile}"
SWAP_SIZE_GIB="${SWAP_SIZE_GIB:-8}"
SWAPPINESS="${SWAPPINESS:-10}"
SYSCTL_FILE="/etc/sysctl.d/90-searchserver-memory.conf"

if [[ "${SWAP_FILE}" != /* ]]; then
  SWAP_FILE="${DATA_DIR%/}/${SWAP_FILE}"
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run this script as root" >&2
  exit 1
fi

if ! [[ "${SWAP_SIZE_GIB}" =~ ^[1-9][0-9]*$ ]]; then
  echo "error: SWAP_SIZE_GIB must be a positive integer" >&2
  exit 1
fi

if ! [[ "${SWAPPINESS}" =~ ^[0-9]+$ ]] || (( SWAPPINESS > 100 )); then
  echo "error: SWAPPINESS must be an integer from 0 to 100" >&2
  exit 1
fi

swap_dir="$(dirname -- "${SWAP_FILE}")"
mkdir -p -- "${swap_dir}"

if [[ ! -e "${SWAP_FILE}" ]]; then
  required_kib=$((SWAP_SIZE_GIB * 1024 * 1024))
  available_kib="$(df --output=avail -k "${swap_dir}" | tail -n 1 | tr -d ' ')"
  if (( available_kib < required_kib + 1048576 )); then
    echo "error: ${swap_dir} needs at least ${SWAP_SIZE_GIB} GiB plus 1 GiB free" >&2
    exit 1
  fi

  echo "Creating ${SWAP_SIZE_GIB} GiB swap file at ${SWAP_FILE} ..."
  dd if=/dev/zero of="${SWAP_FILE}" bs=1M count=$((SWAP_SIZE_GIB * 1024)) status=progress
  chmod 600 "${SWAP_FILE}"
  mkswap "${SWAP_FILE}"
else
  chmod 600 "${SWAP_FILE}"
fi

if ! swapon --show=NAME --noheadings | awk '{$1=$1};1' | grep -Fxq "${SWAP_FILE}"; then
  echo "Enabling ${SWAP_FILE} ..."
  swapon "${SWAP_FILE}"
fi

fstab_entry="${SWAP_FILE} none swap sw 0 0"
if ! awk -v path="${SWAP_FILE}" '$1 == path && $3 == "swap" { found=1 } END { exit !found }' /etc/fstab; then
  printf '%s\n' "${fstab_entry}" >> /etc/fstab
fi

cat > "${SYSCTL_FILE}" <<EOF
# Keep swap as an emergency buffer for Milvus compaction, not primary memory.
vm.swappiness=${SWAPPINESS}
EOF
sysctl -p "${SYSCTL_FILE}"

echo "SearchServer host memory setup complete."
free -h
swapon --show
