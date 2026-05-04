#! /bin/bash
qemu-system-x86_64 \
    -m 4G \
    -accel kvm \
    -cpu host \
    -smp 2 \
    -drive file=ubuntu.img,index=0,format=qcow2,media=disk \
    -drive file=cloud-init/seed.img,index=1,media=cdrom \
    -netdev bridge,id=net0,br=virbr0 \
    -device virtio-net-pci,netdev=net0 \
    -nographic

