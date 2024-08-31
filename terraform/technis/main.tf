locals {
  vms = {
    "k8s1" = { id = "101", node = "ms-01", mac_addr = "74:63:68:6e:73:81" },
    "k8s2" = { id = "102", node = "ms-02", mac_addr = "74:63:68:6e:73:82" },
    "k8s3" = { id = "103", node = "ms-03", mac_addr = "74:63:68:6e:73:83" },
  }
}

resource "proxmox_vm_qemu" "vm" {
  for_each    = local.vms
  name        = each.key
  vmid        = each.value.id
  target_node = each.value.node
  clone       = "talos-nocloud"
  onboot      = true
  cpu         = "host"
  cores       = 8
  memory      = 16384
  scsihw      = "virtio-scsi-single"
  qemu_os     = "other" # kernel newer than 5.x
  boot        = "order=scsi0"
  disks {
    scsi {
      scsi0 {
        disk {
          cache    = "writeback"
          size     = "100G"
          storage  = "core"
          iothread = true
          discard  = true
        }
      }
    }
  }
  network {
    bridge  = "vmbr0"
    macaddr = each.value.mac_addr
    model   = "virtio"
  }
}
