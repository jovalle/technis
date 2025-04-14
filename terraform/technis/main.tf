locals {
  vms = {
    "k8s1" = { id = 1, node = "ms-01" },
    "k8s2" = { id = 2, node = "ms-02" },
    "k8s3" = { id = 3, node = "ms-03" },
  }
}

resource "proxmox_vm_qemu" "vm" {
  boot     = "order=ide2;scsi0"
  cores    = 8
  cpu_type = "host"
  disks {
    ide {
      ide2 {
        cdrom {
          iso = "cephfs:iso/talos-${var.talos_version}-nocloud-amd64.iso"
        }
      }
    }
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
  for_each = local.vms
  memory   = 16384
  name     = each.key
  network {
    id      = 0
    bridge  = "vmbr0"
    macaddr = "74:63:68:6e:73:8${each.value.id}"
    model   = "virtio"
  }
  onboot      = true
  qemu_os  = "other" # kernel newer than 5.x
  scsihw   = "virtio-scsi-single"
  sockets  = 1
  tags     = "k8s"
  target_node = each.value.node
  vmid     = "10${each.value.id}"
}

variable "talos_version" {
  default = "v1.9.5"
}
