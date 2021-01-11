# Atlantis

## Requirements
### Packages
#### CentOS
```
yum install qemu-kvm libvirt libvirt-dev libvirt-devel unzip mkisofs gcc
```
#### Ubuntu
```
apt install qemu-kvm qemu-utils libvirt-daemon-system libvirt-dev libvirt-clients unzip gcc && \
sed -i -- 's/security_driver = "selinux"/security_driver = "none"/g' /etc/libvirt/qemu.conf && \
systemctl restart libvirtd
```
### Dependencies
#### Golang
```
curl -O https://dl.google.com/go/go1.12.5.linux-amd64.tar.gz && \
tar -C /usr/local -zxf go1.12.5.linux-amd64.tar.gz && \
export PATH=$PATH:/usr/local/go/bin \
rm -f go1.12.5.linux-amd64.tar.gz
```
#### Terraform
```
TERRAFORM_VERSION=0.12.20 && \
curl -O https://releases.hashicorp.com/terraform/$TERRAFORM_VERSION/terraform_$TERRAFORM_VERSION_linux_amd64.zip && \
unzip terraform_$TERRAFORM_VERSION_linux_amd64.zip && \
mv terraform /usr/bin/
rm -f terraform_$TERRAFORM_VERSION_linux_amd64.zip
```
#### Terraform Provider for libvirt
```
pushd $HOME && \
go get github.com/dmacvicar/terraform-provider-libvirt && \
mkdir -p $HOME/.terraform.d/plugins/linux_amd64 && \
pushd $HOME/.terraform/plugins/linux_amd64 && \
ln -s $GOPATH/bin/terraform-provider-libvirt && \
popd
```
### Configuration
#### SSH
```
#~/.ssh/config
Host cyborg*
  User root
  IdentityFile ~/.ssh/technis
  StrictHostKeyChecking no
```
#### Bridge Networking
Configure bridge network interface for KVM instances
##### CentOS
```
#/etc/sysconfig/network-scripts/ifcfg-enp4s0
DEVICE=enp4s0
HWADDR=D0:50:99:AA:CB:CB
BOOTPROTO=dhcp
ONBOOT=yes
NM_CONTROLLED=no
BRIDGE=br0

#/etc/sysconfig/network-scripts/ifcfg-br0
DEVICE=br0
TYPE=Bridge
ONBOOT=yes
BOOTPROTO=dhcp
NM_CONTROLLED=no
DELAY=0
```
##### Ubuntu
###### ifupdown
```
#/etc/network/interfaces
auto br0
iface br0 inet dhcp
  bridge_ports enp0s31f6
  bridge_stp off
  bridge_fd 0
  bridge_maxwait 0
```
###### netplan
```
#/etc/netplan/bridged.yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    enp0s31f6:
      dhcp4: true
      dhcp6: false
  bridges:
    br0:
      interfaces: [enp0s31f6]
      dhcp4: yes
```
#### KVM (virsh)
##### Pool
```
virsh pool-define /dev/stdin <<EOF
<pool type='dir'>
  <name>images</name>
  <target>
    <path>/var/lib/libvirt/images</path>
  </target>
</pool>
EOF
virsh pool-start images
virsh pool-autostart images
```
##### Network
Bridged-network. dnsmasq server or network routing handles IPs/hostnames
```
virsh net-define /dev/stdin <<EOF
<network>
  <name>bridged</name>
  <forward mode='bridge'/>
  <bridge name='br0'/>
</network>
EOF
virsh net-start bridged
virsh net-autostart bridged
```
## Notes
### Ubuntu
Although this is mostly geared towards CentOS, on Ubuntu beware of the "Permission Denied" error caused by SELinux. Even if you disable SELinux, the bug persists. See [here](https://github.com/dmacvicar/terraform-provider-libvirt/commit/22f096d9) or just set `security_driver = "none"` in /etc/libvirt/qemu.conf and restart libvirtd
