#!/usr/bin/env python3
"""
VortexL2 Forward Daemon

Manages HAProxy/Socat-based port forwarding based on global config.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vortexl2.config import ConfigManager, GlobalConfig
from vortexl2.forward import get_forward_manager, get_forward_mode

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/vortexl2/forward-daemon.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class ForwardDaemon:
    """Manages HAProxy/Socat-based port forwarding."""

    def __init__(self):
        self.config_manager = ConfigManager()
        self.forward_manager = None
        self.running = False
        # BUG FIX #3: Use asyncio.Event for clean shutdown instead of busy-loop on bool
        self._stop_event = asyncio.Event()

    async def start(self):
        """Start the forward daemon."""
        logger.info("Starting VortexL2 Forward Daemon")

        mode = get_forward_mode()
        logger.info(f"Forward mode: {mode}")

        if mode == "none":
            logger.info("Port forwarding is DISABLED. Use 'sudo vortexl2' to enable a mode.")
            subprocess.run("systemctl stop haproxy", shell=True, capture_output=True)
            subprocess.run("pkill -f 'socat.*TCP-LISTEN'", shell=True, capture_output=True)
            self.running = True
            # BUG FIX #3: Wait on event instead of while loop + sleep(1)
            await self._stop_event.wait()
            return

        if mode == "haproxy":
            logger.info("Starting HAProxy-based port forwarding")
            subprocess.run("pkill -f 'socat.*TCP-LISTEN'", shell=True, capture_output=True)
            result = subprocess.run(
                "systemctl start haproxy",
                shell=True, capture_output=True, text=True
            )
            if result.returncode != 0:
                logger.warning(f"Could not start HAProxy: {result.stderr.strip()}")
        elif mode == "socat":
            logger.info("Starting Socat-based port forwarding")
            logger.info("Stopping HAProxy to free ports for Socat...")
            subprocess.run("systemctl stop haproxy", shell=True, capture_output=True)

        self.running = True
        self.forward_manager = get_forward_manager(None)

        if not self.forward_manager:
            logger.error("Failed to get forward manager")
            return

        logger.info(f"Starting {mode} forwards for all configured tunnels")
        success, msg = await self.forward_manager.start_all_forwards()
        if not success:
            logger.error(f"Failed to start port forwards: {msg}")
        else:
            logger.info(msg)

        logger.info("Forward Daemon started successfully")

        # BUG FIX #3: Wait on event instead of while self.running loop
        await self._stop_event.wait()

    async def stop(self):
        """Stop the forward daemon."""
        logger.info("Stopping VortexL2 Forward Daemon")
        self.running = False

        if self.forward_manager:
            logger.info("Stopping active forwards")
            await self.forward_manager.stop_all_forwards()

        # Signal the event so start() unblocks
        self._stop_event.set()
        logger.info("Forward Daemon stopped")


async def main():
    """Main entry point."""
    daemon = ForwardDaemon()

    loop = asyncio.get_running_loop()

    # BUG FIX #4: asyncio.create_task() called from a sync signal handler crashes.
    # Use loop.create_task() from the running loop via add_signal_handler().
    def handle_signal():
        logger.info("Received shutdown signal")
        loop.create_task(daemon.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await daemon.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        await daemon.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
