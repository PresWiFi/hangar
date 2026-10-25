"""Computer stats (CPU, memory, disks, network, NVIDIA GPUs, top processes), sampled in the background
so every page load is instant and the dashboard can draw short history sparklines."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from collections import deque

import psutil

GPU_FIELDS = ["name", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu",
              "power.draw", "power.limit", "fan.speed"]
SKIP_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "autofs", "iso9660", "udf", "nsfs", "proc", "sysfs"}
INTERVAL = 3
HISTORY = 120  # samples (6 minutes)


def _num(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


class SystemMonitor:
    def __init__(self) -> None:
        self._net = None
        self._nvidia = shutil.which("nvidia-smi")
        self.history: deque = deque(maxlen=HISTORY)
        self.latest: dict = {}
        self.slow: dict = {"disks": [], "procs": [], "gpus": [], "temps": []}
        self.hostname = os.environ.get("HANGAR_HOSTNAME") or socket.gethostname()
        # Optional fixed list of folders whose drives to show (useful in Docker, where mounts look odd).
        self.disk_paths = [p for p in os.environ.get("HANGAR_DISKS", "").split(",") if p.strip()]
        psutil.cpu_percent(percpu=True)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="system").start()

    def _run(self) -> None:
        tick = 0
        while True:
            try:
                self._sample(slow=tick % 3 == 0)
            except Exception:
                pass
            tick += 1
            time.sleep(INTERVAL)

    def _sample(self, slow: bool) -> None:
        now = time.time()
        per_core = psutil.cpu_percent(percpu=True)
        cpu = sum(per_core) / len(per_core) if per_core else 0
        mem, swap = psutil.virtual_memory(), psutil.swap_memory()
        net = psutil.net_io_counters()
        up = down = None
        if self._net and now > self._net[0]:
            dt = now - self._net[0]
            up, down = max(0, (net.bytes_sent - self._net[1]) / dt), max(0, (net.bytes_recv - self._net[2]) / dt)
        self._net = (now, net.bytes_sent, net.bytes_recv)
        try:
            load = os.getloadavg()
        except (OSError, AttributeError):
            load = None
        if slow:
            self.slow = {"disks": self._disks(), "procs": self._procs(), "gpus": self._gpus(), "temps": self._temps()}
        self.latest = {
            "time": now, "hostname": self.hostname, "platform": f"{platform.system()} {platform.release()}",
            "cpu": cpu, "per_core": per_core, "cores": len(per_core), "load": load,
            "mem_used": mem.total - mem.available, "mem_total": mem.total, "mem_pct": mem.percent,
            "swap_used": swap.used, "swap_total": swap.total,
            "net_up": up, "net_down": down, "uptime": now - psutil.boot_time(), **self.slow,
        }
        gpu = self.slow["gpus"][0]["util"] if self.slow["gpus"] else None
        self.history.append({"t": now, "cpu": round(cpu, 1), "mem": mem.percent, "up": up, "down": down, "gpu": gpu})

    def _disks(self) -> list[dict]:
        out, seen = [], set()
        if self.disk_paths:
            targets = [(p.strip(), p.strip()) for p in self.disk_paths]
        else:
            targets = []
            for part in psutil.disk_partitions(all=False):
                m = part.mountpoint
                if part.fstype.lower() in SKIP_FS or m.startswith(("/snap", "/boot", "/etc/", "/proc", "/sys", "/dev")):
                    continue
                targets.append((m, m))
        for label, path in targets:
            try:
                u = psutil.disk_usage(path)
            except OSError:
                continue
            key = (u.total, u.used // (1 << 30))
            if not u.total or key in seen:
                continue
            seen.add(key)
            out.append({"mount": label, "used": u.used, "total": u.total, "pct": u.percent})
        return out[:8]

    def _procs(self, n: int = 8) -> list[dict]:
        procs = []
        cores = psutil.cpu_count() or 1
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cmdline"]):
            info = p.info
            if not info.get("memory_info"):
                continue
            name = info["name"] or "?"
            cmd = info.get("cmdline") or []
            if name in {"node", "bun"} and any("claude" in c for c in cmd[:2]):
                name = "claude"
            procs.append({"pid": info["pid"], "name": name, "cpu": (info["cpu_percent"] or 0) / cores,
                          "mem": info["memory_info"].rss})
        procs.sort(key=lambda p: (p["cpu"], p["mem"]), reverse=True)
        return procs[:n]

    def _gpus(self) -> list[dict]:
        if not self._nvidia:
            return []
        try:
            r = subprocess.run([self._nvidia, f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        gpus = []
        for line in r.stdout.strip().splitlines():
            values = [v.strip() for v in line.split(",")]
            if len(values) != len(GPU_FIELDS):
                continue
            name, *rest = values
            util, used, total, temp, power, limit, fan = map(_num, rest)
            gpus.append({"name": name.replace("NVIDIA ", "").replace("GeForce ", ""), "util": util,
                         "mem_used": used, "mem_total": total, "temp": temp, "power": power,
                         "power_limit": limit, "fan": fan})
        return gpus

    @staticmethod
    def _temps() -> list[dict]:
        try:
            sensors = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return []
        out = []
        for chip, entries in sensors.items():
            for e in entries:
                if e.current and 0 < e.current < 130:
                    out.append({"name": e.label or chip, "chip": chip, "c": e.current})
        out.sort(key=lambda t: -t["c"])
        return out[:4]

    def snapshot(self) -> dict:
        return {"now": self.latest, "history": list(self.history)}


monitor = SystemMonitor()
