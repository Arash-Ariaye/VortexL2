"""
VortexL2 L2TPv3 Tunnel Management

Handles L2TPv3 tunnel and session creation/deletion using iproute2.
"""

import logging
import subprocess
import re
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass

# BUG FIX #1: `from asyncio.log import logger` was wrong — asyncio.log.logger is the
# internal asyncio logger, NOT a module-level logger for this file.
# This caused all tunnel log output to go to the wrong logger context.
logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result of a shell command execution."""
    success: bool
    stdout: str
    stderr: str
    returncode: int


def run_command(cmd: str, check: bool = False, timeout: int = 30) -> CommandResult:
    """Execute a shell command and return result."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return CommandResult(
            success=(result.returncode == 0),
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            returncode=result.returncode
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            success=False,
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            returncode=-1
        )
    except Exception as e:
        return CommandResult(
            success=False,
            stdout="",
            stderr=str(e),
            returncode=-1
        )


class TunnelManager:
    """Manages L2TPv3 tunnel and session operations for a specific tunnel config."""

    def __init__(self, config):
        self.config = config

    @property
    def interface_name(self) -> str:
        return self.config.interface_name

    def install_prerequisites(self) -> Tuple[bool, str]:
        """Install required packages and load kernel modules."""
        steps = []

        result = run_command("uname -r")
        if not result.success:
            return False, "Failed to get kernel version"
        kernel_version = result.stdout.strip()

        steps.append(f"Installing linux-modules-extra-{kernel_version}...")
        result = run_command(
            f"apt-get install -y linux-modules-extra-{kernel_version}",
            timeout=120
        )
        if not result.success:
            steps.append(f"Warning: Could not install modules package: {result.stderr}")
        else:
            steps.append("Package installed successfully")

        result = run_command("apt-get install -y iproute2", timeout=60)
        if not result.success:
            steps.append(f"Warning: Could not install iproute2: {result.stderr}")

        modules = ["l2tp_core", "l2tp_netlink", "l2tp_eth"]
        for module in modules:
            steps.append(f"Loading module {module}...")
            result = run_command(f"modprobe {module}")
            if not result.success:
                return False, f"Failed to load module {module}: {result.stderr}"
            steps.append(f"Module {module} loaded")

        result = run_command("lsmod | grep l2tp")
        if "l2tp" not in result.stdout:
            return False, "L2TP modules not found in lsmod"

        steps.append("All prerequisites installed successfully!")
        return True, "\n".join(steps)

    def check_tunnel_exists(self, tunnel_id: int = None) -> bool:
        """Check if L2TP tunnel exists."""
        if tunnel_id is None:
            tunnel_id = self.config.tunnel_id

        result = run_command("ip l2tp show tunnel")
        if not result.success:
            return False

        pattern = rf"Tunnel\s+{tunnel_id},"
        return bool(re.search(pattern, result.stdout))

    def check_session_exists(self, tunnel_id: int = None, session_id: int = None) -> bool:
        """Check if L2TP session exists."""
        if tunnel_id is None:
            tunnel_id = self.config.tunnel_id
        if session_id is None:
            session_id = self.config.session_id

        result = run_command("ip l2tp show session")
        if not result.success:
            return False

        pattern = rf"Session\s+{session_id}\s+in\s+tunnel\s+{tunnel_id}"
        return bool(re.search(pattern, result.stdout))

    def create_tunnel(self) -> Tuple[bool, str]:
        """Create L2TP tunnel based on configuration."""
        if not self.config.local_ip or not self.config.remote_ip:
            return False, "IPs not configured. Please configure tunnel first."

        ids = self.config.get_tunnel_ids()

        if self.check_tunnel_exists():
            return True, f"Tunnel {ids['tunnel_id']} already exists (idempotent)"

        cmd_parts = [
            "ip l2tp add tunnel",
            f"tunnel_id {ids['tunnel_id']}",
            f"peer_tunnel_id {ids['peer_tunnel_id']}",
        ]

        if self.config.encap_type == "udp":
            cmd_parts.extend([
                "encap udp",
                f"local {self.config.local_ip}",
                f"remote {self.config.remote_ip}",
                f"udp_sport {self.config.udp_port}",
                f"udp_dport {self.config.udp_port}",
            ])
        else:
            cmd_parts.extend([
                "encap ip",
                f"local {self.config.local_ip}",
                f"remote {self.config.remote_ip}",
            ])

        cmd = " ".join(cmd_parts)
        result = run_command(cmd)
        if not result.success:
            return False, f"Failed to create tunnel: {result.stderr}"

        return True, f"Tunnel {ids['tunnel_id']} created successfully ({self.config.encap_type.upper()} mode)"

    def create_session(self) -> Tuple[bool, str]:
        """Create L2TP session in existing tunnel."""
        ids = self.config.get_tunnel_ids()

        if not self.check_tunnel_exists():
            return False, "Tunnel does not exist. Create tunnel first."

        if self.check_session_exists():
            return True, f"Session {ids['session_id']} already exists (idempotent)"

        cmd = (
            f"ip l2tp add session "
            f"tunnel_id {ids['tunnel_id']} "
            f"session_id {ids['session_id']} "
            f"peer_session_id {ids['peer_session_id']}"
        )

        result = run_command(cmd)
        if not result.success:
            return False, f"Failed to create session: {result.stderr}"

        return True, f"Session {ids['session_id']} created successfully"

    def setup_interface(self) -> Tuple[bool, str]:
        """Configure network interface for the tunnel."""
        iface = self.interface_name
        ip = self.config.interface_ip

        # Check interface exists
        result = run_command(f"ip link show {iface}")
        if not result.success:
            return False, f"Interface {iface} does not exist"

        steps = []

        # Set interface UP
        result = run_command(f"ip link set {iface} up")
        if not result.success:
            return False, f"Failed to bring up {iface}: {result.stderr}"
        steps.append(f"Interface {iface} brought up")

        # BUG FIX #2: Check if IP already assigned before adding (prevents EEXIST errors)
        result = run_command(f"ip addr show {iface}")
        if ip.split('/')[0] not in result.stdout:
            result = run_command(f"ip addr add {ip} dev {iface}")
            if not result.success:
                return False, f"Failed to assign IP {ip} to {iface}: {result.stderr}"
            steps.append(f"IP {ip} assigned to {iface}")
        else:
            steps.append(f"IP {ip} already assigned to {iface} (idempotent)")

        return True, "\n".join(steps)

    def delete_tunnel(self) -> Tuple[bool, str]:
        """Delete L2TP tunnel and session."""
        ids = self.config.get_tunnel_ids()
        steps = []

        # Bring interface down first
        iface = self.interface_name
        result = run_command(f"ip link show {iface}")
        if result.success:
            run_command(f"ip link set {iface} down")
            steps.append(f"Interface {iface} brought down")

        # Delete session
        if self.check_session_exists():
            result = run_command(
                f"ip l2tp del session "
                f"tunnel_id {ids['tunnel_id']} "
                f"session_id {ids['session_id']}"
            )
            if result.success:
                steps.append(f"Session {ids['session_id']} deleted")
            else:
                steps.append(f"Warning: Failed to delete session: {result.stderr}")

        # Delete tunnel
        if self.check_tunnel_exists():
            result = run_command(f"ip l2tp del tunnel tunnel_id {ids['tunnel_id']}")
            if result.success:
                steps.append(f"Tunnel {ids['tunnel_id']} deleted")
            else:
                return False, f"Failed to delete tunnel {ids['tunnel_id']}: {result.stderr}"
        else:
            steps.append(f"Tunnel {ids['tunnel_id']} does not exist (already clean)")

        return True, "\n".join(steps) if steps else "Tunnel teardown complete"

    def full_setup(self) -> Tuple[bool, str]:
        """Full tunnel setup: create tunnel, session, configure interface."""
        steps = []

        success, msg = self.create_tunnel()
        steps.append(msg)
        if not success:
            return False, "\n".join(steps)

        success, msg = self.create_session()
        steps.append(msg)
        if not success:
            return False, "\n".join(steps)

        success, msg = self.setup_interface()
        steps.append(msg)
        if not success:
            return False, "\n".join(steps)

        return True, "\n".join(steps)

    def full_teardown(self) -> Tuple[bool, str]:
        """Full tunnel teardown."""
        return self.delete_tunnel()

    def get_status(self) -> Dict:
        """Get comprehensive tunnel status."""
        ids = self.config.get_tunnel_ids()
        iface = self.interface_name

        tunnel_exists = self.check_tunnel_exists()
        session_exists = self.check_session_exists()

        # Check interface
        iface_result = run_command(f"ip link show {iface}")
        iface_up = iface_result.success and "UP" in iface_result.stdout

        # Check IP
        addr_result = run_command(f"ip addr show {iface}")
        has_ip = addr_result.success and "inet " in addr_result.stdout

        return {
            "tunnel_exists": tunnel_exists,
            "session_exists": session_exists,
            "interface_up": iface_up,
            "has_ip": has_ip,
            "healthy": tunnel_exists and session_exists and iface_up and has_ip,
        }
