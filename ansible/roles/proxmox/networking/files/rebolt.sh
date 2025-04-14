#!/bin/bash

# Print the current date and time
echo "Current date and time: $(date)"

# Print topology
vtysh -c "show openfabric topology"

{
  # Enable strict error handling
  set -e
  set -o pipefail

  # Check if both interfaces are connected
  vtysh -c "show openfabric topology" | grep 'TE-IS' | awk '{a[$5]++} END {if(a["thunderbolt0"] && a["thunderbolt1"]) exit 0; else exit 1}'

  # Check if neighbors are uniquely connected
  vtysh -c "show openfabric topology" | grep 'TE-IS' | awk '{print $1","$5}' | awk -F, '{if(++count[$2]>1) exit 1} END {exit 0}'
} || {
  # If any command in the block fails, this block executes.
  echo "Error encountered; restarting frr service..."
  systemctl restart frr
  echo "Waiting 60s before scanning topology..."
  sleep 60
  vtysh -c "show openfabric topology"
}
