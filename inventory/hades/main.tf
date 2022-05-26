# Set credentials for for vsphere
# export VSPHERE_USER="administrator@vsphere.local"
# export VSPHERE_PASSWORD=""
# export VSPHERE_SERVER=mothership.technis.local
provider "vsphere" {
    user           = var.vsphere_user
    password       = var.vsphere_password
    vsphere_server = var.vsphere_server

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

data "vsphere_datastore" "datastore" {
    name = "SSD"
    datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_host" "host" {
    name = "nexus.technis.local"
    datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_network" "network" {
    name = "VM Network"
    datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_virtual_machine" "template" {
    name = "Templates/ubuntu-server-2204"
    datacenter_id = data.vsphere_datacenter.dc.id
}

resource "vsphere_virtual_machine" "vm" {
    name = "Ubuntu Server - zagreus"
    resource_pool_id = data.vsphere_compute_cluster.cluster.resource_pool_id
    datastore_id = data.vsphere_datastore.datastore.id

    num_cpus = 8
    memory = 16384
    guest_id = "ubuntu64Guest"

    # GPU passthrough
    # memory_reservation = 16384
    # host_system_id = data.vsphere_host.host.id
    # pci_device_id = ["0000:82:00.0"]
    # extra_config = {
    #     "hypervisor.cpuid.v0"         = "false"
    #     "pciPassthru.use64bitMMIO"    = "true"
    #     "pciPassthru.64bitMMIOSizeGB" = "32"
    # }

    network_interface {
        network_id = data.vsphere_network.network.id
        use_static_mac = true
        mac_address = "68:61:64:65:73:25"
    }

    disk {
        label = "disk0"
        size  = 480
        thin_provisioned = true
    }

    clone {
        template_uuid = data.vsphere_virtual_machine.template.id
    }
}