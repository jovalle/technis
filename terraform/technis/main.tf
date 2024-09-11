variable "talos_version" {
  default = "v1.7.6"
}

locals {
  vms = {
    "k8s1" = { id = 1, node = "ms-01" },
    "k8s2" = { id = 2, node = "ms-02" },
    "k8s3" = { id = 3, node = "ms-03" },
  }
}

resource "proxmox_vm_qemu" "vm" {
  for_each = local.vms
  name     = each.key
  vmid     = "10${each.value.id}"
  tags     = "k8s"

  onboot      = true
  target_node = each.value.node
  hagroup     = "mothership"
  hastate     = "started"

  cpu     = "host"
  cores   = 8
  sockets = 1
  memory  = 16384
  scsihw  = "virtio-scsi-single"
  qemu_os = "other" # kernel newer than 5.x
  boot    = "order=ide2;scsi0"

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

  network {
    bridge  = "vmbr0"
    macaddr = "74:63:68:6e:73:8${each.value.id}"
    model   = "virtio"
  }
}
