#!/bin/sh -e

# add route from localhost to core/aux networks via router VM
sudo route -n add -net 192.168.1.0/24 192.168.0.203
sudo route -n add -net 192.168.2.0/24 192.168.0.203

