# Recording G HUB's USB traffic

Some newer Logitech features (for example the G915 X's Profile Management, 0x8101,
which holds its custom lighting effects) are undocumented. The safe way to learn them
is to watch G HUB talk to the device. `ghub-vm.sh` runs Windows in QEMU with the
device passed through, and QEMU writes every USB packet to a pcap file (open it in
Wireshark, or give it to `logi_ghub.tools.pcap` for decoding).

1. Install the tools and allow passing the device through (once):

       sudo apt install swtpm swtpm-tools
       sudo cp tools/ghub-capture/71-logi-ghub-capture.rules /etc/udev/rules.d/
       sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb

2. Download the Windows 11 ISO from https://www.microsoft.com/software-download/windows11
   and install Windows: `tools/ghub-capture/ghub-vm.sh install ~/Downloads/Win11_*.iso`.
   Install G HUB inside it. Activation is not needed.
3. Plug the device in by USB cable and start a recording:
   `tools/ghub-capture/ghub-vm.sh capture 046d:c357`. In G HUB, open the device and the
   pages you want recorded, then shut Windows down.
4. The recording is in `~/ghub-vm/capture-*.pcap`. `ghub-vm.sh delete` removes the VM.

Disk use: the ISO is about 7 GB and the VM disk grows to about 25 GB.
