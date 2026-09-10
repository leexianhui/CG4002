#!/usr/bin/env bash
# Generates a self-signed TLS cert/key pair for the Game Engine's WSS listener
# (COMMS_IMPLEMENTATION_PLAN.md §11 — self-signed is acceptable since relays/visualizers pin the CA
# via engine_link.ca_cert_path rather than trusting a public CA chain).
#
# Modern TLS clients (Python 3.7+, browsers) ignore the CN field for hostname verification and
# require a matching Subject Alternative Name (SAN) entry instead, so every host/IP a relay or
# visualizer will actually connect through (loopback for local testing, the real Ultra96 IP for the
# demo) must be listed here or the handshake fails with "certificate is not valid for '<host>'".
#
# Usage: comms/scripts/gen_certs.sh [output_dir] [days] [extra_san_ip_or_dns ...]
#   e.g. comms/scripts/gen_certs.sh ../certs 365 192.168.1.50
# Never commit the generated .key (or .crt, to be safe) — see comms/.gitignore / root .gitignore.

set -euo pipefail

OUT_DIR="${1:-$(dirname "$0")/../certs}"
DAYS="${2:-365}"
shift 2 2>/dev/null || shift "$#"
EXTRA_SANS=("$@")

mkdir -p "$OUT_DIR"

SAN_ENTRIES=("IP:127.0.0.1" "DNS:localhost" "DNS:cg4002-engine")
for host in "${EXTRA_SANS[@]}"; do
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN_ENTRIES+=("IP:$host")
  else
    SAN_ENTRIES+=("DNS:$host")
  fi
done
SAN_LIST=$(IFS=,; echo "${SAN_ENTRIES[*]}")

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$OUT_DIR/engine.key" \
  -out "$OUT_DIR/engine.crt" \
  -days "$DAYS" \
  -subj "/CN=cg4002-engine" \
  -addext "subjectAltName=$SAN_LIST"

echo "Wrote $OUT_DIR/engine.crt and $OUT_DIR/engine.key (self-signed, ${DAYS}d validity, SAN=$SAN_LIST)."
