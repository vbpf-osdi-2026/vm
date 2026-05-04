# Ubuntu Cloud-Init VM Launcher for Kernel Development

An integrated Python script for launching VMs with custom kernels and Ubuntu cloud-init rootfs using QEMU. Features a two-stage boot process perfect for kernel development and debugging.

## Features

- **Two-Stage Boot Process**: First boot with Ubuntu kernel for cloud-init, then development with custom kernel
- **Custom Kernel Support**: Boot your compiled kernel with Ubuntu rootfs after initial setup
- **Kernel Debugging**: GDB debugging with `-d` and `-S` flags, kernel logging with `-l`
- **Cloud-Init Integration**: Uses your custom cloud-init files for system setup (first boot only)
- **Development Workflow**: Integrated download, install, and development process
- **Daemon Mode**: Run VMs in background with logging and monitoring
- **KVM Acceleration**: High-performance virtualization with host CPU passthrough

## Quick Start

### 1. Install Dependencies

If you do not have `uv` installed yet, install it from the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# Install system packages
sudo apt install qemu-system-x86 genisoimage

# Install Python dependencies
uv sync
```

### 2. Prepare Cloud-Init Configuration

Create your cloud-init files in the `cloud-init/` directory:
- `cloud-init/user-data` - User configuration, packages, etc.
- `cloud-init/meta-data` - Instance metadata
- `cloud-init/network-config` - Network configuration

### 3. Prepare Ubuntu Rootfs

```bash
uv run vm.py --prepare
```

This will:
- Download Ubuntu 24.04 Noble cloud image (~600MB) as rootfs
- Resize image for additional space

### 4. First Boot (Install Mode)

```bash
uv run vm.py --install
```

This first boot will:
- Use Ubuntu's own kernel (not your custom kernel)
- Include cloud-init seed image for system setup
- Configure users, packages, and system settings
- After this completes, cloud-init is no longer needed

### 5. Build Your Custom Kernel

```bash
cd ~/linux-6.12.40
make -j$(nproc)
```

The script expects the kernel at `~/linux-6.12.40/arch/x86/boot/bzImage`.

### 6. Development Boots

```bash
# Regular development boot (your custom kernel)
uv run vm.py

# Kernel debugging
uv run vm.py -d -S

# Kernel logging (BPF debugging)
uv run vm.py -l

# Combined debugging
uv run vm.py -d -S -l
```

## Usage

### Command Line Options

**Setup Options:**
- `--prepare` - Download Ubuntu image
- `--iso` - Generate cloud-init seed ISO from existing files
- `--install` - First boot with Ubuntu kernel and cloud-init setup

**Launch Options:**
- `-d, --debug` - Enable debug mode (adds QEMU -s option for GDB)
- `-D, --daemon` - Run in daemon mode (background)
- `-S, --stop` - Stop at first instruction (requires -d for debugging)
- `-l, --log` - Enable kernel debug logging (BPF debugging)

**Other:**
- `-h, --help` - Show help message

### VM Specifications

- **Install Mode**: Ubuntu kernel + cloud-init seed
- **Development Mode**: Your custom kernel (~/linux-6.12.40/arch/x86/boot/bzImage)
- **Rootfs**: Ubuntu 24.04 Noble Server cloud image
- **RAM**: 4GB
- **CPUs**: 2 cores
- **Disk**: Base image + 10GB additional space
- **Network**: Bridged via virbr0
- **Acceleration**: KVM with host CPU passthrough

### Two-Stage Boot Process

1. **Install Boot** (`--install`): Uses Ubuntu kernel with cloud-init for system setup
2. **Development Boot** (default): Uses your custom kernel for development/debugging

### Credentials

After the install boot completes, the Ubuntu login is:

- **Username**: `ubuntu`
- **Password**: `ubuntu`

## Cloud-Init Configuration

The script uses your existing cloud-init files in the `cloud-init/` directory:

- `user-data` - User accounts, packages, system configuration
- `meta-data` - Instance ID and hostname
- `network-config` - Network interface configuration

### Customizing Cloud-Init

Edit the files directly in the `cloud-init/` directory:
- Modify `user-data` for user accounts, packages, and system setup
- Update `meta-data` for instance information
- Configure `network-config` for network settings

## Kernel Development & Debugging

### GDB Kernel Debugging

1. Launch VM in debug mode:
   ```bash
   uv run vm.py -d -S
   ```

2. Connect GDB in another terminal:
   ```bash
   gdb ~/linux-6.12.40/vmlinux
   (gdb) target remote localhost:1234
   (gdb) continue
   ```

### Kernel Logging

Enable kernel debug logging (e.g., for BPF debugging):
```bash
uv run vm.py -l
```

This adds `dyndbg="file kernel/bpf/bpf_diff.c +p"` to kernel parameters.

### Daemon Mode

Launch VM in background:
```bash
uv run vm.py -D
```

Monitor logs:
```bash
tail -f serial.log
```

Connect to QEMU monitor:
```bash
socat - UNIX-CONNECT:/tmp/qemu-monitor.sock
```

## File Structure

```
vm/
├── vm.py                # Main launcher script
├── pyproject.toml       # Python dependencies (uv)
├── README.md           # This file
├── cloud-init/         # Your cloud-init configuration
│   ├── user-data       # User configuration
│   ├── meta-data       # Instance metadata
│   ├── network-config  # Network configuration
│   └── seed.img        # Generated ISO
├── ubuntu.img          # Symlink to Ubuntu image
└── noble-server-cloudimg-amd64.img  # Downloaded Ubuntu image
```

## Migrated Functionality

This script replaces and integrates the following bash scripts:

- **download.sh** → `--prepare` flag with Ubuntu cloud image download
- **cloud-init/gen.sh** → `--iso` flag to generate seed ISO from existing files
- **init.sh** → Two-stage boot process (install + development modes)
- **Enhanced kernel debugging** → Added GDB and logging support
- **Improved workflow** → Separation of system setup and kernel development

## Requirements

### System Packages
- `qemu-system-x86_64` - QEMU virtualization
- `genisoimage` - ISO creation for cloud-init
- `bridge-utils` - Network bridging (if using bridged networking)
- **Custom kernel** - Built at `~/linux-6.12.40/arch/x86/boot/bzImage`

### Python Packages
- `requests` - HTTP downloads
- `tqdm` - Progress bars

Install with: `uv sync`

### Network Setup
The script assumes the `virbr0` bridge exists and QEMU bridge helper allows it.

Configure QEMU bridge helper:
```bash
sudo mkdir -p /etc/qemu
echo "allow virbr0" | sudo tee /etc/qemu/bridge.conf
sudo chmod 0644 /etc/qemu/bridge.conf
```

Create the `virbr0` bridge manually:
```bash
sudo ip link add name virbr0 type bridge
sudo ip addr add 192.168.122.1/24 dev virbr0
sudo ip link set virbr0 up
```

If using libvirt:
```bash
sudo systemctl start libvirtd
sudo virsh net-start default
```

## Troubleshooting

### Common Issues

1. **"qemu-system-x86_64 not found"**
   - Install QEMU: `sudo apt install qemu-system-x86_64`

2. **"genisoimage not found"**
   - Install genisoimage: `sudo apt install genisoimage`

3. **"Custom kernel not found"** (development mode only)
   - Build kernel: `cd ~/linux-6.12.40 && make -j$(nproc)`
   - Or update kernel path in script
   - Note: Install mode doesn't need custom kernel

4. **"Missing cloud-init files"** (install mode only)
   - Create required files in `cloud-init/` directory: `user-data`, `meta-data`, `network-config`
   - Note: Development mode doesn't use cloud-init

5. **Network connectivity issues**
   - Ensure virbr0 bridge exists and libvirtd is running
   - Alternative: Change networking to user mode in QEMU flags

5. **Permission denied for /dev/kvm**
   - Add user to kvm group: `sudo usermod -a -G kvm $USER`
   - Logout and login again

### Debug Information

For additional debugging, check:
- QEMU process: `ps aux | grep qemu`
- Network interfaces: `ip link show`
- Bridge status: `brctl show virbr0`
- KVM availability: `kvm-ok` or `/dev/kvm` permissions

## License

This script is provided as-is for development and testing purposes.
