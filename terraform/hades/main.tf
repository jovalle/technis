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
  name          = "local"
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "datastore" {
  name          = "tier1"
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_host" "host" {
  name          = "core.technis.net"
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_network" "network" {
  name          = "VM Network"
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_virtual_machine" "template" {
  name          = "templates/debian12"
  datacenter_id = data.vsphere_datacenter.dc.id
}

locals {
  vms = {
    "cerberus" = { mac_addr = "68:61:64:65:73:11" },
    "zagreus"  = { mac_addr = "68:61:64:65:73:12" },
    "thanatos" = { mac_addr = "68:61:64:65:73:13" },
    "orpheus"  = { mac_addr = "68:61:64:65:73:14" },
  }
}

resource "vsphere_virtual_machine" "vm" {
  for_each = local.vms

  name             = each.key
  resource_pool_id = data.vsphere_compute_cluster.cluster.resource_pool_id
  datastore_id     = data.vsphere_datastore.datastore.id

  num_cpus             = 8
  num_cores_per_socket = 8
  memory               = 16384
  guest_id             = "debian11_64Guest"

  network_interface {
    network_id     = data.vsphere_network.network.id
    use_static_mac = true
    mac_address    = each.value.mac_addr
  }

  disk {
    label            = "disk0"
    size             = 480
    thin_provisioned = true
  }

  clone {
    template_uuid = data.vsphere_virtual_machine.template.id
  }
}