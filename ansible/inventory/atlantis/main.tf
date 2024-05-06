provider "libvirt" {
  uri = "qemu:///system"
}

variable "guests" {
  default = 9
}
variable "hostname" {
  type    = string
  default = "cyborg%03d"
}
variable "mac" {
  type    = string
  default = "3c:cd:5a:00:00:%02x"
}

data "template_file" "user_data" {
  template = "${file("${path.module}/cloud_init.cfg")}"
}

resource "libvirt_volume" "os" {
  name   = "ubuntu.qcow2"
  format = "qcow2"
  source = "/var/lib/libvirt/images/bionic-server-cloudimg-amd64.img"
  pool   = "images"
}

resource "libvirt_cloudinit_disk" "cloudinit" {
  name           = "commoninit.iso"
  user_data      = "${data.template_file.user_data.rendered}"
  pool           = "images"
}

resource "libvirt_volume" "vol"{
  name           = "${format(var.hostname, count.index + 1)}.qcow2"
  count          = "${var.guests}"
  base_volume_id = "${libvirt_volume.os.id}"
  pool           = "images"
  size           = 21474836480
}

resource "libvirt_domain" "dom" {
  name      = "${format(var.hostname, count.index + 1)}"
  memory    = 2048
  vcpu      = 2
  cloudinit = "${libvirt_cloudinit_disk.cloudinit.id}"
  count     = "${var.guests}"
  console {
    type        = "pty"
    target_type = "serial"
    target_port = "0"
  }
  disk {
    volume_id = "${element(libvirt_volume.vol.*.id, count.index)}"
  }
  graphics {
    type        = "spice"
    listen_type = "address"
    autoport    = true
  }
  network_interface {
    hostname     = "${format(var.hostname, count.index + 1)}"
    mac          = "${format(var.mac, count.index + 1)}"
    network_name = "bridged"
  }
}
