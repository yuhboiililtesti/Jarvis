"""Network introspection: ``network.info``, ``network.ping``, ``network.ports``.

All three avoid shelling out:

  * ``network.info``  - interfaces/IPs via psutil, primary address via a UDP
    socket trick, gateway/DNS via a read-only OS lookup (registry on Windows,
    ``/proc`` + ``/etc/resolv.conf`` on POSIX) that degrades to ``null``.
  * ``network.ping``  - reachability by opening a TCP connection; it does *not*
    depend on a ``ping`` binary (ICMP is often blocked and needs privileges).
  * ``network.ports`` - listening sockets via psutil, tolerating AccessDenied.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import time
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

try:  # optional at import time so the Core still loads without psutil
    import psutil  # type: ignore
except Exception:  # pragma: no cover - exercised only on a bare interpreter
    psutil = None  # type: ignore

log = get_logger("jarvis.tools.network")

__all__ = ["NetworkInfoTool", "NetworkPingTool", "NetworkPortsTool", "PSUTIL_MISSING"]

PSUTIL_MISSING = "psutil is not installed; network inspection is unavailable"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _primary_ip() -> str | None:
    """Best-effort local address used to reach the outside world (no packets)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _fam_label(fam: Any) -> str:
    if fam == socket.AF_INET:
        return "ipv4"
    if fam == socket.AF_INET6:
        return "ipv6"
    if getattr(psutil, "AF_LINK", None) == fam:
        return "mac"
    return str(fam)


def _posix_gateway() -> str | None:
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            lines = fh.readlines()[1:]
    except OSError:
        return None
    for line in lines:
        cols = line.split()
        if len(cols) < 4:
            continue
        dest, gw, flags = cols[1], cols[2], cols[3]
        try:
            if dest == "00000000" and (int(flags, 16) & 0x2):
                return socket.inet_ntoa(struct.pack("<I", int(gw, 16)))
        except ValueError:
            continue
    return None


def _windows_gateway() -> str | None:
    """Read the IPv4 default route via iphlpapi.GetIpForwardTable (read-only)."""
    try:
        import ctypes
        from ctypes import wintypes

        class MIB_IPFORWARDROW(ctypes.Structure):
            _fields_ = [
                ("dwForwardDest", wintypes.DWORD),
                ("dwForwardMask", wintypes.DWORD),
                ("dwForwardPolicy", wintypes.DWORD),
                ("dwForwardNextHop", wintypes.DWORD),
                ("dwForwardIfIndex", wintypes.DWORD),
                ("dwForwardType", wintypes.DWORD),
                ("dwForwardProto", wintypes.DWORD),
                ("dwForwardAge", wintypes.DWORD),
                ("dwForwardNextHopAS", wintypes.DWORD),
                ("dwForwardMetric1", wintypes.DWORD),
                ("dwForwardMetric2", wintypes.DWORD),
                ("dwForwardMetric3", wintypes.DWORD),
                ("dwForwardMetric4", wintypes.DWORD),
                ("dwForwardMetric5", wintypes.DWORD),
            ]

        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        size = wintypes.ULONG(0)
        iphlpapi.GetIpForwardTable(None, ctypes.byref(size), False)
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetIpForwardTable(buf, ctypes.byref(size), False) != 0:
            return None
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        row_size = ctypes.sizeof(MIB_IPFORWARDROW)
        base = ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD)
        best = None
        for i in range(count):
            row = MIB_IPFORWARDROW.from_address(base + i * row_size)
            if row.dwForwardDest == 0 and row.dwForwardMask == 0:
                metric = row.dwForwardMetric1
                if best is None or metric < best[0]:
                    best = (metric, row.dwForwardNextHop)
        if best is None or best[1] == 0:
            return None
        return socket.inet_ntoa(struct.pack("<I", best[1]))
    except Exception as exc:  # pragma: no cover - platform/ctypes variance
        log.debug("windows gateway lookup failed: %s", exc)
        return None


def _gateway() -> str | None:
    return _windows_gateway() if os.name == "nt" else _posix_gateway()


def _split_dns(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [part for part in value.replace(",", " ").replace(";", " ").split() if part]


def _windows_dns() -> list[str]:
    servers: list[str] = []
    try:
        import winreg  # Windows-only stdlib module
    except ImportError:  # pragma: no cover
        return servers
    base = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
            for name in ("NameServer", "DhcpNameServer"):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                    servers.extend(_split_dns(value))
                except FileNotFoundError:
                    pass
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base + r"\Interfaces") as ifaces:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(ifaces, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(ifaces, sub) as sk:
                        for name in ("NameServer", "DhcpNameServer"):
                            try:
                                value, _ = winreg.QueryValueEx(sk, name)
                                servers.extend(_split_dns(value))
                            except FileNotFoundError:
                                pass
                except OSError:
                    continue
    except OSError:
        pass
    return _unique(servers)


def _posix_dns() -> list[str]:
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.append(parts[1])
    except OSError:
        return []
    return _unique(servers)


def _dns_servers() -> list[str]:
    return _windows_dns() if os.name == "nt" else _posix_dns()


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@register(["net.info"])
class NetworkInfoTool(BaseTool):
    spec = ToolSpec(
        name="network.info",
        summary="Report local interfaces, IP addresses, gateway and DNS servers.",
        version="1.0",
        category="system",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {hostname, primary_ip, interfaces, gateway, dns, notes}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            if psutil is None:
                return ToolResult(ok=False, error=PSUTIL_MISSING)

            notes: list[str] = []
            interfaces: list[dict[str, Any]] = []
            try:
                addrs = psutil.net_if_addrs()
                stats = psutil.net_if_stats()
            except Exception as exc:
                return ToolResult(ok=False, error=f"interface enumeration failed: {exc}")

            for name, addr_list in addrs.items():
                stat = stats.get(name)
                entry: dict[str, Any] = {
                    "name": name,
                    "up": bool(stat.isup) if stat is not None else None,
                    "speed_mbps": getattr(stat, "speed", None) if stat is not None else None,
                    "addresses": [
                        {
                            "family": _fam_label(a.family),
                            "address": a.address,
                            "netmask": a.netmask,
                            "broadcast": a.broadcast,
                        }
                        for a in addr_list
                    ],
                }
                interfaces.append(entry)

            try:
                hostname = socket.gethostname()
            except OSError:
                hostname = None

            gateway = _gateway()
            dns = _dns_servers()
            if gateway is None:
                notes.append("gateway could not be determined on this platform")
            if not dns:
                notes.append("DNS servers could not be determined")

            data = {
                "hostname": hostname,
                "primary_ip": _primary_ip(),
                "interfaces": interfaces,
                "gateway": gateway,
                "dns": dns,
                "notes": notes,
            }
            return ToolResult(
                ok=True,
                data=data,
                evidence={"method": "psutil+socket", "interface_count": len(interfaces)},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("network.info failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["net.ping", "ping"])
class NetworkPingTool(BaseTool):
    spec = ToolSpec(
        name="network.ping",
        summary="TCP-connect reachability check for a host:port (no ping binary).",
        version="1.0",
        category="system",
        side_effect=SideEffect.NETWORK,
        params=(
            ToolParam("host", "string", "Hostname or IP to reach.", required=True),
            ToolParam("port", "integer", "TCP port to connect to.", default=443),
            ToolParam("timeout_s", "number", "Connect timeout in seconds.", default=2.0),
        ),
        returns="object: {host, port, reachable, latency_ms, method}",
        needs_network=True,
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            host = str(kwargs.get("host") or "").strip()
            if not host:
                return ToolResult(ok=False, error="host is required")
            try:
                port = int(kwargs.get("port") or 443)
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="port must be an integer")
            if not (0 < port < 65536):
                return ToolResult(ok=False, error="port must be between 1 and 65535")
            try:
                timeout = max(0.1, float(kwargs.get("timeout_s") or 2.0))
            except (TypeError, ValueError):
                timeout = 2.0

            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=timeout)
            except (asyncio.TimeoutError, OSError) as exc:
                latency = round((time.perf_counter() - t0) * 1000.0, 1)
                return ToolResult(
                    ok=False,
                    data={"host": host, "port": port, "reachable": False,
                          "latency_ms": latency, "method": "tcp_connect"},
                    error=f"{host}:{port} unreachable ({type(exc).__name__}: {exc})",
                    evidence={"method": "tcp_connect", "reachable": False},
                    certainty="settled",
                )

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover - best effort
                pass
            latency = round((time.perf_counter() - t0) * 1000.0, 1)
            return ToolResult(
                ok=True,
                data={"host": host, "port": port, "reachable": True,
                      "latency_ms": latency, "method": "tcp_connect"},
                evidence={"method": "tcp_connect", "reachable": True,
                          "latency_ms": latency},
                certainty="settled",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("network.ping failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    def verify(self, ctx: ToolContext, result: ToolResult):
        """Remote reachability has no local read-back; the probe is its own evidence."""
        return None


@register(["net.ports"])
class NetworkPortsTool(BaseTool):
    spec = ToolSpec(
        name="network.ports",
        summary="List locally listening TCP/UDP ports with owning process.",
        version="1.0",
        category="system",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {ports: [{address, port, pid, process, family}], count}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            if psutil is None:
                return ToolResult(ok=False, error=PSUTIL_MISSING)
            try:
                conns = psutil.net_connections(kind="inet")
            except psutil.AccessDenied as exc:
                return ToolResult(
                    ok=False,
                    error=("access denied enumerating connections "
                           f"({exc}); try running with elevated privileges"),
                    evidence={"method": "psutil.net_connections", "access_denied": True},
                )

            listen_status = getattr(psutil, "CONN_LISTEN", "LISTEN")
            ports: list[dict[str, Any]] = []
            for conn in conns:
                if conn.status != listen_status:
                    continue
                proc_name = None
                if conn.pid:
                    try:
                        proc_name = psutil.Process(conn.pid).name()
                    except Exception:
                        proc_name = None
                laddr = conn.laddr
                address = getattr(laddr, "ip", None)
                port = getattr(laddr, "port", None)
                if address is None and isinstance(laddr, tuple) and laddr:
                    address = laddr[0]
                if port is None and isinstance(laddr, tuple) and len(laddr) > 1:
                    port = laddr[1]
                ports.append({
                    "address": address,
                    "port": port,
                    "pid": conn.pid,
                    "process": proc_name,
                    "family": "ipv6" if conn.family == socket.AF_INET6 else "ipv4",
                })
            ports.sort(key=lambda p: (p.get("port") or 0))
            return ToolResult(
                ok=True,
                data={"ports": ports, "count": len(ports)},
                evidence={"method": "psutil.net_connections", "count": len(ports)},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("network.ports failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
