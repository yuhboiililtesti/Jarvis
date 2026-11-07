"""LAN discovery, hardware inventory and a passive SSDP probe.

Discovery answers two questions:

  1. Which machines are on my local /24, and which of the expected ports are
     open? Answering this uses *ordinary TCP connect* against a bounded port
     list with a concurrency cap and a short timeout. No raw sockets, no
     administrator rights, no SYN scanning.
  2. What hardware am I running on? CPU, memory, disks, NICs, OS and (best
     effort via ``nvidia-smi``) GPUs.

Discovered hosts are persisted to the ``nodes`` table with ``status='discovered'``
and a ``trusted: false`` capability flag. A host that merely answered a TCP
connect is **never** treated as an enrolled, trusted node; enrolment is a
separate, owner-authorised action elsewhere.

The SSDP probe is passive, best effort and disabled by default so startup can
never block on the network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import platform
import socket
import time
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger

try:  # psutil is a declared dependency but the Core must start without it
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

log = get_logger("jarvis.discovery.service")

__all__ = ["DiscoveryService"]

PROBE_PORTS = (2223, 2225, 2226, 22, 3389, 5900, 8123)
CONNECT_TIMEOUT_S = 0.35
CONCURRENCY = 64
# Ports that imply a useful base_url for a JARVIS node, in preference order.
BASE_URL_PORTS = (2226, 2223, 8123, 5900, 3389, 22)
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_ST = "ssdp:all"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class DiscoveryService:
    def __init__(self, jarvis: Any) -> None:
        self.j = jarvis
        self.config = getattr(jarvis, "config", None)
        self.db = getattr(jarvis, "db", None)
        self.bus = getattr(jarvis, "bus", None)

        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_scan: dict[str, Any] | None = None
        self._last_scan_at: str | None = None

        # Periodic scanning is opt-in; a scan is a real network event.
        try:
            self.interval_s = float(os.environ.get("JARVIS_DISCOVERY_INTERVAL_S", "0") or 0)
        except ValueError:
            self.interval_s = 0.0
        self.ssdp_enabled = _truthy(os.environ.get("JARVIS_DISCOVERY_SSDP"))

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        if self.interval_s > 0:
            self._task = asyncio.create_task(self._loop(), name="discovery.scan")
            self.bus and self.bus.status("discovery", "started",
                                         interval_s=self.interval_s)
            log.info("discovery periodic scan every %ss", self.interval_s)
        else:
            self.bus and self.bus.status("discovery", "idle",
                                         note="periodic scan disabled; call scan() on demand")

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.bus and self.bus.status("discovery", "stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never raise into the kernel
                log.warning("discovery scan failed: %s", exc)
                self.bus and self.bus.degrade("discovery", f"scan failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(30.0, self.interval_s))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    # -- scanning --------------------------------------------------------
    async def scan(self, *, include_ssdp: bool | None = None,
                   max_hosts_per_subnet: int = 254) -> dict[str, Any]:
        """Probe the local /24(s) and return discovered hosts + inventory."""
        ts = now_iso()
        notes: list[str] = []
        targets = self._local_networks()
        if not targets:
            notes.append("no non-loopback IPv4 interface found; LAN scan skipped")

        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = []
        self_ips = {t["source_ip"] for t in targets}
        for net in targets:
            addresses = self._expand(net["network"], max_hosts_per_subnet)
            for ip in addresses:
                tasks.append(self._probe_host(ip, sem))
        results: list[dict[str, Any]] = []
        if tasks:
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for item in gathered:
                if isinstance(item, Exception):
                    notes.append(f"probe error: {item}")
                    continue
                if item and item.get("ports"):
                    results.append(item)

        # add best-effort identity to each responder
        await asyncio.gather(*(self._identify(h) for h in results))

        hosts: list[dict[str, Any]] = []
        for h in results:
            h["self"] = h["ip"] in self_ips
            h["base_url"] = self._base_url(h["ip"], h.get("ports", []))
            h["trusted"] = False
            hosts.append(h)
        hosts.sort(key=_host_sort_key)

        persist = self._persist_hosts(hosts)

        scan_result: dict[str, Any] = {
            "scanned_at": ts,
            "networks": [n["cidr"] for n in targets],
            "hosts": hosts,
            "count": len(hosts),
            "persisted_nodes": persist,
            "inventory": self.inventory(),
            "notes": notes,
        }

        want_ssdp = self.ssdp_enabled if include_ssdp is None else include_ssdp
        if want_ssdp:
            scan_result["ssdp"] = await self._ssdp_scan()
        else:
            scan_result["ssdp"] = {"enabled": False,
                                   "note": "passive SSDP disabled (opt in via "
                                           "JARVIS_DISCOVERY_SSDP=1 or include_ssdp=True)"}

        self._last_scan = scan_result
        self._last_scan_at = ts
        if self.bus is not None:
            try:
                self.bus.publish("discovery.scan", ts=ts, count=len(hosts),
                                 networks=scan_result["networks"],
                                 hosts=[{"ip": h["ip"], "ports": h["ports"]} for h in hosts])
            except Exception:  # pragma: no cover
                pass
        try:
            self.db.audit("discovery.scan", detail={"count": len(hosts),
                                                    "networks": scan_result["networks"]})
        except Exception:  # pragma: no cover
            pass
        return scan_result

    def _local_networks(self) -> list[dict[str, str]]:
        """Distinct local /24s derived from active IPv4 interfaces."""
        found: dict[str, dict[str, str]] = {}
        if psutil is None:
            return []
        try:
            ifaces = psutil.net_if_addrs()
        except Exception as exc:  # pragma: no cover
            log.warning("could not enumerate interfaces: %s", exc)
            return []
        for name, addrs in ifaces.items():
            for snic in addrs:
                if snic.family != socket.AF_INET:
                    continue
                ip = snic.address
                if not _usable_ipv4(ip):
                    continue
                try:
                    net = ipaddress.ip_network(f"{ip}/24", strict=False)
                except ValueError:
                    continue
                key = str(net)
                found.setdefault(key, {
                    "cidr": key,
                    "network": net.with_prefixlen.split("/")[0],
                    "source_ip": ip,
                    "interface": name,
                })
        return list(found.values())

    @staticmethod
    def _expand(network_base: str, limit: int) -> list[str]:
        """Addresses .1 .. .254 inside a /24, capped at ``limit``."""
        parts = network_base.split(".")
        if len(parts) != 4:
            return []
        prefix = ".".join(parts[:3])
        out = [f"{prefix}.{i}" for i in range(1, 255)]
        return out[:max(0, limit)]

    async def _probe_host(self, ip: str, sem: asyncio.Semaphore) -> dict[str, Any] | None:
        async with sem:
            open_ports: list[int] = []
            for port in PROBE_PORTS:
                if await self._port_open(ip, port):
                    open_ports.append(port)
            if not open_ports:
                return None
            return {"ip": ip, "ports": open_ports, "name": None, "identity": None}

    @staticmethod
    async def _port_open(ip: str, port: int) -> bool:
        try:
            conn = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(conn, timeout=CONNECT_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            return False
        except Exception:  # pragma: no cover - defensive
            return False
        try:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
            except Exception:  # pragma: no cover
                pass
        finally:
            _ = reader
        return True

    async def _identify(self, host: dict[str, Any]) -> None:
        """Best-effort reverse DNS / hostname; never fail the scan on this."""
        ip = host["ip"]
        name = await asyncio.to_thread(_reverse_dns, ip)
        host["name"] = name or ip
        host["identity"] = {"rdns": name}

    @staticmethod
    def _base_url(ip: str, ports: list[int]) -> str | None:
        for port in BASE_URL_PORTS:
            if port in ports:
                scheme = "http" if port in (2223, 2226, 8123) else "ssh"
                return f"{scheme}://{ip}:{port}"
        return None

    # -- persistence -----------------------------------------------------
    def _persist_hosts(self, hosts: list[dict[str, Any]]) -> dict[str, int]:
        inserted = updated = 0
        ts = now_iso()
        for h in hosts:
            node_key = f"disc_{h['ip'].replace('.', '_').replace(':', '_')}"
            caps = {
                "ports": h.get("ports", []),
                "identity": h.get("identity"),
                "discovered_by": "tcp-connect",
                "trusted": False,
                "last_scan": ts,
            }
            try:
                row = self.db.query_one("SELECT id FROM nodes WHERE id=?", (node_key,))
                payload = (h.get("name") or h["ip"], h.get("base_url"),
                           json.dumps(caps, ensure_ascii=False))
                if row is not None:
                    self.db.execute(
                        "UPDATE nodes SET name=?, base_url=COALESCE(?, base_url), "
                        "last_seen=?, capabilities=?, status='discovered' WHERE id=?",
                        (payload[0], payload[1], ts, payload[2], node_key))
                    updated += 1
                else:
                    self.db.execute(
                        "INSERT INTO nodes(id,name,base_url,kind,first_seen,last_seen,"
                        "capabilities,status) VALUES(?,?,?,?,?,?,?,?)",
                        (node_key, payload[0], payload[1], "node", ts, ts,
                         payload[2], "discovered"))
                    inserted += 1
            except Exception as exc:  # pragma: no cover - persistence must not kill scan
                log.warning("could not persist discovered host %s: %s", h["ip"], exc)
        return {"inserted": inserted, "updated": updated}

    def hosts(self, *, only_discovered: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT id,name,base_url,kind,first_seen,last_seen,capabilities,status FROM nodes"
        if only_discovered:
            sql += " WHERE status='discovered'"
        sql += " ORDER BY last_seen DESC"
        out = []
        for r in self.db.query(sql):
            d = dict(r)
            d["capabilities"] = _loads(d.get("capabilities"))
            out.append(d)
        return out

    # -- hardware inventory ---------------------------------------------
    def inventory(self) -> dict[str, Any]:
        inv: dict[str, Any] = {"collected_at": now_iso()}
        inv["os"] = {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "node": platform.node(),
            "python": platform.python_version(),
        }
        if psutil is None:
            inv["note"] = "psutil not installed; hardware inventory limited"
            return inv
        try:
            inv["cpu"] = {
                "brand": platform.processor() or None,
                "physical_cores": psutil.cpu_count(logical=False),
                "logical_cores": psutil.cpu_count(logical=True),
            }
            try:
                freq = psutil.cpu_freq()
                if freq:
                    inv["cpu"]["freq_mhz"] = round(float(freq.current), 1)
            except Exception:
                pass
        except Exception as exc:  # pragma: no cover
            inv["cpu_error"] = str(exc)
        try:
            vm = psutil.virtual_memory()
            inv["memory"] = {"total_bytes": int(vm.total), "available_bytes": int(vm.available)}
        except Exception as exc:  # pragma: no cover
            inv["memory_error"] = str(exc)
        try:
            disks = []
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except Exception:
                    continue
                disks.append({
                    "device": part.device, "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_bytes": int(usage.total), "free_bytes": int(usage.free),
                })
            inv["disks"] = disks
        except Exception as exc:  # pragma: no cover
            inv["disks_error"] = str(exc)
        try:
            nics = []
            stats = psutil.net_if_stats()
            for name, addrs in psutil.net_if_addrs().items():
                entry: dict[str, Any] = {"name": name, "addresses": []}
                for snic in addrs:
                    if snic.family == socket.AF_INET:
                        entry["addresses"].append(
                            {"type": "ipv4", "address": snic.address,
                             "netmask": snic.netmask})
                    elif snic.family == socket.AF_INET6:
                        entry["addresses"].append({"type": "ipv6", "address": snic.address})
                st = stats.get(name)
                if st is not None:
                    entry["is_up"] = bool(st.isup)
                    entry["speed_mbps"] = int(st.speed)
                nics.append(entry)
            inv["nics"] = nics
        except Exception as exc:  # pragma: no cover
            inv["nics_error"] = str(exc)
        try:
            inv["uptime_s"] = round(time.time() - psutil.boot_time(), 1)
        except Exception:  # pragma: no cover
            pass
        inv["gpus"] = _gpus_via_nvidia_smi()
        if not inv["gpus"]:
            inv["gpu_note"] = "nvidia-smi unavailable or no NVIDIA GPU detected"
        return inv

    # -- passive SSDP ----------------------------------------------------
    async def _ssdp_scan(self, timeout: float = 2.0) -> dict[str, Any]:
        try:
            responses = await asyncio.wait_for(
                asyncio.to_thread(_ssdp_probe, timeout), timeout=timeout + 2.0)
            return {"enabled": True, "count": len(responses), "responses": responses}
        except asyncio.TimeoutError:
            return {"enabled": True, "count": 0, "error": "ssdp probe timed out"}
        except Exception as exc:
            return {"enabled": True, "count": 0, "error": f"{type(exc).__name__}: {exc}"}

    # -- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        try:
            discovered = len(self.hosts())
        except Exception:  # pragma: no cover
            discovered = 0
        return {
            "component": "discovery",
            "running": self._running,
            "interval_s": self.interval_s,
            "ssdp_enabled": self.ssdp_enabled,
            "last_scan_at": self._last_scan_at,
            "last_scan_count": (self._last_scan or {}).get("count"),
            "discovered_nodes": discovered,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _usable_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_unspecified or addr.is_reserved)


def _reverse_dns(ip: str) -> str | None:
    try:
        name, _, _ = socket.gethostbyaddr(ip)
        return name or None
    except (OSError, socket.herror, socket.gaierror):
        return None
    except Exception:  # pragma: no cover
        return None


def _host_sort_key(host: dict[str, Any]) -> tuple[int, str]:
    try:
        last = int(host["ip"].split(".")[-1])
    except (ValueError, KeyError, IndexError):
        last = 0
    return (last, host.get("ip", ""))


def _loads(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _gpus_via_nvidia_smi() -> list[dict[str, Any]]:
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    query = "name,memory.total,driver_version"
    try:
        proc = subprocess.run([exe, f"--query-gpu={query}", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=2.5)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in (proc.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts:
            continue
        out.append({
            "name": parts[0],
            "memory_total": parts[1] if len(parts) > 1 else None,
            "driver": parts[2] if len(parts) > 2 else None,
        })
    return out


def _ssdp_probe(timeout: float) -> list[dict[str, Any]]:
    """Send one M-SEARCH and collect responses. Passive, best effort, no admin."""
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        f"ST: {SSDP_ST}\r\n\r\n"
    ).encode("ascii")
    responses: list[dict[str, Any]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        try:
            sock.sendto(message, SSDP_ADDR)
        except OSError:
            return []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                break
            headers = _parse_ssdp(data.decode("utf-8", errors="replace"))
            headers["from"] = addr[0]
            responses.append(headers)
            if len(responses) >= 64:
                break
    finally:
        sock.close()
    return responses


def _parse_ssdp(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in text.splitlines()[1:]:
        key, _, value = line.partition(":")
        if not _:
            continue
        k = key.strip().lower()
        if k in ("location", "server", "st", "usn", "cache-control"):
            out[k] = value.strip()
    return out
