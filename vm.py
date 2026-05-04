#!/usr/bin/env -S uv run

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
import requests
from tqdm import tqdm
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import json
import socket


class VMError(Exception):
    """Base exception for VM operations"""

    pass


class VMConfigError(VMError):
    """Configuration validation errors"""

    pass


class VMExecutionError(VMError):
    """VM execution errors"""

    pass


@dataclass(frozen=True)
class KernelConfig:
    """Kernel development configuration"""

    kernel_path: Path = Path.home() / "linux-vbpf/arch/x86/boot/bzImage"
    bpf_debug_string: str = 'dyndbg="file kernel/bpf/bpf_diff.c +p"'
    version: Optional[str] = None

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "KernelConfig":
        """Create KernelConfig from configuration dict"""
        kernel_config = config.get("kernel", {})
        defaults = cls()
        return cls(
            kernel_path=Path(kernel_config.get("path", defaults.kernel_path)),
            bpf_debug_string=kernel_config.get(
                "bpf_debug_string", defaults.bpf_debug_string
            ),
            version=kernel_config.get("version"),
        )

    def extract_kernel_version(self) -> str:
        """Extract kernel version from bzImage"""
        try:
            result = subprocess.run(
                ["strings", str(self.kernel_path)],
                capture_output=True,
                text=True,
                check=True,
            )

            # Look for version pattern like "6.12.40-ga565d4974162-dirty"
            for line in result.stdout.splitlines():
                if re.match(r"^\d+\.\d+\.\d+", line) and " (" in line:
                    # Extract just the version part before the space
                    version = line.split(" ")[0]
                    return version

            raise VMConfigError(f"Could not find kernel version in {self.kernel_path}")

        except subprocess.CalledProcessError:
            raise VMConfigError(f"Failed to extract version from {self.kernel_path}")
        except FileNotFoundError:
            raise VMConfigError("strings command not found. Install binutils package.")


@dataclass(frozen=True)
class VMDefaults:
    """Default VM configuration constants"""

    ubuntu_image_url: str = (
        "https://cloud-images.ubuntu.com/noble/20250725/noble-server-cloudimg-amd64.img"
    )
    ubuntu_image_filename: str = "noble-server-cloudimg-amd64.img"
    ubuntu_image_symlink: str = "ubuntu.img"
    memory: str = "4G"
    cpus: str = "2"

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "VMDefaults":
        """Create VMDefaults from configuration dict"""
        vm_config = config.get("vm", {})
        defaults = cls()
        return cls(
            ubuntu_image_url=vm_config.get(
                "ubuntu_image_url", defaults.ubuntu_image_url
            ),
            ubuntu_image_filename=vm_config.get(
                "ubuntu_image_filename", defaults.ubuntu_image_filename
            ),
            ubuntu_image_symlink=vm_config.get(
                "ubuntu_image_symlink", defaults.ubuntu_image_symlink
            ),
            memory=vm_config.get("memory", defaults.memory),
            cpus=vm_config.get("cpus", defaults.cpus),
        )


def cpu_type(value):
    """Validate CPU count during argument parsing"""
    try:
        cpu_count = int(value)
        if cpu_count < 1 or cpu_count > 128:
            raise argparse.ArgumentTypeError(
                f"CPU count must be between 1 and 128, got: {value}"
            )
        return value
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"CPU count must be a valid integer, got: {value}"
        )


def memory_type(value):
    """Validate memory format during argument parsing"""
    # Validate memory format (should end with G, M, or be a number)
    match = re.match(r"^\d+[GMK]?$", value.upper())
    if not match:
        raise argparse.ArgumentTypeError(
            f"Memory size must be in format like '4G', '2048M', or '1024', got: {value}"
        )

    # Extract numeric part and check reasonable limits
    numeric_match = re.match(r"^(\d+)", value)
    if not numeric_match:
        raise argparse.ArgumentTypeError(
            f"Unable to parse numeric part of memory size: {value}"
        )

    numeric_part = numeric_match.group(1)
    size_mb = int(numeric_part)

    if value.upper().endswith("G"):
        size_mb *= 1024
    elif value.upper().endswith("K"):
        size_mb //= 1024

    if size_mb < 256 or size_mb > 65536:  # 256MB to 64GB
        raise argparse.ArgumentTypeError(
            f"Memory size should be between 256M and 64G, got: {value}"
        )

    return value


class VMConfigFile:
    """Handles persistent VM configuration storage"""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or Path("config.json")
        self._ensure_config_dir()

    def _ensure_config_dir(self):
        """Create config directory if it doesn't exist"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        """Load configuration from file"""
        if not self.config_path.exists():
            return self._default_config()

        try:
            with open(self.config_path, "r") as f:
                config = json.load(f)
                return {**self._default_config(), **config}
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Failed to load config from {self.config_path}: {e}")
            return self._default_config()

    def save(self, config: Dict[str, Any]):
        """Save configuration to file"""
        self._validate_config(config)
        try:
            with open(self.config_path, "w") as f:
                json.dump(config, f, indent=2, default=str)
        except OSError as e:
            raise VMConfigError(f"Failed to save config to {self.config_path}: {e}")

    def _validate_config(self, config: Dict[str, Any]):
        """Validate configuration before saving"""
        errors = []

        # Validate kernel section
        if "kernel" in config:
            kernel_path = config["kernel"].get("path")
            if kernel_path:
                kernel_path_obj = Path(kernel_path)
                if not kernel_path_obj.parent.exists():
                    errors.append(
                        f"Kernel directory does not exist: {kernel_path_obj.parent}"
                    )

        # Validate VM section
        if "vm" in config:
            vm_config = config["vm"]

            # Validate memory format
            memory = vm_config.get("memory")
            if memory:
                try:
                    memory_type(memory)
                except argparse.ArgumentTypeError as e:
                    errors.append(f"Invalid memory format: {e}")

            # Validate CPU count
            cpus = vm_config.get("cpus")
            if cpus:
                try:
                    cpu_type(cpus)
                except argparse.ArgumentTypeError as e:
                    errors.append(f"Invalid CPU count: {e}")

        # Validate cloud-init section
        if "cloud_init" in config:
            cloudinit_dir = config["cloud_init"].get("directory")
            if cloudinit_dir:
                cloudinit_path = Path(cloudinit_dir)
                if not cloudinit_path.exists():
                    errors.append(
                        f"Cloud-init directory does not exist: {cloudinit_path}"
                    )

        if errors:
            raise VMConfigError(
                "Configuration validation failed:\n"
                + "\n".join(f"  - {error}" for error in errors)
            )

    def _default_config(self) -> Dict[str, Any]:
        """Return default configuration values"""
        return {
            "kernel": {
                "path": str(Path.home() / "linux-vbpf/arch/x86/boot/bzImage"),
                "bpf_debug_string": 'dyndbg="file kernel/bpf/bpf_diff.c +p"',
                "version": None,
            },
            "vm": {
                "memory": "4G",
                "cpus": "2",
                "ubuntu_image_url": "https://cloud-images.ubuntu.com/noble/20250725/noble-server-cloudimg-amd64.img",
                "ubuntu_image_filename": "noble-server-cloudimg-amd64.img",
                "ubuntu_image_symlink": "ubuntu.img",
            },
            "cloud_init": {"directory": "cloud-init"},
        }

    def reset_to_defaults(self):
        """Reset configuration to defaults"""
        self.save(self._default_config())

    def get_config_display(self) -> str:
        """Get formatted config display for CLI"""
        config = self.load()
        lines = []

        lines.append("Current VM Configuration:")
        lines.append("=" * 40)

        lines.append("\nKernel Settings:")
        lines.append(f"  Path: {config['kernel']['path']}")
        lines.append(f"  BPF Debug: {config['kernel']['bpf_debug_string']}")

        lines.append("\nVM Settings:")
        lines.append(f"  Memory: {config['vm']['memory']}")
        lines.append(f"  CPUs: {config['vm']['cpus']}")
        lines.append(f"  Ubuntu Image: {config['vm']['ubuntu_image_symlink']}")

        lines.append("\nCloud-Init Settings:")
        lines.append(f"  Directory: {config['cloud_init']['directory']}")

        lines.append(f"\nConfig file: {self.config_path}")

        return "\n".join(lines)


class VMConfigTUI:
    """Textual-based TUI for VM configuration"""

    def __init__(self, config_file: VMConfigFile):
        self.config_file = config_file
        self.config = config_file.load()

    def run(self):
        """Run the TUI configuration interface"""
        try:
            from textual.app import App, ComposeResult
            from textual.containers import Container, Horizontal
            from textual.widgets import (
                Input,
                Button,
                Label,
                Header,
                Footer,
                TabbedContent,
                TabPane,
            )
            from textual.screen import ModalScreen
            from textual import on
        except ImportError:
            raise VMConfigError("Textual not available. Install with: uv add textual")

        class SaveConfirmScreen(ModalScreen):
            """Modal screen for save confirmation"""

            def compose(self) -> ComposeResult:
                yield Container(
                    Label("Save configuration changes?", id="save-dialog-label"),
                    Horizontal(
                        Button("Save", variant="success", id="save-confirm"),
                        Button("Cancel", variant="error", id="save-cancel"),
                        classes="save-dialog-buttons",
                    ),
                    id="save-dialog",
                )

            @on(Button.Pressed, "#save-confirm")
            def save_confirmed(self):
                self.dismiss(True)

            @on(Button.Pressed, "#save-cancel")
            def save_cancelled(self):
                self.dismiss(False)

        class VMConfigApp(App):
            """Main VM configuration application"""

            CSS = """
            #save-dialog {
                width: 50;
                height: 10;
                background: $surface;
                border: thick $primary;
            }

            #save-dialog-label {
                width: 100%;
                content-align: center middle;
                margin: 1;
            }

            .save-dialog-buttons {
                width: 100%;
                height: 3;
                align: center middle;
            }

            Input {
                margin: 0 1;
            }

            Label {
                margin: 1 0;
            }

            Button {
                margin: 1;
            }

            TabbedContent {
                height: 100%;
            }
            """

            def __init__(self, config: Dict[str, Any], config_file: VMConfigFile):
                super().__init__()
                self.config = config.copy()
                self.config_file = config_file
                self.original_config = config.copy()

            def compose(self) -> ComposeResult:
                yield Header()
                with TabbedContent(id="tabs"):
                    with TabPane("Kernel"):
                        yield from self._compose_kernel_tab()
                    with TabPane("VM Settings"):
                        yield from self._compose_vm_tab()
                    with TabPane("Cloud-Init"):
                        yield from self._compose_cloudinit_tab()
                yield Footer()

            def _compose_kernel_tab(self) -> ComposeResult:
                yield Container(
                    Label("Kernel Configuration"),
                    Label("Kernel Path:"),
                    Input(value=self.config["kernel"]["path"], id="kernel-path"),
                    Label("BPF Debug String:"),
                    Input(
                        value=self.config["kernel"]["bpf_debug_string"], id="bpf-debug"
                    ),
                    Button("Save Changes", variant="success", id="save-kernel"),
                )

            def _compose_vm_tab(self) -> ComposeResult:
                yield Container(
                    Label("VM Configuration"),
                    Label("Memory (e.g., 4G, 2048M):"),
                    Input(value=self.config["vm"]["memory"], id="vm-memory"),
                    Label("CPU Cores:"),
                    Input(value=self.config["vm"]["cpus"], id="vm-cpus"),
                    Label("Ubuntu Image URL:"),
                    Input(value=self.config["vm"]["ubuntu_image_url"], id="ubuntu-url"),
                    Label("Ubuntu Image Filename:"),
                    Input(
                        value=self.config["vm"]["ubuntu_image_filename"],
                        id="ubuntu-filename",
                    ),
                    Label("Ubuntu Image Symlink:"),
                    Input(
                        value=self.config["vm"]["ubuntu_image_symlink"],
                        id="ubuntu-symlink",
                    ),
                    Button("Save Changes", variant="success", id="save-vm"),
                )

            def _compose_cloudinit_tab(self) -> ComposeResult:
                yield Container(
                    Label("Cloud-Init Configuration"),
                    Label("Cloud-Init Directory:"),
                    Input(
                        value=self.config["cloud_init"]["directory"], id="cloudinit-dir"
                    ),
                    Button("Save Changes", variant="success", id="save-cloudinit"),
                )

            @on(Button.Pressed, "#save-kernel")
            def save_kernel_config(self):
                kernel_path = self.query_one("#kernel-path", Input).value
                bpf_debug = self.query_one("#bpf-debug", Input).value

                self.config["kernel"]["path"] = kernel_path
                self.config["kernel"]["bpf_debug_string"] = bpf_debug
                self._show_save_confirmation()

            @on(Button.Pressed, "#save-vm")
            def save_vm_config(self):
                memory = self.query_one("#vm-memory", Input).value
                cpus = self.query_one("#vm-cpus", Input).value
                ubuntu_url = self.query_one("#ubuntu-url", Input).value
                ubuntu_filename = self.query_one("#ubuntu-filename", Input).value
                ubuntu_symlink = self.query_one("#ubuntu-symlink", Input).value

                # Validate memory and CPU formats
                try:
                    memory_type(memory)
                    cpu_type(cpus)
                except argparse.ArgumentTypeError as e:
                    self.notify(f"Validation error: {e}", severity="error")
                    return

                self.config["vm"]["memory"] = memory
                self.config["vm"]["cpus"] = cpus
                self.config["vm"]["ubuntu_image_url"] = ubuntu_url
                self.config["vm"]["ubuntu_image_filename"] = ubuntu_filename
                self.config["vm"]["ubuntu_image_symlink"] = ubuntu_symlink
                self._show_save_confirmation()

            @on(Button.Pressed, "#save-cloudinit")
            def save_cloudinit_config(self):
                cloudinit_dir = self.query_one("#cloudinit-dir", Input).value
                self.config["cloud_init"]["directory"] = cloudinit_dir
                self._show_save_confirmation()

            def _show_save_confirmation(self):
                def handle_save_result(should_save: bool):
                    if should_save:
                        try:
                            self.config_file.save(self.config)
                            self.notify(
                                "Configuration saved successfully!",
                                severity="information",
                            )
                        except VMConfigError as e:
                            self.notify(f"Save failed: {e}", severity="error")
                    else:
                        self.notify("Save cancelled", severity="warning")

                self.push_screen(SaveConfirmScreen(), handle_save_result)

            def action_quit(self):
                if self.config != self.original_config:

                    def handle_quit_save(should_save: bool):
                        if should_save:
                            try:
                                self.config_file.save(self.config)
                            except VMConfigError as e:
                                self.notify(f"Save failed: {e}", severity="error")
                                return
                        self.exit()

                    self.push_screen(SaveConfirmScreen(), handle_quit_save)
                else:
                    self.exit()

        app = VMConfigApp(self.config, self.config_file)
        app.run()


@dataclass(frozen=True)
class CloudInitConfig:
    """Cloud-init configuration"""

    cloud_init_dir: Path = Path("cloud-init")

    @property
    def seed_img(self) -> Path:
        return self.cloud_init_dir / "seed.img"

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "CloudInitConfig":
        """Create CloudInitConfig from configuration dict"""
        cloud_init_config = config.get("cloud_init", {})
        defaults = cls()
        return cls(
            cloud_init_dir=Path(
                cloud_init_config.get("directory", defaults.cloud_init_dir)
            )
        )


@dataclass(frozen=True)
class VMConfig:
    """Immutable VM configuration that composes cleanly"""

    memory: str = VMDefaults.memory
    cpus: str = VMDefaults.cpus
    kernel: Optional[Path] = None
    ubuntu_img: str = VMDefaults.ubuntu_image_symlink
    cloud_init: Optional[CloudInitConfig] = None
    debug: bool = False
    stop: bool = False
    debug_log: bool = False
    daemon: bool = False
    kernel_config: KernelConfig = KernelConfig()

    def to_qemu_flags(self) -> List[str]:
        """Generate QEMU flags from configuration"""
        flags = [
            "-machine",
            "accel=kvm",
            "-cpu",
            "host",
            "-m",
            self.memory,
            "-smp",
            self.cpus,
            "-drive",
            f"file={self.ubuntu_img},index=0,format=qcow2,media=disk",
            "-netdev",
            "bridge,id=net0,br=virbr0",
            "-device",
            "virtio-net-pci,netdev=net0",
            "-display",
            "none",
        ]

        # Boot configuration
        if self.kernel:
            flags.extend(["-kernel", str(self.kernel)])
            # Kernel boot parameters
            boot_params = "root=/dev/sda1 ro console=tty1 console=ttyS0"
            if self.debug_log:
                boot_params += f" {self.kernel_config.bpf_debug_string}"
            if self.debug:
                boot_params += " nokaslr"
            flags.extend(["-append", boot_params])
        elif self.cloud_init:
            flags.extend(
                ["-drive", f"file={self.cloud_init.seed_img},index=1,media=cdrom"]
            )

        # Debug flags
        if self.debug:
            flags.append("-s")
            if self.stop:
                flags.append("-S")

        # Display mode
        if self.daemon:
            flags.extend(
                [
                    "-pidfile",
                    "qemu.pid",
                    "-serial",
                    "file:serial.log",
                    "-monitor",
                    "unix:/tmp/qemu-monitor.sock,server,nowait",
                    "-daemonize",
                ]
            )
        else:
            flags.append("-nographic")

        return flags


class VMConfigBuilder:
    """Unified builder for VM configuration supporting both install and development modes"""

    def __init__(
        self,
        install_mode: bool = False,
        kernel_path: Optional[Path] = None,
        ubuntu_img: str = "ubuntu.img",
        vm_defaults: Optional[VMDefaults] = None,
    ):
        self.install_mode = install_mode
        self.kernel_path = kernel_path
        self.ubuntu_img = ubuntu_img
        defaults = vm_defaults or VMDefaults()
        self.memory = defaults.memory
        self.cpus = defaults.cpus
        self.debug = False
        self.stop = False
        self.debug_log = False
        self.daemon = False

    def with_memory(self, memory: str) -> "VMConfigBuilder":
        self.memory = memory
        return self

    def with_cpus(self, cpus: str) -> "VMConfigBuilder":
        self.cpus = cpus
        return self

    def with_debug(self, debug: bool = True) -> "VMConfigBuilder":
        self.debug = debug
        return self

    def with_stop(self, stop: bool = True) -> "VMConfigBuilder":
        self.stop = stop
        return self

    def with_debug_log(self, debug_log: bool = True) -> "VMConfigBuilder":
        self.debug_log = debug_log
        return self

    def with_daemon(self, daemon: bool = True) -> "VMConfigBuilder":
        self.daemon = daemon
        return self

    def build(self) -> VMConfig:
        return VMConfig(
            memory=self.memory,
            cpus=self.cpus,
            kernel=None if self.install_mode else self.kernel_path,
            ubuntu_img=self.ubuntu_img,
            cloud_init=CloudInitConfig() if self.install_mode else None,
            debug=self.debug,
            stop=self.stop,
            debug_log=self.debug_log,
            daemon=self.daemon,
        )


class CloudInitManager:
    """Manages cloud-init configuration and ISO generation"""

    def __init__(self, cloud_init_dir: Path = Path("cloud-init")):
        self.cloud_init_dir = cloud_init_dir

    def check_files(self) -> bool:
        """Check if required cloud-init files exist"""
        required_files = ["user-data", "meta-data", "network-config"]
        missing_files = []

        for file in required_files:
            if not (self.cloud_init_dir / file).exists():
                missing_files.append(file)

        if missing_files:
            print(f"Error: Missing cloud-init files: {', '.join(missing_files)}")
            print(f"Please create these files in {self.cloud_init_dir}/")
            print("Example content:")
            if "user-data" in missing_files:
                print("  user-data: #cloud-config with user configuration")
            if "meta-data" in missing_files:
                print("  meta-data: instance-id and hostname")
            if "network-config" in missing_files:
                print("  network-config: network configuration")
            return False

        print("✓ All required cloud-init files found")
        return True

    def generate_iso(self) -> bool:
        """Generate cloud-init seed ISO"""
        if not self.cloud_init_dir.exists():
            print(f"Error: {self.cloud_init_dir} directory doesn't exist.")
            return False

        if not self.check_files():
            return False

        try:
            seed_img = self.cloud_init_dir / "seed.img"
            cmd = [
                "genisoimage",
                "-output",
                str(seed_img),
                "-volid",
                "cidata",
                "-rational-rock",
                "-joliet",
                str(self.cloud_init_dir / "user-data"),
                str(self.cloud_init_dir / "meta-data"),
                str(self.cloud_init_dir / "network-config"),
            ]

            result = subprocess.run(
                cmd, cwd=self.cloud_init_dir.parent, capture_output=True, text=True
            )

            if result.returncode == 0:
                print("✓ Cloud-init seed ISO created successfully")
                return True
            else:
                print(f"Error creating cloud-init ISO: {result.stderr}")
                return False

        except FileNotFoundError:
            print("Error: genisoimage not found. Please install it:")
            print("Ubuntu/Debian: sudo apt install genisoimage")
            print("RHEL/CentOS: sudo yum install genisoimage")
            return False


class QEMURunner:
    """Handles QEMU command execution only"""

    def __init__(self, qemu_cmd: str = "qemu-system-x86_64"):
        self.qemu_cmd = qemu_cmd

    def cleanup(self):
        """Clean up existing QEMU instance and files"""
        qemu_pid_file = "qemu.pid"
        qemu_monitor_socket = "/tmp/qemu-monitor.sock"
        qemu_serial_log = "serial.log"

        if os.path.exists(qemu_pid_file):
            try:
                with open(qemu_pid_file, "r") as f:
                    pid = int(f.read().strip())
                os.kill(pid, 9)
            except (OSError, ValueError, ProcessLookupError):
                pass
            finally:
                os.remove(qemu_pid_file)

        for file_path in [qemu_monitor_socket, qemu_serial_log]:
            if os.path.exists(file_path):
                os.remove(file_path)

    def run(self, config: VMConfig) -> bool:
        """Run QEMU with the given configuration"""
        cmd = [self.qemu_cmd] + config.to_qemu_flags()
        print(" ".join(cmd))

        try:
            if config.daemon:
                subprocess.Popen(cmd)
            else:
                subprocess.run(cmd)
            return True
        except KeyboardInterrupt:
            print("\nVM stopped by user")
            return False
        except FileNotFoundError:
            print(f"Error: {self.qemu_cmd} not found. Please install QEMU.")
            return False
        except Exception as e:
            print(f"Error launching VM: {e}")
            return False


class QEMULauncher:
    def __init__(
        self,
        kernel_config: Optional[KernelConfig] = None,
        config_file: Optional[VMConfigFile] = None,
    ):
        self.config_file = config_file or VMConfigFile()
        self.user_config = self.config_file.load()

        self.kernel_config = kernel_config or KernelConfig.from_config(self.user_config)
        self.vm_defaults = VMDefaults.from_config(self.user_config)
        self.cloud_init_manager = CloudInitManager(
            cloud_init_dir=CloudInitConfig.from_config(self.user_config).cloud_init_dir
        )
        self.qemu_runner = QEMURunner()

    def get_kernel_version(self) -> str:
        """Get kernel version with caching"""
        config = self.config_file.load()
        kernel_config = config.get("kernel", {})
        cached_version = kernel_config.get("version")

        # Check if we need to extract/re-extract the version
        kernel_path = Path(kernel_config.get("path", self.kernel_config.kernel_path))

        if cached_version is None or not kernel_path.exists():
            # Extract version and cache it
            version = self.kernel_config.extract_kernel_version()
            kernel_config["version"] = version
            config["kernel"] = kernel_config
            self.config_file.save(config)
            return version

        return cached_version

    def install_modules(self, modules_path: str) -> bool:
        """Install kernel modules to VM via SCP"""
        try:
            modules_path = Path(modules_path).resolve()
            if not modules_path.exists():
                raise VMExecutionError(f"Modules path does not exist: {modules_path}")

            # Get kernel version for target path
            kernel_version = self.get_kernel_version()
            target_path = f"/lib/modules/{kernel_version}/"

            print(f"Installing modules from {modules_path}")
            print(f"Target kernel version: {kernel_version}")
            print(f"VM target path: {target_path}")

            # Check if VM is reachable
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", "192.168.122.10"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise VMExecutionError(
                    "VM is not reachable at 192.168.122.10. Make sure VM is running."
                )

            # Create target directory on VM
            ssh_result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "ubuntu@192.168.122.10",
                    f"sudo mkdir -p {target_path}",
                ],
                capture_output=True,
                text=True,
            )
            if ssh_result.returncode != 0:
                raise VMExecutionError(
                    f"Failed to create target directory on VM: {ssh_result.stderr}"
                )

            # Transfer modules with SCP
            print("Transferring modules...")
            scp_result = subprocess.run(
                [
                    "scp",
                    "-r",
                    "-p",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"{modules_path}/*",
                    "ubuntu@192.168.122.10:/tmp/modules_transfer/",
                ],
                capture_output=True,
                text=True,
            )
            if scp_result.returncode != 0:
                raise VMExecutionError(f"SCP transfer failed: {scp_result.stderr}")

            # Move modules to final location with proper permissions
            ssh_result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "ubuntu@192.168.122.10",
                    f"sudo mkdir -p /tmp/modules_transfer && sudo cp -r /tmp/modules_transfer/* {target_path} && sudo rm -rf /tmp/modules_transfer",
                ],
                capture_output=True,
                text=True,
            )
            if ssh_result.returncode != 0:
                raise VMExecutionError(
                    f"Failed to install modules on VM: {ssh_result.stderr}"
                )

            print(f"✓ Modules successfully installed to {target_path}")
            return True

        except subprocess.CalledProcessError as e:
            raise VMExecutionError(f"Command failed: {e}")
        except Exception as e:
            raise VMExecutionError(f"Module installation failed: {e}")

    def build_config(self, args) -> VMConfig:
        """Build VM configuration using unified builder"""
        builder = VMConfigBuilder(
            install_mode=args.install,
            kernel_path=self.kernel_config.kernel_path,
            ubuntu_img=self.vm_defaults.ubuntu_image_symlink,
            vm_defaults=self.vm_defaults,
        )

        if args.mem:
            builder = builder.with_memory(args.mem)
        if args.cpus:
            builder = builder.with_cpus(args.cpus)
        if args.debug:
            builder = builder.with_debug()
        if args.stop:
            builder = builder.with_stop()
        if args.log:
            builder = builder.with_debug_log()
        if args.daemon:
            builder = builder.with_daemon()

        return builder.build()

    def _validate_vm_files(self, config: VMConfig):
        """Validate required VM files exist"""
        if not os.path.exists(config.ubuntu_img):
            raise VMConfigError(f"{config.ubuntu_img} not found. Run --prepare first.")

        if config.cloud_init:
            if not config.cloud_init.seed_img.exists():
                print("Cloud-init seed image not found. Generating...")
                if not self.cloud_init_manager.generate_iso():
                    raise VMExecutionError("Failed to generate cloud-init ISO")
        else:
            if not config.kernel or not config.kernel.exists():
                raise VMConfigError(
                    f"Custom kernel not found at {config.kernel}. "
                    f"Build your kernel first: cd ~/linux-vbpf && make -j$(nproc)"
                )

    def _print_vm_info(self, config: VMConfig):
        """Print VM startup information"""
        if config.cloud_init:
            print("Starting VM with Ubuntu kernel for cloud-init installation...")
            print(f"Rootfs: {config.ubuntu_img}")
            print("This is first boot - cloud-init will configure the system.")
        else:
            print("Starting VM with custom kernel for development...")
            print(f"Kernel: {config.kernel}")
            print(f"Rootfs: {config.ubuntu_img}")

    def _print_daemon_info(self):
        """Print daemon process information"""
        try:
            with open("qemu.pid", "r") as f:
                pid = f.read().strip()
            print(f"VM started in background. PID: {pid}")
            print("Serial output is logged to: serial.log")
            print("Monitor socket: /tmp/qemu-monitor.sock")
            print("Default login: ubuntu/ubuntu")
        except FileNotFoundError:
            print("Warning: Could not read PID file")

    def launch_vm(self, config: VMConfig):
        """Launch the QEMU VM with given configuration"""
        self._validate_vm_files(config)
        self._print_vm_info(config)

        self.qemu_runner.cleanup()
        success = self.qemu_runner.run(config)

        if not success:
            raise VMExecutionError("VM failed to start")

        if config.daemon:
            self._print_daemon_info()

    def dump_command(self, config: VMConfig):
        """Show the QEMU command that would be executed without launching VM"""
        # Check if required files exist for validation (but don't exit)
        missing_files = []

        if not os.path.exists(config.ubuntu_img):
            missing_files.append(f"Ubuntu image: {config.ubuntu_img}")

        if config.cloud_init:
            # Install mode: check cloud-init seed
            if not config.cloud_init.seed_img.exists():
                missing_files.append(f"Cloud-init seed: {config.cloud_init.seed_img}")
        else:
            # Development mode: check custom kernel
            if not config.kernel or not config.kernel.exists():
                missing_files.append(f"Custom kernel: {config.kernel}")

        if missing_files:
            print("Warning: Missing files that would prevent VM launch:")
            for missing in missing_files:
                print(f"  - {missing}")
            print()

        # Show configuration info
        if config.cloud_init:
            print("Configuration: First boot with Ubuntu kernel and cloud-init")
            print(f"Rootfs: {config.ubuntu_img}")
            if config.cloud_init.seed_img.exists():
                print(f"Cloud-init seed: {config.cloud_init.seed_img}")
        else:
            print("Configuration: Development boot with custom kernel")
            print(f"Kernel: {config.kernel}")
            print(f"Rootfs: {config.ubuntu_img}")

        print(f"VM specs: {config.memory} memory, {config.cpus} CPU cores")

        print("\nQEMU command that would be executed:")
        cmd = ["qemu-system-x86_64"] + config.to_qemu_flags()
        print(" ".join(cmd))

    def show_help(self):
        """Show help message"""
        print(f"Usage: {sys.argv[0]} [options]")
        print("")
        print("Ubuntu Cloud-Init VM Launcher for Kernel Development")
        print("====================================================")
        print("")
        print("Setup Options:")
        print("  --prepare     Prepare environment (download image, create cloud-init)")
        print("  --iso         Generate cloud-init seed ISO from existing files")
        print("  --install     First boot with Ubuntu kernel and cloud-init setup")
        print("")
        print("Launch Options:")
        print("  -d, --debug   Enable debug mode (adds QEMU -s option)")
        print("  -D, --daemon  Run in daemon mode")
        print("  -S, --stop    Stop at first instruction (adds QEMU -S option)")
        print("  -l, --log     Enable kernel debug logging")
        print(f"  --cpus N      Number of CPU cores (default: {self.vm_defaults.cpus})")
        print(
            f"  --mem SIZE    Memory size, e.g. 2G, 4096M (default: {self.vm_defaults.memory})"
        )
        print("")
        print("Configuration:")
        print("  config        Launch interactive TUI configuration")
        print("  config --show Show current configuration")
        print("  config --reset Reset configuration to defaults")
        print("")
        print("Other:")
        print("  -h, --help    Show this help message")
        print("  --dump        Show QEMU command without launching VM")
        print("  --stop-vm     Stop running VM via QEMU monitor")
        print("")
        print("Requirements:")
        print("  - QEMU (qemu-system-x86_64)")
        print("  - genisoimage (for cloud-init ISO creation)")
        print("  - Python packages: uv sync")
        print("  - Custom kernel: ~/linux-vbpf/arch/x86/boot/bzImage")
        print("  - cloud-init files: user-data, meta-data, network-config")
        print("")
        print("Quick Start:")
        print("  1. uv run vm.py --prepare    # Download Ubuntu rootfs")
        print("  2. Create cloud-init files in cloud-init/ directory")
        print(
            "  3. uv run vm.py --install    # First boot (Ubuntu kernel + cloud-init)"
        )
        print("  4. Build your custom kernel")
        print("  5. uv run vm.py              # Development (your kernel)")
        print("")
        print("Two-Stage Boot Process:")
        print("  --install: First boot with Ubuntu kernel for cloud-init setup")
        print("  (default): Development boot with your custom kernel")
        print("  Use -d -S for GDB debugging, -l for kernel logging")
        print("")
        print(
            "VM specs: 4GB RAM, 2 CPUs, KVM acceleration (configurable with --mem/--cpus)"
        )

    def parse_args(self):
        """Parse command line arguments"""
        parser = argparse.ArgumentParser(
            description="Launch QEMU VM with custom kernel and Ubuntu cloud-init",
            add_help=False,  # We'll handle help ourselves
        )

        # Flags
        parser.add_argument(
            "-h", "--help", action="store_true", help="Show this help message"
        )
        parser.add_argument(
            "-d",
            "--debug",
            action="store_true",
            help="Enable debug mode (adds QEMU -s option)",
        )
        parser.add_argument(
            "-D", "--daemon", action="store_true", help="Run in daemon mode"
        )
        parser.add_argument(
            "-S",
            "--stop",
            action="store_true",
            help="Stop at first instruction (adds QEMU -S option)",
        )
        parser.add_argument(
            "-l", "--log", action="store_true", help="Enable kernel debug logging"
        )

        # Actions
        parser.add_argument(
            "--prepare",
            action="store_true",
            help="Prepare environment (download image, create cloud-init)",
        )
        parser.add_argument(
            "--iso",
            action="store_true",
            help="Generate cloud-init seed ISO from existing files",
        )
        parser.add_argument(
            "--install",
            action="store_true",
            help="First boot with Ubuntu kernel and cloud-init setup",
        )
        parser.add_argument(
            "--dump", action="store_true", help="Show QEMU command without launching VM"
        )
        parser.add_argument(
            "--stop-vm", action="store_true", help="Stop running VM via QEMU monitor"
        )
        parser.add_argument(
            "--modules-install",
            metavar="PATH",
            help="Install kernel modules from PATH to VM via SCP",
        )

        # Configuration management
        parser.add_argument(
            "config_command",
            nargs="?",
            choices=["config"],
            help="Configuration management",
        )
        parser.add_argument(
            "--show",
            action="store_true",
            help="Show current configuration (use with 'config')",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Reset configuration to defaults (use with 'config')",
        )

        # Parameters with validation
        parser.add_argument(
            "--cpus",
            type=cpu_type,
            default=None,
            metavar="N",
            help=f"Number of CPU cores (default: {self.vm_defaults.cpus})",
        )
        parser.add_argument(
            "--mem",
            type=memory_type,
            default=None,
            metavar="SIZE",
            help=f"Memory size, e.g. 2G, 4096M (default: {self.vm_defaults.memory})",
        )

        return parser.parse_args()

    def _download_file_with_progress(self, url: str, filename: str):
        """Download file with progress bar"""
        response = requests.get(url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))

        with open(filename, "wb") as f:
            with tqdm(
                total=total_size, unit="B", unit_scale=True, desc=filename
            ) as pbar:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))

    def _create_ubuntu_symlink(self, filename: str, symlink_name: str):
        """Create symlink for Ubuntu image"""
        if os.path.exists(symlink_name):
            os.remove(symlink_name)
        os.symlink(filename, symlink_name)

    def _resize_ubuntu_image(self, filename: str):
        """Resize Ubuntu image with qemu-img"""
        try:
            subprocess.run(
                ["qemu-img", "resize", filename, "+10G"],
                check=True,
                capture_output=True,
            )
            print("✓ Image resized (+10GB)")
        except subprocess.CalledProcessError:
            print("Warning: Could not resize image. qemu-img might not be available.")
        except FileNotFoundError:
            print("Warning: qemu-img not found. Image not resized.")

    def download_ubuntu_image(self):
        """Download Ubuntu cloud image"""
        print("Downloading Ubuntu cloud image...")

        try:
            self._download_file_with_progress(
                self.vm_defaults.ubuntu_image_url,
                self.vm_defaults.ubuntu_image_filename,
            )

            self._create_ubuntu_symlink(
                self.vm_defaults.ubuntu_image_filename,
                self.vm_defaults.ubuntu_image_symlink,
            )

            print(f"✓ Download completed: {self.vm_defaults.ubuntu_image_filename}")
            print(f"✓ Symlinked as: {self.vm_defaults.ubuntu_image_symlink}")
            file_size = Path(self.vm_defaults.ubuntu_image_filename).stat().st_size / (
                1024**3
            )
            print(f"File size: {file_size:.2f} GB")

            self._resize_ubuntu_image(self.vm_defaults.ubuntu_image_filename)

        except requests.exceptions.RequestException as e:
            raise VMExecutionError(f"Download failed: {e}")
        except KeyboardInterrupt:
            raise VMExecutionError("Download cancelled")

    def stop_vm(self):
        """Stop running VM via QEMU monitor interface"""
        monitor_socket = "/tmp/qemu-monitor.sock"
        pid_file = "qemu.pid"

        # Check if VM is running by checking PID file
        if not os.path.exists(pid_file):
            print("No running VM found (no PID file)")
            return 0

        # Check if monitor socket exists
        if not os.path.exists(monitor_socket):
            print("Monitor socket not found - VM may not be running in daemon mode")
            return 1

        try:
            # Connect to QEMU monitor socket
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(monitor_socket)

            # Read the initial QEMU monitor banner
            banner = sock.recv(1024).decode()

            # Send system_powerdown command
            sock.send(b"system_powerdown\n")

            # Read response
            response = sock.recv(1024).decode()

            sock.close()

            print("✓ Powerdown command sent to VM")
            print("VM should shutdown gracefully...")

            # Clean up files after sending powerdown
            import time

            time.sleep(2)  # Give VM time to start shutdown

            # Remove PID file and other daemon files if they still exist
            for cleanup_file in [pid_file, "serial.log"]:
                if os.path.exists(cleanup_file):
                    try:
                        os.remove(cleanup_file)
                    except OSError:
                        pass

            if os.path.exists(monitor_socket):
                try:
                    os.remove(monitor_socket)
                except OSError:
                    pass

            return 0

        except (socket.error, ConnectionRefusedError, FileNotFoundError) as e:
            print(f"Error connecting to QEMU monitor: {e}")
            print("VM may have already stopped or not running in daemon mode")
            return 1
        except Exception as e:
            print(f"Error stopping VM: {e}")
            return 1

    def run(self):
        """Main execution function"""
        try:
            args = self.parse_args()

            if args.help:
                self.show_help()
                return 0

            if args.iso:
                print("Generating cloud-init seed ISO...")
                if not self.cloud_init_manager.generate_iso():
                    raise VMExecutionError("Failed to generate cloud-init ISO")
                return 0

            if args.prepare:
                print("Preparing Ubuntu rootfs for kernel development...")
                self.download_ubuntu_image()
                print("\n✓ Ubuntu rootfs image downloaded successfully!")
                print("Next steps:")
                print("1. Ensure cloud-init files exist in cloud-init/ directory")
                print("2. Run: uv run vm.py --install (first boot with cloud-init)")
                print("3. Build your custom kernel")
                print("4. Run: uv run vm.py (development with your kernel)")
                return 0

            if getattr(args, "stop_vm", False):
                return self.stop_vm()

            if getattr(args, "modules_install", None):
                print(f"Installing kernel modules from: {args.modules_install}")
                if self.install_modules(args.modules_install):
                    return 0
                else:
                    return 1

            # Handle config command
            if args.config_command == "config":
                if args.show:
                    print(self.config_file.get_config_display())
                    return 0
                elif args.reset:
                    self.config_file.reset_to_defaults()
                    print("✓ Configuration reset to defaults")
                    print(f"Config file: {self.config_file.config_path}")
                    return 0
                else:
                    # Launch TUI
                    try:
                        tui = VMConfigTUI(self.config_file)
                        tui.run()
                        print("✓ Configuration updated")
                        return 0
                    except VMConfigError as e:
                        print(f"Error: {e}")
                        return 1

            config = self.build_config(args)

            if args.dump:
                self.dump_command(config)
                return 0

            self.launch_vm(config)
            return 0

        except VMError as e:
            print(f"Error: {e}")
            return 1
        except KeyboardInterrupt:
            print("\nOperation cancelled by user")
            return 1


if __name__ == "__main__":
    launcher = QEMULauncher()
    sys.exit(launcher.run())
