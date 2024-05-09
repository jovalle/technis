# Set credentials for vSphere provider
# export VSPHERE_USER="administrator@vsphere.local"
# export VSPHERE_PASSWORD=""
# export VSPHERE_SERVER=vsphere.local

provider "vsphere" {
    # Self-signed cert for now
    allow_unverified_ssl = true
}

data "vsphere_datacenter" "dc" {
    name = "technis"
}

data "vsphere_compute_cluster" "cluster" {
    name = "local"
    datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "iso_datastore" {
    name = "tier0"
    datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "datastore" {
    name = "tier1"
    datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_host" "host" {
    name = "core.technis.net"
    datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_network" "network" {
    name = "VM Network"
    datacenter_id = data.vsphere_datacenter.dc.id
}

locals {
   vms = {
    "k8s-0" = { mac_addr = "74:61:6c:6f:73:10" },
    "k8s-1" = { mac_addr = "74:61:6c:6f:73:11" },
    "k8s-2" = { mac_addr = "74:61:6c:6f:73:12" },
    "k8s-3" = { mac_addr = "74:61:6c:6f:73:13" },
    "k8s-4" = { mac_addr = "74:61:6c:6f:73:14" },
    "k8s-5" = { mac_addr = "74:61:6c:6f:73:15" },
  }
}

resource "vsphere_virtual_machine" "vm" {
    for_each = local.vms

    name = each.key
    resource_pool_id = data.vsphere_compute_cluster.cluster.resource_pool_id
    datastore_id = data.vsphere_datastore.datastore.id

    num_cpus = 8
    num_cores_per_socket = 8
    memory = 16384
    guest_id = "debian11_64Guest"

    wait_for_guest_net_timeout = 0
    wait_for_guest_ip_timeout  = 0

    network_interface {
        network_id = data.vsphere_network.network.id
        use_static_mac = true
        mac_address = each.value.mac_addr
    }

    disk {
        label = "disk0"
        size  = 240
        thin_provisioned = true
    }

    cdrom {
        datastore_id = data.vsphere_datastore.iso_datastore.id
        path         = "iso/metal-amd64.iso"
    }
}