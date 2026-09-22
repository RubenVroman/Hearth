"""Is the Tuya hardware on the network, whatever Home Assistant thinks?

Tuya devices listen on TCP 6668 for local control. Checking that port answers
separates two states that produce the same silence from Home Assistant's side:
the device is unpaired, or the device is unplugged. Only the first is fixed by
adding an integration.

What this deliberately does not do:

* **Identify anything.** An open 6668 says "a Tuya device is here", not which
  one. Naming a device needs its id and local key, which come from the Tuya
  account during pairing. Hearth reports the address and stops there.
* **Sweep the subnet.** Only operator-configured hosts (``TUYA_LAN_HOSTS``) or
  explicitly passed ones are probed, and only private addresses. Hearth is a
  house agent, not a scanner.
* **Replace Home Assistant's discovery.** Tuya broadcast discovery needs to
  receive UDP on the LAN; HA runs with host networking and can, Hearth sits on
  the compose bridge and cannot. Pairing discovery is HA's job.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Sequence
from typing import Any

from hearth.config import settings

# A house has a handful of these. A long list is a scan, not a health check.
MAX_HOSTS = 24


def _private(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A hostname — allowed, since the LAN resolver is the house's own.
        return bool(host) and not host.lower().startswith(("localhost", "0.0.0.0"))
    return bool(address.is_private and not address.is_loopback)


async def _probe_one(host: str, port: int, timeout: float) -> dict[str, Any]:
    if not _private(host):
        return {
            "host": host,
            "open": False,
            "error": "only private LAN addresses are probed",
        }
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        del reader
        return {"host": host, "open": True}
    except asyncio.TimeoutError:
        return {"host": host, "open": False, "error": "timed out"}
    except OSError as exc:
        return {"host": host, "open": False, "error": exc.strerror or str(exc)}
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def probe_tuya_lan(
    hosts: Sequence[str] | None = None,
    *,
    port: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Check which configured Tuya addresses still answer on the LAN port."""
    candidates = [str(host).strip() for host in (hosts or settings.tuya_lan_host_list)]
    candidates = [host for host in candidates if host][:MAX_HOSTS]
    tuya_port = int(port or settings.tuya_lan_port)
    wait = float(timeout or settings.tuya_lan_timeout_seconds)

    if not candidates:
        message = (
            "No Tuya LAN addresses are configured. Set TUYA_LAN_HOSTS to the "
            "device IPs so I can tell an unpaired device from an offline one."
        )
        return {
            "ok": True,
            "port": tuya_port,
            "hosts": [],
            "open_count": 0,
            "configured": False,
            "speak": message,
        }

    results = await asyncio.gather(
        *(_probe_one(host, tuya_port, wait) for host in candidates)
    )
    answering = [row for row in results if row["open"]]
    silent = [row for row in results if not row["open"]]

    if answering and not silent:
        speak = (
            f"All {len(answering)} Tuya addresses answer on port {tuya_port}. "
            "The hardware is on the network."
        )
    elif answering:
        quiet = ", ".join(str(row["host"]) for row in silent)
        speak = (
            f"{len(answering)} of {len(results)} Tuya addresses answer on port "
            f"{tuya_port}. No answer from {quiet} — powered off, or the DHCP "
            "lease moved."
        )
    else:
        speak = (
            f"None of the {len(results)} configured Tuya addresses answer on port "
            f"{tuya_port}. Check the devices are powered and still hold those "
            "addresses."
        )

    return {
        "ok": True,
        "port": tuya_port,
        "hosts": results,
        "open_count": len(answering),
        "configured": True,
        # An open port proves a Tuya device is there, never which one.
        "identifies_devices": False,
        "speak": speak,
    }


__all__ = ["MAX_HOSTS", "probe_tuya_lan"]
