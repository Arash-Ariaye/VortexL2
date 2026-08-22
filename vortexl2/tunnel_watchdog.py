#!/usr/bin/env python3
"""
VortexL2 Tunnel Watchdog

Monitors tunnel health and automatically recovers from failures.
Restarts failed tunnels and port forwards with backoff strategy.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from vortexl2.config import ConfigManager, TunnelConfig, GlobalConfig
from vortexl2.tunnel import TunnelManager
from vortexl2.health_monitor import HealthMonitor


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/vortexl2/watchdog.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class TunnelWatchdog:
    """Monitors and recovers tunnel health."""

    def __init__(self, check_interval: int = 30, recovery_delay: int = 5):
        self.check_interval = check_interval
        self.recovery_delay = recovery_delay
        self.config_manager = ConfigManager()
        self.health_monitor = HealthMonitor(check_interval, failure_threshold=2)
        self.running = False
        self.tunnel_managers = {}
        # BUG FIX #5: Use asyncio.Event for clean shutdown
        self._stop_event = asyncio.Event()

    async def initialize(self):
        """Initialize tunnel managers for all configured tunnels."""
        tunnels = self.config_manager.get_all_tunnels()
        for tunnel_config in tunnels:
            if tunnel_config.is_configured():
                self.tunnel_managers[tunnel_config.name] = TunnelManager(tunnel_config)
                logger.info(f"Initialized watchdog for tunnel: {tunnel_config.name}")

    async def check_health(self):
        """Check health of all tunnels and ports."""
        # BUG FIX #6: Watchdog must respect tunnel_mode — don't check L2TP health
        # when running in EasyTier mode (different interface/check logic)
        global_config = GlobalConfig()
        tunnel_mode = global_config.tunnel_mode

        tunnels = self.config_manager.get_all_tunnels()
        configured_tunnels = [t for t in tunnels if t.is_configured()]

        all_ports = []
        for tunnel in configured_tunnels:
            all_ports.extend(tunnel.forwarded_ports)

        if tunnel_mode == "l2tpv3":
            tunnel_statuses = self.health_monitor.check_all_tunnel_health(configured_tunnels)
        else:
            # EasyTier tunnels have their own systemd service — skip L2TP checks
            tunnel_statuses = {}

        port_statuses = self.health_monitor.check_all_port_health(all_ports)
        return tunnel_statuses, port_statuses

    async def recover_unhealthy_tunnel(self, tunnel_config: TunnelConfig):
        """Attempt to recover an unhealthy tunnel with exponential backoff."""
        tunnel_name = tunnel_config.name
        logger.warning(f"Attempting to recover tunnel: {tunnel_name}")

        if tunnel_name not in self.tunnel_managers:
            self.tunnel_managers[tunnel_name] = TunnelManager(tunnel_config)

        tunnel_mgr = self.tunnel_managers[tunnel_name]

        # BUG FIX #7: Use backoff multiplier from failure count to avoid recovery storms
        failure_count = self.health_monitor.tunnel_health.get(
            tunnel_name, None
        )
        backoff = min(self.recovery_delay * (2 ** (getattr(failure_count, 'failure_count', 1) - 1)), 60)

        try:
            success, msg = tunnel_mgr.delete_tunnel()
            if not success and "does not exist" not in msg:
                logger.warning(f"Failed to delete tunnel: {msg}")

            await asyncio.sleep(backoff)

            success, msg = tunnel_mgr.full_setup()
            if success:
                logger.info(f"Successfully recovered tunnel: {tunnel_name}")
                return True
            else:
                logger.error(f"Failed to recover tunnel {tunnel_name}: {msg}")
                return False
        except Exception as e:
            logger.error(f"Exception during tunnel recovery: {e}", exc_info=True)
            return False

    async def recover_unhealthy_ports(self, tunnel_config: TunnelConfig):
        """Attempt to restart unhealthy ports for a tunnel."""
        tunnel_name = tunnel_config.name
        unhealthy_ports = [
            port for port in tunnel_config.forwarded_ports
            if port in self.health_monitor.port_health and
               not self.health_monitor.port_health[port].healthy
        ]

        if not unhealthy_ports:
            return True

        logger.warning(f"Attempting to restart {len(unhealthy_ports)} unhealthy ports in tunnel {tunnel_name}")

        try:
            from vortexl2.forward import get_forward_manager
            forward_manager = get_forward_manager(tunnel_config)

            if not forward_manager:
                logger.error(f"Could not get forward manager for tunnel {tunnel_name}")
                return False

            recovered = 0
            for port in unhealthy_ports:
                try:
                    success, msg = forward_manager.remove_forward(port)
                    if not success and "not in forwarded list" not in msg:
                        logger.warning(f"Failed to remove port {port}: {msg}")

                    await asyncio.sleep(1)

                    success, msg = forward_manager.create_forward(port)
                    if success:
                        logger.info(f"Successfully recovered port {port}: {msg}")
                        recovered += 1
                        self.health_monitor.clear_port_health(port)
                    else:
                        logger.error(f"Failed to recover port {port}: {msg}")
                except Exception as e:
                    logger.error(f"Exception recovering port {port}: {e}")

            return recovered > 0

        except Exception as e:
            logger.error(f"Exception during port recovery: {e}", exc_info=True)
            return False

    async def recovery_cycle(self):
        """Perform recovery for unhealthy components."""
        tunnels = self.config_manager.get_all_tunnels()
        unhealthy_tunnels, unhealthy_ports = self.health_monitor.get_recovery_needed()

        for tunnel_name in unhealthy_tunnels:
            tunnel_config = next((t for t in tunnels if t.name == tunnel_name), None)
            if tunnel_config:
                await self.recover_unhealthy_tunnel(tunnel_config)
                await asyncio.sleep(2)

        for tunnel in tunnels:
            if tunnel.is_configured():
                await self.recover_unhealthy_ports(tunnel)

    async def run(self):
        """Main watchdog loop."""
        logger.info("Starting VortexL2 Tunnel Watchdog")
        await self.initialize()
        self.running = True

        while self.running and not self._stop_event.is_set():
            try:
                tunnel_statuses, port_statuses = await self.check_health()

                if tunnel_statuses or port_statuses:
                    logger.debug(self.health_monitor.print_health_report())

                tunnels_to_recover, ports_to_recover = self.health_monitor.get_recovery_needed()
                if tunnels_to_recover or ports_to_recover:
                    logger.warning(
                        f"Recovery needed — Tunnels: {tunnels_to_recover}, Ports: {ports_to_recover}"
                    )
                    await asyncio.sleep(2)
                    await self.recovery_cycle()

                # BUG FIX #5: Use wait_for with timeout so we respond to stop event promptly
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._stop_event.wait()),
                        timeout=self.check_interval
                    )
                    break  # stop event fired
                except asyncio.TimeoutError:
                    pass  # normal timeout — continue loop

            except Exception as e:
                logger.error(f"Error in watchdog loop: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def stop(self):
        """Stop the watchdog."""
        logger.info("Stopping VortexL2 Tunnel Watchdog")
        self.running = False
        self._stop_event.set()


async def main():
    """Main entry point."""
    watchdog = TunnelWatchdog(check_interval=30, recovery_delay=5)

    loop = asyncio.get_running_loop()

    # BUG FIX #4 (same issue): Use loop.add_signal_handler, not signal.signal + create_task
    def handle_signal():
        logger.info("Received shutdown signal")
        loop.create_task(watchdog.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await watchdog.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        await watchdog.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
