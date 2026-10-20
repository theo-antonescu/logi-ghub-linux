#!/bin/sh
# Record what Logitech G HUB sends to a device, from a Windows VM on Linux.
#
#   ./ghub-vm.sh install Win11.iso   create the VM and boot the Windows installer
#   ./ghub-vm.sh capture [046d:c357 ...]  start Windows with the devices passed through; each one's
#                                    USB traffic is written to ~/ghub-vm/capture-<time>-<pid>.pcap
#   ./ghub-vm.sh start               start Windows without passing any device through
#   ./ghub-vm.sh delete              remove the VM (captures are kept)
#
# Needs: qemu-system-x86, OVMF, swtpm, KVM access, and write access to the device's
# /dev/bus/usb node (see 71-logi-ghub-capture.rules). Plug the device in by USB cable.
set -eu

VM="${GHUB_VM_DIR:-$HOME/ghub-vm}"
DISK="$VM/windows.qcow2"
VARS="$VM/OVMF_VARS.fd"
CODE=/usr/share/OVMF/OVMF_CODE_4M.ms.fd

tpm() {
    mkdir -p "$VM/tpm"
    if [ ! -S "$VM/tpm.sock" ] || ! pgrep -f "swtpm socket --tpmstate dir=$VM/tpm" >/dev/null; then
        rm -f "$VM/tpm.sock"
        swtpm socket --tpmstate dir="$VM/tpm" --ctrl type=unixio,path="$VM/tpm.sock" --tpm2 --daemon
        sleep 0.5
    fi
}

run() {
    tpm
    # GHUB_VM_NONET=1 starts Windows without network (lets setup skip the Microsoft account)
    if [ "${GHUB_VM_NONET:-0}" = 1 ]; then NET="-nic none"; else NET="-netdev user,id=net0 -device e1000e,netdev=net0,romfile="; fi
    exec qemu-system-x86_64 \
        -name "G HUB capture VM" -machine q35,accel=kvm -cpu host -smp 4 -m 8G \
        -drive if=pflash,format=raw,readonly=on,file="$CODE" \
        -drive if=pflash,format=raw,file="$VARS" \
        -chardev socket,id=chrtpm,path="$VM/tpm.sock" -tpmdev emulator,id=tpm0,chardev=chrtpm -device tpm-tis,tpmdev=tpm0 \
        -device ahci,id=ahci -drive id=disk,file="$DISK",if=none,format=qcow2 -device ide-hd,drive=disk,bus=ahci.0,bootindex=1 \
        $NET \
        -device qemu-xhci,id=xhci -device usb-tablet,bus=xhci.0 \
        -vga std -display gtk \
        "$@"
}

case "${1:-}" in
install)
    ISO="${2:?give the path to the Windows 11 ISO}"
    mkdir -p "$VM"
    [ -f "$DISK" ] || qemu-img create -f qcow2 "$DISK" 64G
    [ -f "$VARS" ] || cp /usr/share/OVMF/OVMF_VARS_4M.ms.fd "$VARS"
    run -drive id=cd,file="$ISO",if=none,media=cdrom,readonly=on -device ide-cd,drive=cd,bus=ahci.1,bootindex=0
    ;;
capture)
    shift
    [ $# -gt 0 ] || set -- 046d:c357   # G915 X LIGHTSPEED TKL over USB
    stamp=$(date +%Y%m%dT%H%M%S)
    devices=""
    for d in "$@"; do
        vid="${d%%:*}"; pid="${d##*:}"
        out="$VM/capture-$stamp-$pid.pcap"
        echo "Recording USB traffic of $d to $out"
        devices="$devices -device usb-host,bus=xhci.0,vendorid=0x$vid,productid=0x$pid,pcap=$out"
    done
    # shellcheck disable=SC2086
    run $devices
    ;;
start)
    run
    ;;
delete)
    pkill -f "swtpm socket --tpmstate dir=$VM/tpm" || true
    rm -rf "$DISK" "$VARS" "$VM/tpm" "$VM/tpm.sock"
    echo "VM removed; captures kept in $VM"
    ;;
*)
    sed -n '2,13p' "$0"
    exit 1
    ;;
esac
