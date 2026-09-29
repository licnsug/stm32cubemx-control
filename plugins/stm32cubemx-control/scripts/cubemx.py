#!/usr/bin/env python3
"""STM32CubeMX control toolkit.

Programmatic control of STM32CubeMX projects:

  * discover what an MCU can be configured with, from CubeMX's own device
    database (db/mcu/*.xml)
  * read and modify any parameter in a .ioc project file
  * let CubeMX itself validate and normalise hand edits
  * regenerate code headlessly, with guards and a backup
  * verify the result by building with Keil MDK (UV4)

The toolkit never writes inside the target project except through CubeMX
itself, during a generation the caller explicitly requested. Backups, logs and
CubeMX script files go to the toolkit work directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

def _default_work_dir() -> Path:
    """Backups, script files and logs land here, never inside the target project."""
    for var in ("CUBEMX_TOOLKIT_WORK_DIR", "CODEX_WORK_DIR"):
        raw = os.environ.get(var)
        if raw:
            return Path(raw) / "cubemx-toolkit"
    return Path.home() / ".cubemx-toolkit"


DEFAULT_WORK_DIR = _default_work_dir()

GENERATE_TIMEOUT = int(os.environ.get("CUBEMX_TOOLKIT_GENERATE_TIMEOUT", "300"))
# CubeMX can block on a dialog it cannot show in script mode. Treat a log that
# stops growing as a hang instead of waiting out the full timeout.
STALL_TIMEOUT = int(os.environ.get("CUBEMX_TOOLKIT_STALL_TIMEOUT", "90"))
BUILD_TIMEOUT = int(os.environ.get("CUBEMX_TOOLKIT_BUILD_TIMEOUT", "600"))

# Log lines CubeMX emits look like "2026-09-22 22:35:09,050 [INFO] ...". Anything
# else on stdout is either an echoed console command or its OK / KO verdict.
_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \[")
_NOISE_PREFIXES = ("log4j", "Configure log4j", "WARNING:", "Warning:", "Bye bye")

# The console's own help output lists these; anything else on stdout is CubeMX
# noise, including Java stack traces which would otherwise read as commands.
_CONSOLE_VERBS = frozenset("""
    log set import ocxtest multiconfig load get lock reset clearpinout
    possible_value not_a_possible_value add check csv generate tinyload
    waitclock switch unset export list loadboard project setdriver
    setstructure setcopylibrary pinout disable tooltip clock updateipui swmgr
    config cpload cpexport user_analytics simulate ispluginerror exit_mx
    setprop getprop getenv xcopy xts xtest xbuild popupwrapper xcubedl help
    exit script
""".split())

# Build artefacts a backup does not need; copying them turns a 3 MB source tree
# into a 34 MB backup for no benefit.
BACKUP_SKIP_SUFFIXES = (
    ".o", ".crf", ".d", ".dep", ".axf", ".hex", ".bin", ".map", ".lnp", ".lst",
    ".htm", ".build_log", ".iex", ".sct.bak",
)
BACKUP_SKIP_DIRS = (".vscode", "MXTmpFiles", "M2TmpFiles", "DebugConfig")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _ps(command: str, timeout: int = 60) -> str:
    """Run a PowerShell snippet and return stdout (never raises on exit code)."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return "ERROR: {}".format(exc)


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _write_text(path: Path, text: str) -> None:
    """UTF-8 without BOM and LF newlines, independent of the Python version."""
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def expand_mcu_ref(name: str):
    """Expand a database MCU name into the order codes it covers.

    STM32G431C(6-8-B)Ux means the C6Ux, C8Ux and CBUx parts, and a project writes
    itself as STM32G431CBUx, so the two need reconciling before lookup.
    """
    variants = {""}
    for part in re.split(r"(\([^)]*\))", name):
        if not part:
            continue
        if part.startswith("(") and part.endswith(")"):
            options = [o for o in part[1:-1].split("-") if o]
        else:
            options = [part]
        variants = {prefix + option for prefix in variants for option in options}
    return {v.lower() for v in variants}


def escape_ioc_key(name: str) -> str:
    """Keys in a .ioc escape spaces and colons; values do not."""
    return name.replace(":", "\\:").replace(" ", "\\ ")


def _iter_xml_docs(path: Path):
    """CubeMX db files may hold several concatenated XML documents."""
    raw = path.read_bytes()
    chunks = raw.split(b"<?xml")
    if len(chunks) == 1:
        try:
            yield ET.fromstring(raw)
        except ET.ParseError:
            return
        return
    for chunk in chunks[1:]:
        try:
            yield ET.fromstring(b"<?xml" + chunk)
        except ET.ParseError:
            continue


# --------------------------------------------------------------------------
# environment discovery
# --------------------------------------------------------------------------

def _resolve_shortcut(lnk: Path):
    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        "$l = $s.CreateShortcut('" + _ps_quote(str(lnk)) + "'); "
        "if ($l.TargetPath) { $l.TargetPath }"
    )
    out = _ps(ps).strip()
    return Path(out) if out and "ERROR" not in out else None


def find_cubemx(deep: bool = False):
    """Locate STM32CubeMX.exe."""
    for var in ("STM32CUBEMX", "CUBEMX_HOME", "STM32CUBEMX_HOME"):
        raw = os.environ.get(var)
        if raw:
            base = Path(raw)
            for cand in (base, base / "STM32CubeMX.exe"):
                if cand.is_file():
                    return cand

    start_menus = []
    program_data = os.environ.get("ProgramData")
    if program_data:
        start_menus.append(Path(program_data) / "Microsoft/Windows/Start Menu")
    appdata = os.environ.get("APPDATA")
    if appdata:
        start_menus.append(Path(appdata) / "Microsoft/Windows/Start Menu")
    for menu in start_menus:
        if not menu.exists():
            continue
        try:
            links = sorted(menu.rglob("STM32CubeMX.lnk"))
        except OSError:
            links = []
        for lnk in links:
            target = _resolve_shortcut(lnk)
            if target and target.is_file():
                return target

    common = [
        Path("C:/Program Files/STMicroelectronics/STM32Cube/STM32CubeMX/STM32CubeMX.exe"),
        Path("C:/Program Files (x86)/STMicroelectronics/STM32Cube/STM32CubeMX/STM32CubeMX.exe"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "STMicroelectronics/STM32Cube/STM32CubeMX/STM32CubeMX.exe",
        Path(os.environ.get("USERPROFILE", "")) / "STM32CubeMX/STM32CubeMX.exe",
    ]
    for drive in ("C:", "D:", "E:", "F:"):
        common.append(Path(drive + "/STM32MX/STM32CubeMX.exe"))
        common.append(Path(drive + "/STM32CubeMX/STM32CubeMX.exe"))
    for cand in common:
        if cand.is_file():
            return cand

    for hive in ("HKLM", "HKCU", "HKLM32"):
        if hive == "HKLM":
            base = r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        elif hive == "HKLM32":
            base = r"HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
        else:
            base = r"HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        ps = (
            "Get-ChildItem '" + base + "' -ErrorAction SilentlyContinue | "
            "ForEach-Object { $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue; "
            "if ($p.DisplayName -match 'STM32CubeMX' -and $p.InstallLocation) { $p.InstallLocation } }"
        )
        for line in _ps(ps).splitlines():
            line = line.strip()
            if line and Path(line).exists():
                cand = Path(line) / "STM32CubeMX.exe"
                if cand.is_file():
                    return cand

    if deep:
        for drive in ("C:/", "D:/", "E:/", "F:/"):
            root = Path(drive)
            if not root.exists():
                continue
            for pattern in ("STM32CubeMX.exe", "*/STM32CubeMX.exe", "*/*/STM32CubeMX.exe",
                            "*/STM32MX/STM32CubeMX.exe", "*/*/STM32MX/STM32CubeMX.exe"):
                try:
                    for cand in root.glob(pattern):
                        if cand.is_file():
                            return cand
                except OSError:
                    continue
    return None


def find_keil_uv4():
    """Locate Keil UV4.exe (MDK command line build tool)."""
    ps = (
        "foreach ($k in @('HKLM:\\SOFTWARE\\WOW6432Node\\Keil\\Products\\MDK',"
        "'HKLM:\\SOFTWARE\\Keil\\Products\\MDK',"
        "'HKCU:\\SOFTWARE\\Keil\\Products\\MDK')) { "
        "$p = (Get-ItemProperty $k -ErrorAction SilentlyContinue).Path; if ($p) { $p } }"
    )
    for line in _ps(ps).splitlines():
        line = line.strip()
        if line and Path(line).exists():
            cand = Path(line).parent / "UV4" / "UV4.exe"
            if cand.is_file():
                return cand
    for drive in ("C:", "D:", "E:", "F:"):
        for name in ("Keil_v5", "Keil", "Keil_v4"):
            cand = Path(drive + "/" + name + "/UV4/UV4.exe")
            if cand.is_file():
                return cand
    return None


def find_repository():
    """Return (repository_path, [pack_names])."""
    for var in ("STM32CUBE_REPOSITORY", "STM32CUBE_REPO"):
        raw = os.environ.get(var)
        if raw and Path(raw).exists():
            repo = Path(raw)
            return repo, sorted(p.name for p in repo.iterdir() if p.name.startswith("STM32Cube_FW_"))
    candidates = []
    home = os.environ.get("USERPROFILE")
    if home:
        candidates.append(Path(home) / "STM32Cube/Repository")
    for drive in ("C:", "D:", "E:", "F:"):
        candidates.append(Path(drive + "/STM32Cube/Repository"))
    for repo in candidates:
        if repo.exists():
            packs = sorted(p.name for p in repo.iterdir() if p.name.startswith("STM32Cube_FW_"))
            if packs:
                return repo, packs
    return None, []


def env_report(deep: bool = False) -> dict:
    cubemx = find_cubemx(deep=deep)
    uv4 = find_keil_uv4()
    repo, packs = find_repository()
    report = {
        "cubemx_exe": str(cubemx) if cubemx else None,
        "cubemx_dir": str(cubemx.parent) if cubemx else None,
        "cubemx_version": _cubemx_version(cubemx) if cubemx else None,
        "keil_uv4": str(uv4) if uv4 else None,
        "repository": str(repo) if repo else None,
        "fw_packs": packs,
        "work_dir": str(DEFAULT_WORK_DIR),
    }
    if cubemx:
        db = cubemx.parent / "db"
        report["db_found"] = db.is_dir()
        report["db_path"] = str(db) if db.is_dir() else None
    return report


def _cubemx_version(cubemx: Path):
    ps = (
        "(Get-Item -LiteralPath '" + _ps_quote(str(cubemx)) + "').VersionInfo | "
        "ForEach-Object { $_.FileVersion }"
    )
    out = _ps(ps).strip().splitlines()
    return out[0].strip() if out and out[0].strip() else None


# --------------------------------------------------------------------------
# CubeMX device database
# --------------------------------------------------------------------------

class CubeDb:
    """Read-only access to the CubeMX device database."""

    def __init__(self, cubemx_root: Path):
        self.root = Path(cubemx_root)
        self.db = self.root / "db"
        self.mcu_dir = self.db / "mcu"
        if not self.mcu_dir.is_dir():
            raise FileNotFoundError("no CubeMX device database under {}".format(self.db))

    def find_mcu(self, ref: str):
        exact = self.mcu_dir / (ref + ".xml")
        if exact.is_file():
            return exact
        lowered = ref.lower()
        for cand in sorted(self.mcu_dir.glob("STM32*.xml")):
            if lowered in expand_mcu_ref(cand.stem):
                return cand
        for cand in sorted(self.mcu_dir.glob("STM32*.xml")):
            if cand.stem.lower() == lowered:
                return cand
        for cand in sorted(self.mcu_dir.glob("STM32*.xml")):
            if lowered and lowered in cand.stem.lower():
                return cand
        return None

    def mcu_info(self, ref: str) -> dict:
        path = self.find_mcu(ref)
        if path is None:
            return {"error": "mcu not found", "query": ref}
        docs = list(_iter_xml_docs(path))
        if not docs:
            return {"error": "unparsable mcu xml", "path": str(path)}
        root = docs[0]
        ips = []
        for el in root:
            if _strip_ns(el.tag) == "IP":
                ips.append({
                    "name": el.get("Name"),
                    "instance": el.get("InstanceName"),
                    "version": el.get("Version"),
                    "config_file": el.get("ConfigFile"),
                    "clock_enable": el.get("ClockEnableMode"),
                })
        return {
            "path": str(path),
            "ref_name": root.get("RefName"),
            "family": root.get("Family"),
            "line": root.get("Line"),
            "package": root.get("Package"),
            "core": _child_text(root, "Core"),
            "die": _child_text(root, "Die"),
            "frequency_mhz": _child_text(root, "Frequency"),
            "ram_kb": _child_text(root, "Ram"),
            "flash_kb": [_child_text_el(e) for e in root if _strip_ns(e.tag) == "Flash"],
            "io_count": _child_text(root, "IONb"),
            "ips": ips,
            "ip_names": sorted({i["name"] for i in ips if i["name"]}),
        }

    def modes_files(self, ip: str):
        ip_dir = self.mcu_dir / "IP"
        if not ip_dir.is_dir():
            return []
        found = sorted(ip_dir.glob(ip + "*.xml"))
        generic = [f for f in found if f.stem.startswith(ip + "-v") or f.stem == ip]
        return generic or found

    def ip_modes(self, ip: str, brief: bool = True, name_filter: str = None) -> dict:
        """Modes (which map to virtual pins) and configurable parameters of an IP.

        brief mode returns just names, defaults and allowed values so the result
        stays small enough to read; full mode adds groups, tabs and comments.
        """
        pattern = re.compile(name_filter, re.IGNORECASE) if name_filter else None
        result = {"ip": ip, "files": [], "modes": [], "virtual_signals": [], "parameters": []}
        for path in self.modes_files(ip):
            result["files"].append(path.name)
            for doc in _iter_xml_docs(path):
                for el in doc.iter():
                    tag = _strip_ns(el.tag)
                    if tag == "RefMode":
                        if (el.get("Abstract") or "false").lower() == "true":
                            continue
                        if pattern and not pattern.search(el.get("Name") or ""):
                            continue
                        result["modes"].append({
                            "mode": el.get("Name"),
                            "base": el.get("BaseMode"),
                            "comment": el.get("Comment"),
                            "file": path.name,
                        })
                    elif tag == "RefSignal":
                        if (el.get("Virtual") or "").lower() == "true":
                            result["virtual_signals"].append(el.get("Name"))
                    elif tag == "Signal":
                        name = el.get("Name") or ""
                        if name.startswith("VS_"):
                            result["virtual_signals"].append(name)
                    elif tag == "RefParameter":
                        name = el.get("Name")
                        if pattern and not pattern.search(name or ""):
                            continue
                        entry = {
                            "name": name,
                            "default": el.get("DefaultValue"),
                            "type": el.get("Type"),
                            "values": [
                                pv.get("Value") for pv in el
                                if _strip_ns(pv.tag) == "PossibleValue"
                            ],
                        }
                        if not brief:
                            entry["group"] = el.get("Group")
                            entry["tab"] = el.get("TabName")
                            entry["comment"] = el.get("Comment")
                        result["parameters"].append(entry)
        result["virtual_signals"] = sorted(set(result["virtual_signals"]))
        seen = set()
        unique = []
        for mode in result["modes"]:
            if mode["mode"] not in seen:
                seen.add(mode["mode"])
                unique.append(mode)
        result["modes"] = unique
        deduped = []
        seen_params = set()
        for param in result["parameters"]:
            key = (param["name"], param["default"], param["type"])
            if key in seen_params:
                continue
            seen_params.add(key)
            deduped.append(param)
        result["parameters"] = deduped
        return result

    def ip_params(self, ip: str, family: str = "STM32G4", config_file: str = None,
                  brief: bool = True, name_filter: str = None) -> dict:
        pattern = re.compile(name_filter, re.IGNORECASE) if name_filter else None
        config_dir = self.mcu_dir / "config"
        if not config_dir.is_dir():
            return {"error": "no config dir", "ip": ip}
        name = config_file or ip
        candidates = []
        listed = config_dir / (name + "_Configs.xml")
        if listed.is_file():
            candidates.append(listed)
        for cand in sorted(config_dir.glob(ip + "-*_Configs.xml")):
            if cand not in candidates:
                candidates.append(cand)
        if not candidates:
            return {"error": "no config file for ip", "ip": ip}
        chosen = candidates[0]
        if family:
            for cand in candidates:
                if family[:7] in cand.name:
                    chosen = cand
                    break
        arguments = []
        ref_configs = []
        parameters = []
        for doc in _iter_xml_docs(chosen):
            for el in doc.iter():
                tag = _strip_ns(el.tag)
                if tag == "Argument":
                    name = el.get("Name")
                    if pattern and not pattern.search(name or ""):
                        continue
                    entry = {"name": name}
                    if not brief:
                        entry["comment"] = el.get("Comment")
                        entry["generic_type"] = el.get("GenericType")
                    arguments.append(entry)
                elif tag == "RefConfig":
                    ref_configs.append(el.get("Name"))
                elif tag == "RefParameter":
                    name = el.get("Name")
                    if pattern and not pattern.search(name or ""):
                        continue
                    entry = {
                        "name": name,
                        "default": el.get("DefaultValue"),
                        "type": el.get("Type"),
                        "values": [
                            pv.get("Value") for pv in el
                            if _strip_ns(pv.tag) == "PossibleValue"
                        ],
                    }
                    if not brief:
                        entry["group"] = el.get("Group")
                        entry["tab"] = el.get("TabName")
                        entry["comment"] = el.get("Comment")
                    parameters.append(entry)
        return {
            "ip": ip,
            "file": str(chosen),
            "file_candidates": [str(c) for c in candidates],
            "ref_configs": ref_configs,
            "arguments": arguments,
            "parameters": parameters,
        }

    def pin_signals(self, ref: str, filter_re: str = None) -> dict:
        """Which signals each pin can carry, straight from the device database.

        This is the pin solver's raw material: for a signal such as TIM1_CH3N it
        answers which of this package's pins can produce it.
        """
        path = self.find_mcu(ref)
        if path is None:
            return {"error": "mcu not found", "query": ref}
        pattern = re.compile(filter_re, re.IGNORECASE) if filter_re else None
        pin_to_signals = {}
        signal_to_pins = {}
        for doc in _iter_xml_docs(path):
            for el in doc.iter():
                if _strip_ns(el.tag) != "Pin":
                    continue
                pin = el.get("Name")
                if not pin:
                    continue
                signals = sorted({
                    child.get("Name") for child in el
                    if _strip_ns(child.tag) == "Signal" and child.get("Name")
                })
                pin_to_signals[pin] = signals
                for signal in signals:
                    signal_to_pins.setdefault(signal, []).append(pin)
        signal_to_pins = {k: sorted(v) for k, v in sorted(signal_to_pins.items())}
        if pattern:
            signal_to_pins = {k: v for k, v in signal_to_pins.items() if pattern.search(k)}
            pin_to_signals = {
                p: [s for s in sigs if pattern.search(s)]
                for p, sigs in pin_to_signals.items()
            }
            pin_to_signals = {p: s for p, s in pin_to_signals.items() if s}
        return {
            "mcu": ref,
            "path": str(path),
            "pin_count": len(pin_to_signals),
            "signal_count": len(signal_to_pins),
            "pin_to_signals": pin_to_signals,
            "signal_to_pins": signal_to_pins,
        }

    def ip_variants(self, ip: str) -> dict:
        """All known variants of a middleware IP, with the kernel versions they map to."""
        modes = self.ip_modes(ip)
        params = self.ip_params(ip)
        return {"ip": ip, "modes": modes, "params": params}


def _child_text(root, name):
    for el in root:
        if _strip_ns(el.tag) == name:
            return _child_text_el(el)
    return None


def _child_text_el(el):
    return (el.text or "").strip() or None


# --------------------------------------------------------------------------
# .ioc project file
# --------------------------------------------------------------------------

class Ioc:
    """A CubeMX .ioc file: flat key=value lines with escaped colons."""

    def __init__(self, path, load: bool = True):
        self.path = Path(path)
        self.lines = []
        if load:
            self.load()

    def load(self):
        self.lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()

    def entries(self):
        out = []
        for idx, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            out.append((idx, key.strip(), value))
        return out

    def as_dict(self):
        data = {}
        for _, key, value in self.entries():
            data.setdefault(key, value)
        return data

    def get(self, key, default=None):
        wanted = escape_ioc_key(key)
        for _, k, v in self.entries():
            if k == wanted:
                return v
        return default

    def set(self, key, value):
        self.set_many({key: value})

    def set_many(self, mapping):
        for key, value in mapping.items():
            key = escape_ioc_key(key)
            value = "" if value is None else str(value)
            replaced = False
            for idx, line in enumerate(self.lines):
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in line:
                    continue
                if line.partition("=")[0].strip() == key:
                    self.lines[idx] = "{}={}".format(key, value)
                    replaced = True
                    break
            if not replaced:
                self.lines.append("{}={}".format(key, value))

    def delete(self, key):
        wanted = escape_ioc_key(key)
        self.lines = [
            line for line in self.lines
            if not (("=" in line) and not line.strip().startswith("#")
                    and line.partition("=")[0].strip() == wanted)
        ]

    def save(self, path=None):
        target = Path(path) if path else self.path
        text = "\n".join(self.lines)
        if not text.endswith("\n"):
            text += "\n"
        _write_text(target, text)
        return target

    # ---- structured views -------------------------------------------------

    def ip_list(self):
        items = []
        for _, key, value in self.entries():
            match = re.fullmatch(r"Mcu\.IP(\d+)", key)
            if match:
                items.append((int(match.group(1)), value))
        items.sort()
        return [value for _, value in items]

    def pin_list(self):
        items = []
        for _, key, value in self.entries():
            match = re.fullmatch(r"Mcu\.Pin(\d+)", key)
            if match:
                items.append((int(match.group(1)), value))
        items.sort()
        return [value for _, value in items]

    def virtual_pins(self):
        pins = []
        for _, key, value in self.entries():
            match = re.fullmatch(r"(VP_[A-Za-z0-9_]+)\.Signal", key)
            if match:
                pins.append(value)
        return pins

    def summary(self):
        data = self.as_dict()
        gates = [(k, v) for k, v in data.items() if k.startswith("VP_") and k.endswith(".Mode")]
        pins = [v for v in self.pin_list() if not v.startswith("VP_")]
        return {
            "path": str(self.path),
            "mcu": data.get("Mcu.UserName") or data.get("Mcu.Name"),
            "family": data.get("Mcu.Family"),
            "package": data.get("Mcu.Package"),
            "ips": self.ip_list(),
            "virtual_modes": {k[:-5]: v for k, v in gates},
            "physical_pins": pins,
            "sysclk_source": data.get("RCC.SYSCLKSource"),
            "rtos": data.get("rtos.0.ip"),
            "project_name": data.get("ProjectManager.ProjectName"),
            "toolchain": data.get("ProjectManager.TargetToolchain"),
            "function_list": data.get("ProjectManager.functionlistsort"),
        }

    # ---- structural edits -------------------------------------------------

    def add_ip(self, ip, mode=None):
        """Enable an IP, optionally with a virtual mode, keeping Mcu.IP*/Mcu.Pin* contiguous."""
        changes = {}
        ips = self.ip_list()
        if ip not in ips:
            ips.append(ip)
            for idx, name in enumerate(ips):
                changes["Mcu.IP{}".format(idx)] = name
            changes["Mcu.IPNb"] = len(ips)
        if mode:
            pin = "VP_{}_VS_{}".format(ip, mode)
            pins = self.pin_list()
            if pin not in pins:
                pins.append(pin)
                for idx, name in enumerate(pins):
                    changes["Mcu.Pin{}".format(idx)] = name
                changes["Mcu.PinsNb"] = len(pins)
            changes[pin + ".Mode"] = mode
            changes[pin + ".Signal"] = "{}_VS_{}".format(ip, mode)
        self.set_many(changes)
        return changes

    def remove_ip(self, ip):
        changes = {}
        ips = [i for i in self.ip_list() if i != ip]
        for idx, name in enumerate(ips):
            changes["Mcu.IP{}".format(idx)] = name
        self.delete("Mcu.IP{}".format(len(ips)))
        changes["Mcu.IPNb"] = len(ips)

        pins = [p for p in self.pin_list() if not p.startswith("VP_{}_VS_".format(ip))]
        for idx, name in enumerate(pins):
            changes["Mcu.Pin{}".format(idx)] = name
        self.delete("Mcu.Pin{}".format(len(pins)))
        changes["Mcu.PinsNb"] = len(pins)

        for _, key, _ in list(self.entries()):
            if key.startswith("VP_{}_VS_".format(ip)):
                self.delete(key)
        if self.get("rtos.0.ip") == ip:
            self.delete("rtos.0.ip")
        self.set_many(changes)
        return changes

    def merge_from(self, reference, ips=(), pins=(), nvic=(), functionlist=True):
        """Transplant a peripheral configuration from a known-good .ioc.

        Hand-assembling every key of a complex peripheral is error prone. It is
        far more reliable to copy the block from a project that already works
        (an ST example, or your own earlier project) and let CubeMX validate it.
        """
        ref = reference if isinstance(reference, Ioc) else Ioc(reference)
        ref_ips = ref.ip_list()
        changes = {}
        for ip in ips:
            if ip in ref_ips:
                changes.update(self.add_ip(ip))
            for _, key, value in ref.entries():
                if key.startswith(ip + "."):
                    changes[key] = value
        current = self.pin_list()
        pins_added = False
        for pin in pins:
            pin_keys = [k for _, k, _ in ref.entries()
                        if k == pin or k.startswith(pin + ".")]
            if not pin_keys:
                continue
            if pin not in current:
                current.append(pin)
                pins_added = True
            for _, key, value in ref.entries():
                if key == pin or key.startswith(pin + "."):
                    changes[key] = value
        if pins_added:
            for idx, name in enumerate(current):
                changes["Mcu.Pin{}".format(idx)] = name
            changes["Mcu.PinsNb"] = len(current)
        for pattern in nvic:
            rx = re.compile(pattern)
            for _, key, value in ref.entries():
                if key.startswith("NVIC.") and rx.search(key):
                    changes[key] = value
        if functionlist:
            merged = merge_function_lists(
                self.get("ProjectManager.functionlistsort"),
                ref.get("ProjectManager.functionlistsort"),
            )
            if merged:
                changes["ProjectManager.functionlistsort"] = merged
        self.set_many(changes)
        return changes


def merge_function_lists(current, other):
    """Union two functionlistsort strings, keeping the current order first."""
    def _parse(text):
        items = []
        for part in (text or "").split(","):
            part = part.strip()
            if not part:
                continue
            fields = part.split("-")
            name = fields[1] if len(fields) > 1 else part
            items.append((name, fields))
        return items

    merged = []
    seen = set()
    for name, fields in _parse(current) + _parse(other):
        if name in seen:
            continue
        seen.add(name)
        merged.append((name, fields))
    out = []
    for idx, (_, fields) in enumerate(merged, start=1):
        if len(fields) > 1:
            fields = fields[1:]
        out.append("-".join([str(idx)] + fields))
    return ",".join(out)


# --------------------------------------------------------------------------
# CubeMX driven operations
# --------------------------------------------------------------------------

def resolve_ioc(project):
    path = Path(project)
    if path.is_file() and path.suffix == ".ioc":
        return path
    if path.is_dir():
        direct = sorted(path.glob("*.ioc"))
        if len(direct) == 1:
            return direct[0]
        found = sorted(path.rglob("*.ioc"))
        if found:
            return found[0]
    raise FileNotFoundError("no .ioc found at or under {}".format(project))


def gui_conflicts(ioc: Path):
    """Return CubeMX GUI processes that currently have this project open."""
    probe = "'{}','{}'".format(_ps_quote(ioc.name), _ps_quote(ioc.parent.name))
    ps = (
        "$names = @(" + probe + "); "
        "Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.ProcessName -match 'javaw|STM32CubeMX' -and $_.MainWindowTitle } | "
        "ForEach-Object { $t = $_.MainWindowTitle; "
        "$hit = $false; foreach ($n in $names) { if ($t -like ('*' + $n.replace('.ioc','') + '*')) { $hit = $true } }; "
        "if ($hit) { [pscustomobject]@{ pid = $_.Id; title = $t } } } | "
        "ConvertTo-Json -Compress"
    )
    out = _ps(ps).strip()
    if not out or out.startswith("ERROR"):
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return data


def snapshot_tree(project_root: Path, skip_dirs=(".git",)):
    state = {}
    for path in project_root.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.is_file():
            try:
                state[str(path)] = path.stat().st_mtime_ns
            except OSError:
                pass
    return state


def diff_snapshot(before: dict, after: dict):
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    return {"added": added, "removed": removed, "changed": changed}


def backup_project(project_root: Path, work_dir: Path, label: str, keep: int = 5):
    """Snapshot a project's sources and project files, skipping build artefacts."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(work_dir) / "backups" / "{}-{}".format(label, stamp)
    # The stamp has one second resolution; successive runs would otherwise
    # overwrite each other's snapshot.
    bump = 2
    while dest.exists():
        dest = Path(work_dir) / "backups" / "{}-{}-{}".format(label, stamp, bump)
        bump += 1
    skipped_locked = []
    stats = {"files": 0, "bytes": 0, "skipped_artifacts": 0}

    def _walk(src: Path, dst: Path):
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            if entry.is_dir():
                if entry.name in BACKUP_SKIP_DIRS:
                    continue
                _walk(entry, dst / entry.name)
                continue
            if entry.suffix.lower() in BACKUP_SKIP_SUFFIXES:
                stats["skipped_artifacts"] += 1
                continue
            try:
                shutil.copy2(entry, dst / entry.name)
                stats["files"] += 1
                stats["bytes"] += entry.stat().st_size
            except OSError:
                skipped_locked.append(str(entry))

    _walk(project_root, dest)
    return {
        "path": str(dest),
        "skipped_locked": skipped_locked,
        "files": stats["files"],
        "bytes": stats["bytes"],
        "skipped_artifacts": stats["skipped_artifacts"],
        "pruned": _prune_backups(Path(work_dir) / "backups", label, keep),
    }


def _prune_backups(backups_dir: Path, label: str, keep: int):
    """Keep only the newest N snapshots for this project."""
    if keep <= 0 or not backups_dir.is_dir():
        return []
    candidates = sorted(
        (p for p in backups_dir.iterdir()
         if p.is_dir() and p.name.startswith(label + "-")),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = []
    for old in candidates[keep:]:
        try:
            shutil.rmtree(str(old))
            removed.append(old.name)
        except OSError:
            pass
    return removed


def list_backups(work_dir: Path, label: str = None):
    backups_dir = Path(work_dir) / "backups"
    if not backups_dir.is_dir():
        return []
    return [
        path.name for path in sorted(backups_dir.iterdir(), reverse=True)
        if path.is_dir() and (not label or path.name.startswith(label + "-"))
    ]


def restore_backup(work_dir: Path, project_root: Path, name: str):
    """Copy a snapshot back over the project. Only ever runs when asked for."""
    source = Path(work_dir) / "backups" / name
    if not source.is_dir():
        return {"ok": False, "error": "no such backup", "name": name,
                "available": list_backups(work_dir)}
    done = {"files": 0, "bytes": 0}
    failed = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        target = Path(project_root) / path.relative_to(source)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            done["files"] += 1
            done["bytes"] += path.stat().st_size
        except OSError:
            failed.append(str(path))
    return {"ok": not failed, "from": str(source), "project": str(project_root),
            "restored": done, "failed": failed}


def project_family(ioc_path: Path) -> str:
    return _ioc_key_values(_read_text(ioc_path)).get("Mcu.Family", "")


def create_project(cubemx_exe: Path, out_dir, project_name, mcu,
                   toolchain=None, work_dir: Path = DEFAULT_WORK_DIR):
    """Create a project from nothing but an MCU name.

    The console can start from a bare device: load opens the MCU (the same step
    as picking a part in the GUI), project name and project path decide where it
    lands, and config save writes the .ioc. CubeMX then fills in the order code,
    database version and toolchain itself, so the result is a project it fully
    owns. Nothing needs to exist beforehand.
    """
    parent = Path(out_dir)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / project_name
    ioc_path = target / (project_name + ".ioc")
    forward = lambda p: str(p).replace("\\", "/")  # noqa: E731

    essential = [
        "load {}".format(mcu),
        "project name {}".format(project_name),
        "project path {}".format(forward(parent)),
    ]
    optional = []
    if toolchain:
        optional.append("project toolchain {}".format(toolchain))
    commands = essential + optional + ["config save {}".format(forward(ioc_path))]

    script = _write_script(work_dir, commands + ["exit"])
    run = _run_cubemx(cubemx_exe, script, work_dir, GENERATE_TIMEOUT)
    run["commands"] = commands
    written = ioc_path.is_file()
    run["ioc"] = str(ioc_path) if written else None
    run["warnings"] = list(run.get("warnings") or [])
    # A rejected optional command should not mask a project that was written.
    failed = [item["command"] for item in run.get("command_results", [])
              if item["result"] == "KO" and item["command"] in optional]
    if failed:
        run["warnings"].append("optional command rejected: " + "; ".join(failed))
    run["ok"] = (written and run.get("bye_bye") and not run.get("timed_out")
                 and not run.get("stalled")
                 and not [c for c in run.get("failed_commands", []) if c in essential
                          or c.endswith(ioc_path.name) and c.startswith("config save")])
    return run


def family_warnings(family: str):
    notes = []
    if "STM32H7" in family:
        notes.append("STM32H7 stalls CubeMX in headless mode on a power-supply dialog "
                     "it cannot display; expect a timeout. Other families are fine.")
    return notes


def _write_script(work_dir: Path, lines) -> Path:
    scripts = Path(work_dir) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / "cubemx-{}.txt".format(time.strftime("%Y%m%d-%H%M%S"))
    _write_text(path, "\n".join(lines) + "\n")
    return path


def _run_cubemx(cubemx_exe: Path, script: Path, work_dir: Path, timeout: int):
    """Run CubeMX in script mode and wait for it to really finish.

    STM32CubeMX.exe is only a launcher: it starts a JVM and returns at once, so
    simply waiting for the launcher is not enough. The JVM inherits our stdout
    pipe, which usually keeps it open until the run is over; as a second net we
    also watch for newly spawned JVM processes and wait for them to disappear.
    """
    logs = Path(work_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "cubemx-{}.log".format(time.strftime("%Y%m%d-%H%M%S"))
    started = time.time()
    timed_out = False
    stalled = False
    before = _java_pids()
    with open(log, "wb") as handle:
        try:
            proc = subprocess.Popen(
                [str(cubemx_exe), "-q", str(script)],
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            handle.write("ERROR: {}".format(exc).encode("utf-8", "replace"))
            proc = None
        if proc is not None:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()

        deadline = started + timeout
        last_size = -1
        last_change = time.time()
        while time.time() < deadline:
            spawned = _java_pids() - before
            if not spawned:
                break
            try:
                size = log.stat().st_size
            except OSError:
                size = 0
            if size != last_size:
                last_size = size
                last_change = time.time()
            elif time.time() - last_change > STALL_TIMEOUT:
                stalled = True
                _kill_pids(spawned)
                break
            time.sleep(3)
        else:
            timed_out = True
            _kill_pids(_java_pids() - before)
    text = log.read_text(encoding="utf-8", errors="replace")
    problems = [
        line.strip() for line in text.splitlines()
        if "OptionalMessage_ERROR" in line or re.search(r"\[ERROR\]", line)
    ]
    return {
        "log": str(log),
        "seconds": round(time.time() - started, 1),
        "timed_out": timed_out,
        "stalled": stalled,
        "bye_bye": "Bye bye" in text,
        "problems": problems[:20],
        "command_results": _command_results(text),
        "failed_commands": [
            item["command"] for item in _command_results(text) if item["result"] == "KO"
        ],
        "tail": text.strip().splitlines()[-12:],
    }


def _command_results(text: str) -> list:
    """Pair each echoed console command with the OK / KO verdict that follows it."""
    results = []
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _LOG_LINE.match(line):
            continue
        if not _is_console_command(line):
            continue
        if line in ("OK", "KO"):
            if pending is not None:
                results.append({"command": pending, "result": line})
                pending = None
            continue
        if line.startswith(_NOISE_PREFIXES) or line == "exit":
            continue
        pending = line
    if pending is not None:
        results.append({"command": pending, "result": "no verdict"})
    return results


def _is_console_command(line: str) -> bool:
    if line in ("OK", "KO"):
        return True
    if line.startswith(("\t", "at ", "...")) or ".java:" in line:
        return False
    return line.split(None, 1)[0].lower() in _CONSOLE_VERBS


def _java_pids():
    out = _ps("Get-Process javaw,java -ErrorAction SilentlyContinue | "
              "Select-Object -ExpandProperty Id")
    return {int(x) for x in out.split() if x.strip().isdigit()}


def _kill_pids(pids):
    for pid in sorted(pids):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass


def normalize_ioc(cubemx_exe: Path, ioc: Path, work_dir: Path, timeout: int = GENERATE_TIMEOUT):
    """Let CubeMX load and re-save a .ioc, canonicalising it and validating hand edits."""
    run = run_console(cubemx_exe, ioc, [], work_dir, timeout=timeout)
    diff = run["ioc_diff"]
    run["changed"] = bool(diff["dropped"] or diff["added"] or diff["changed"])
    return run


def _ioc_key_values(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value
    return out


def diff_ioc_keys(before: str, after: str) -> dict:
    """Which keys CubeMX dropped, added or rewrote when it re-saved the .ioc."""
    old = _ioc_key_values(before)
    new = _ioc_key_values(after)
    return {
        "dropped": sorted(set(old) - set(new)),
        "added": sorted(set(new) - set(old)),
        "changed": sorted(k for k in set(old) & set(new) if old[k] != new[k]),
    }


CONSOLE_HELP = """CubeMX console commands usable between load and save:

  set mode <Periph> <Mode>              enable a peripheral; CubeMX assigns the pins.
                                        Quote modes that contain spaces, e.g.
                                        set mode TIM1 "PWM Generation1 CH1 CH1N"
  set pin <Pin> <SIGNAL>                map or relocate a signal, e.g. set pin PD5 USART2_TX
  set ip parameters <IP> <Param> <Val>  set a parameter. CubeMX does not validate the value,
                                        so take it from the device database.
  set gpio parameters <Pin> <Param> <Val>  GPIO_Label, GPIO_PuPd, GPIO_Speed, PinState
  clock resolution                      resolve the clock tree after changing RCC settings

Look up exact mode strings with ip-modes and parameter values with ip-params.
"""


def run_console(cubemx_exe: Path, project, commands, work_dir: Path,
                load: bool = True, save: bool = True, timeout: int = GENERATE_TIMEOUT):
    """Run raw CubeMX console commands against a project, then report what changed.

    This is the lever that actually configures peripherals: the console drives
    CubeMX's own model, so pin assignment and validation are CubeMX's job.
    """
    ioc = resolve_ioc(project)
    before = _read_text(ioc)
    lines = []
    if load:
        lines.append("config load {}".format(str(ioc).replace("\\", "/")))
    lines.extend(commands)
    if save:
        lines.append("config save {}".format(str(ioc).replace("\\", "/")))
    lines.append("exit")
    script = _write_script(work_dir, lines)
    run = _run_cubemx(cubemx_exe, script, work_dir, timeout)
    after = _read_text(ioc)
    run["ioc"] = str(ioc)
    run["commands"] = lines
    run["ioc_diff"] = diff_ioc_keys(before, after)
    run["cubeMX_errors"] = extract_cubemx_errors(run, run.get("log"))
    run["warnings"] = family_warnings(project_family(ioc))
    run["ok"] = _run_succeeded(run)
    return run


def verify(project, expect=()):
    """Report a project's pin map and flag the gaps that actually matter.

    Enabling a peripheral can leave a channel unassigned (a three-phase request
    once left CH3N off), so checking the result beats assuming it worked.
    """
    ioc = Ioc(resolve_ioc(project))
    slots = ioc.pin_list()
    physical = [p for p in slots if not p.startswith("VP_")]
    virtual = [p for p in slots if p.startswith("VP_")]

    fields = {}
    for _, key, value in ioc.entries():
        if "." not in key:
            continue
        head, _, attr = key.rpartition(".")
        if attr in ("Mode", "Signal", "GPIO_Label", "Locked"):
            fields.setdefault(head, {})[attr] = value

    signals = {}
    for pin, attrs in fields.items():
        signal = attrs.get("Signal")
        if signal:
            signals.setdefault(signal, []).append(pin)

    duplicates = sorted({p for p in slots if slots.count(p) > 1})
    unlisted = sorted(p for p in fields
                      if fields[p].get("Signal") and p not in slots)
    missing = [s for s in expect if s not in signals]
    return {
        "ioc": str(ioc.path),
        "mcu": ioc.summary().get("mcu"),
        "ips": ioc.ip_list(),
        "physical_pins": physical,
        "virtual_pins": virtual,
        "pin_map": {p: fields.get(p, {}) for p in physical},
        "signals": signals,
        "duplicate_pin_slots": duplicates,
        "signals_on_unlisted_pins": unlisted,
        "expected_signals_missing": missing,
        "ok": not duplicates and not unlisted and not missing,
    }


def plan_pins(project, wants, db, allow_used: bool = False):
    """Pick a free pin for each wanted signal and return the set pin commands.

    Enabling a peripheral can leave a channel unassigned because CubeMX could not
    place it; this works out where it can go and hands back the commands to do it.
    """
    ioc = Ioc(resolve_ioc(project))
    mcu = ioc.get("Mcu.Name") or ioc.get("Mcu.UserName") or ""
    table = db.pin_signals(mcu)
    if "error" in table:
        return {"ok": False, "error": table["error"], "query": mcu}

    used = set()
    assigned = {}
    for _, key, value in ioc.entries():
        if "." not in key:
            continue
        head, _, attr = key.rpartition(".")
        if attr != "Signal":
            continue
        assigned[value] = head
        if head and not head.startswith("VP_"):
            used.add(head)
    used_before = sorted(used)

    plan = []
    for signal in wants:
        if signal in assigned:
            plan.append({"signal": signal, "status": "already assigned",
                         "pin": assigned[signal], "commands": []})
            continue
        candidates = table["signal_to_pins"].get(signal, [])
        if not candidates:
            plan.append({"signal": signal, "status": "not available on this package",
                         "pin": None, "candidates": [], "commands": []})
            continue
        free = [p for p in candidates if p not in used]
        pool = list(candidates) if allow_used else free
        if not pool:
            plan.append({"signal": signal, "status": "no free pin left",
                         "pin": None, "candidates": candidates, "commands": []})
            continue
        chosen = pool[0]
        used.add(chosen)
        plan.append({"signal": signal, "status": "assign", "pin": chosen,
                     "candidates": candidates,
                     "commands": ["set pin {} {}".format(chosen, signal)]})

    commands = [c for item in plan for c in item["commands"]]
    unresolved = [item["signal"] for item in plan
                  if item["status"] not in ("assign", "already assigned")]
    return {
        "ioc": str(ioc.path),
        "mcu": mcu,
        "used_pins_before": used_before,
        "used_pins_after": sorted(used),
        "plan": plan,
        "commands": commands,
        "unresolved": unresolved,
        "ok": not unresolved,
    }


IOC_HEADER = "#MicroXplorer Configuration settings - do not modify"

# Keys a skeletal project needs: MCU identity, clock, NVIC and project settings.
SKELETON_PREFIXES = ("Mcu.", "RCC.", "NVIC.", "ProjectManager.", "MxCube.", "MxDb.", "board")

CANNOT_SYNTHESIZE = {
    "ok": False,
    "error": "a new project needs a reference .ioc",
    "why": ("CubeMX rejects partial projects. Synthesised from the device database it "
            "leaves Mcu.getDie() null (no order-code index exists in the database) and "
            "project generate throws NullPointerException; stripped down to a skeleton "
            "the session hangs on a dialog script mode cannot display."),
    "do": "pass --from <reference.ioc>; the whole project is copied, so any working project works",
}


def new_project(reference, out_dir, project_name, toolchain=None):
    """Start a new project by copying a complete .ioc.

    CubeMX will not accept a partial project: one synthesised from the device
    database leaves Mcu.getDie() null (the database has no order-code index) and
    project generate throws, while one stripped down to a skeleton makes the
    session hang on a dialog script mode cannot show. A whole, valid .ioc copies
    cleanly, which is also why the projects that offer a from-scratch command
    ship curated templates rather than deriving one.
    """
    src = Ioc(resolve_ioc(reference))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / (project_name + ".ioc")
    src.set("ProjectManager.ProjectName", project_name)
    if toolchain:
        src.set("ProjectManager.TargetToolchain", toolchain)
    src.save(target)
    return {
        "ok": True,
        "ioc": str(target),
        "from": str(src.path),
        "mcu": src.get("Mcu.Name") or src.get("Mcu.UserName"),
        "note": ("the copy keeps the reference's peripheral configuration; "
                 "run generate to build the tree, then script to change it"),
    }

def extract_cubemx_errors(run: dict, log_path: Path = None) -> list:
    """Pull CubeMX's own complaints out of a run: invalid IP parameters and the like."""
    messages = []
    text = ""
    if log_path and Path(log_path).exists():
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        marker = "(OptionalMessage_ERROR)"
        if marker in line:
            messages.append(line.split(marker, 1)[1].strip())
    return messages


def generate(cubemx_exe: Path, project, work_dir: Path, force: bool = False,
             drop_groups=None, drop_include=None, timeout: int = GENERATE_TIMEOUT):
    """Regenerate a project headlessly, with a GUI guard, a backup and a change report."""
    ioc = resolve_ioc(project)
    project_root = ioc.parent
    conflicts = gui_conflicts(ioc)
    if conflicts and not force:
        return {
            "ok": False,
            "blocked": "cubeMX_gui_has_project_open",
            "detail": ("Close STM32CubeMX (or use force) first: its in-memory copy of "
                       "the .ioc would overwrite the regenerated one."),
            "conflicts": conflicts,
        }

    before = snapshot_tree(project_root)
    backup = backup_project(project_root, work_dir, project_root.name)
    script = _write_script(work_dir, [
        "config load {}".format(str(ioc).replace("\\", "/")),
        "project generate",
        "exit",
    ])
    run = _run_cubemx(cubemx_exe, script, work_dir, timeout)
    after = snapshot_tree(project_root)
    changes = diff_snapshot(before, after)

    cleaned = None
    if drop_groups:
        uvprojx = sorted(project_root.rglob("*.uvprojx"))
        if uvprojx:
            cleaned = strip_uvprojx_groups(uvprojx[0], drop_groups, drop_include or [])

    return {
        "ok": _run_succeeded(run),
        "ioc": str(ioc),
        "backup": backup,
        "run": run,
        "changes": changes,
        "cleaned_uvprojx": cleaned,
        "warnings": family_warnings(project_family(ioc)),
    }


def _run_succeeded(run: dict) -> bool:
    """A CubeMX run only counts as successful if nothing said KO.

    CubeMX ends with "Bye bye" even after a command throws, so the verdicts and
    the captured problems have to be part of the judgement.
    """
    if run.get("timed_out") or not run.get("bye_bye"):
        return False
    if run.get("stalled"):
        return False
    if run.get("failed_commands"):
        return False
    return True


def strip_uvprojx_groups(uvprojx: Path, group_names, include_entries=()):
    """Remove stale hand-added file groups from a Keil project file.

    CubeMX regenerates the project file but keeps groups it does not manage, so
    a previously hand-added kernel or driver folder keeps getting compiled and
    can shadow the CubeMX-managed sources.
    """
    uvprojx = Path(uvprojx)
    backups = uvprojx.with_suffix(".uvprojx.bak")
    shutil.copy2(uvprojx, backups)

    lines = uvprojx.read_text(encoding="utf-8", errors="replace").splitlines()
    wanted = set(group_names)
    out = []
    idx = 0
    removed = []
    while idx < len(lines):
        line = lines[idx]
        if line.strip().startswith("<Group>"):
            end = idx
            depth = 0
            while end < len(lines):
                stripped = lines[end].strip()
                if stripped.startswith("<Group>"):
                    depth += 1
                elif stripped.startswith("</Group>"):
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            block = lines[idx:end + 1]
            name = None
            for entry in block:
                match = re.search(r"<GroupName>(.*?)</GroupName>", entry)
                if match:
                    name = match.group(1)
                    break
            if name in wanted:
                removed.append(name)
                idx = end + 1
                continue
            out.extend(block)
            idx = end + 1
            continue
        out.append(line)
        idx += 1

    text = "\n".join(out)
    for entry in include_entries:
        text = text.replace(entry, "")
    _write_text(uvprojx, text)
    return {"uvprojx": str(uvprojx), "backup": str(backups),
            "removed_groups": removed, "include_entries_dropped": list(include_entries)}


def build(uv4_exe: Path, project, work_dir: Path, timeout: int = BUILD_TIMEOUT):
    """Build with Keil UV4 and parse the result."""
    target = Path(project)
    if target.is_dir():
        found = sorted(target.rglob("*.uvprojx"))
        if not found:
            raise FileNotFoundError("no .uvprojx under {}".format(project))
        target = found[0]
    logs = Path(work_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "build-{}.log".format(time.strftime("%Y%m%d-%H%M%S"))
    timed_out = False
    try:
        subprocess.run(
            [str(uv4_exe), "-b", str(target), "-j0", "-o", str(log)],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    errors = warnings = None
    for match in re.finditer(r"(\d+)\s+Error\(s\),\s+(\d+)\s+Warning\(s\)", text):
        errors, warnings = int(match.group(1)), int(match.group(2))
    diagnostics = [
        line.strip() for line in text.splitlines()
        if re.search(r"\b(error|warning):", line) or "Error:" in line or ": Error" in line
    ]
    return {
        "ok": errors == 0 and not timed_out,
        "uvprojx": str(target),
        "log": str(log),
        "errors": errors,
        "warnings": warnings,
        "timed_out": timed_out,
        "diagnostics": diagnostics[:40],
        "summary": text.strip().splitlines()[-1] if text.strip() else None,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _db_from_env(deep=False):
    cubemx = find_cubemx(deep=deep)
    if not cubemx:
        raise SystemExit("STM32CubeMX not found; set STM32CUBEMX to the exe path")
    return CubeDb(cubemx.parent), cubemx


def main(argv=None):
    parser = argparse.ArgumentParser(description="STM32CubeMX control toolkit")
    parser.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    p_env = sub.add_parser("env", help="locate CubeMX, Keil and firmware packs")
    p_env.add_argument("--deep", action="store_true")

    p_new = sub.add_parser("new", help="seed a new project from an existing .ioc of the same part")
    p_new.add_argument("name")
    p_new.add_argument("out_dir")
    p_new.add_argument("--from", dest="reference",
                       help="reference .ioc written by CubeMX for the same part")
    p_new.add_argument("--toolchain")
    p_new.add_argument("--no-normalize", action="store_true")

    p_create = sub.add_parser("create", help="create a project from nothing but an MCU name")
    p_create.add_argument("mcu")
    p_create.add_argument("name")
    p_create.add_argument("out_dir")
    p_create.add_argument("--toolchain", default="MDK-ARM V5.32")
    p_create.add_argument("--generate", action="store_true", help="also generate the code")

    p_mcu = sub.add_parser("mcu", help="describe an MCU from the CubeMX database")
    p_mcu.add_argument("ref")
    p_mcu.add_argument("--deep", action="store_true")

    p_modes = sub.add_parser("ip-modes", help="modes, virtual pins and parameters for an IP")
    p_modes.add_argument("ip")
    p_modes.add_argument("--deep", action="store_true")
    p_modes.add_argument("--full", action="store_true", help="include groups, tabs and comments")
    p_modes.add_argument("--filter", help="regex on parameter name")

    p_params = sub.add_parser("ip-params", help="family level parameters for an IP")
    p_params.add_argument("ip")
    p_params.add_argument("--family", default="STM32G4")
    p_params.add_argument("--config-file")
    p_params.add_argument("--deep", action="store_true")
    p_params.add_argument("--full", action="store_true", help="include comments")
    p_params.add_argument("--filter", help="regex on parameter name")

    p_show = sub.add_parser("show", help="summarise a .ioc")
    p_show.add_argument("project")

    p_verify = sub.add_parser("verify", help="report the pin map and flag gaps")
    p_verify.add_argument("project")
    p_verify.add_argument("--expect", action="append", default=[],
                          help="signal that must be present, e.g. TIM1_CH3N")

    p_pinmap = sub.add_parser("pin-map", help="which pins can carry which signals")
    p_pinmap.add_argument("mcu")
    p_pinmap.add_argument("--filter", help="regex on signal name, e.g. TIM1_CH")
    p_pinmap.add_argument("--by-pin", action="store_true", help="also list pin -> signals")

    p_plan = sub.add_parser("pin-plan", help="suggest a free pin for wanted signals")
    p_plan.add_argument("project")
    p_plan.add_argument("--want", action="append", default=[],
                        help="signal to place, e.g. TIM1_CH3N; repeatable")
    p_plan.add_argument("--allow-used", action="store_true",
                        help="consider pins that are already taken")
    p_plan.add_argument("--apply", action="store_true",
                        help="run the suggested set pin commands through CubeMX")

    p_bak = sub.add_parser("backups", help="list snapshots taken before generation")
    p_bak.add_argument("--label")

    p_restore = sub.add_parser("restore", help="copy a snapshot back over the project")
    p_restore.add_argument("project")
    p_restore.add_argument("backup", help="snapshot name from the backups command")

    p_set = sub.add_parser("set", help="set .ioc keys (key=value ...)")
    p_set.add_argument("project")
    p_set.add_argument("assignments", nargs="+")

    p_add = sub.add_parser("enable-ip", help="enable an IP, optionally in a mode")
    p_add.add_argument("project")
    p_add.add_argument("ip")
    p_add.add_argument("--mode")

    p_rm = sub.add_parser("disable-ip", help="disable an IP")
    p_rm.add_argument("project")
    p_rm.add_argument("ip")

    p_merge = sub.add_parser("merge", help="transplant a peripheral config from another .ioc")
    p_merge.add_argument("project")
    p_merge.add_argument("reference")
    p_merge.add_argument("--ip", action="append", default=[])
    p_merge.add_argument("--pin", action="append", default=[])
    p_merge.add_argument("--nvic", action="append", default=[],
                         help="regex matched against NVIC.* key names in the reference")
    p_merge.add_argument("--no-functionlist", action="store_true")

    p_norm = sub.add_parser("normalize", help="let CubeMX canonicalise and validate a .ioc")
    p_norm.add_argument("project")

    p_script = sub.add_parser("script", help="run raw CubeMX console commands (set mode, set pin, ...)")
    p_script.add_argument("project", nargs="?")
    p_script.add_argument("--cmd", action="append", default=[],
                          help="console command; repeat for several, run between load and save")
    p_script.add_argument("--file", help="read console commands from a file, one per line")
    p_script.add_argument("--no-load", action="store_true")
    p_script.add_argument("--no-save", action="store_true")
    p_script.add_argument("--help-console", action="store_true",
                          help="print the console command vocabulary")

    p_gen = sub.add_parser("generate", help="regenerate code headlessly")
    p_gen.add_argument("project")
    p_gen.add_argument("--force", action="store_true")
    p_gen.add_argument("--drop-group", action="append", default=[])
    p_gen.add_argument("--drop-include", action="append", default=[])

    p_build = sub.add_parser("build", help="build a Keil project")
    p_build.add_argument("project")

    args = parser.parse_args(argv)
    work_dir = Path(args.work_dir)

    if args.command == "env":
        _print(env_report(deep=args.deep))
        return 0

    if args.command == "new":
        if not args.reference:
            _print(CANNOT_SYNTHESIZE)
            return 2
        result = new_project(args.reference, args.out_dir, args.name,
                             toolchain=args.toolchain)
        if result.get("ok") and not args.no_normalize:
            cubemx = find_cubemx()
            if cubemx:
                result["normalize"] = normalize_ioc(cubemx, Path(result["ioc"]), work_dir)
        _print(result)
        return 0 if result.get("ok") else 2

    if args.command == "create":
        cubemx = find_cubemx()
        if not cubemx:
            raise SystemExit("STM32CubeMX not found")
        result = create_project(cubemx, args.out_dir, args.name, args.mcu,
                                toolchain=args.toolchain, work_dir=work_dir)
        if result.get("ok") and args.generate:
            result["generate"] = generate(cubemx, Path(result["ioc"]).parent, work_dir)
        _print(result)
        return 0 if result.get("ok") else 2

    if args.command == "mcu":
        db, _ = _db_from_env(args.deep)
        _print(db.mcu_info(args.ref))
        return 0

    if args.command == "ip-modes":
        db, _ = _db_from_env(args.deep)
        _print(db.ip_modes(args.ip, brief=not args.full, name_filter=args.filter))
        return 0

    if args.command == "ip-params":
        db, _ = _db_from_env(args.deep)
        _print(db.ip_params(args.ip, family=args.family, config_file=args.config_file,
                            brief=not args.full, name_filter=args.filter))
        return 0

    if args.command == "show":
        _print(Ioc(resolve_ioc(args.project)).summary())
        return 0

    if args.command == "verify":
        result = verify(args.project, expect=args.expect)
        _print(result)
        return 0 if result["ok"] else 2

    if args.command == "pin-map":
        db, _ = _db_from_env()
        table = db.pin_signals(args.mcu, filter_re=args.filter)
        if not args.by_pin:
            table.pop("pin_to_signals", None)
        _print(table)
        return 0

    if args.command == "pin-plan":
        db, cubemx = _db_from_env()
        result = plan_pins(args.project, args.want, db, allow_used=args.allow_used)
        if args.apply and result.get("commands"):
            result["applied"] = run_console(cubemx, args.project, result["commands"], work_dir)
        _print(result)
        return 0 if result.get("ok") else 2

    if args.command == "backups":
        _print({"work_dir": str(work_dir), "backups": list_backups(work_dir, args.label)})
        return 0

    if args.command == "restore":
        ioc = resolve_ioc(args.project)
        _print(restore_backup(work_dir, ioc.parent, args.backup))
        return 0

    if args.command == "set":
        ioc = Ioc(resolve_ioc(args.project))
        mapping = {}
        for item in args.assignments:
            key, _, value = item.partition("=")
            mapping[key] = value
        ioc.set_many(mapping)
        ioc.save()
        _print({"ok": True, "path": str(ioc.path), "set": mapping})
        return 0

    if args.command == "enable-ip":
        ioc = Ioc(resolve_ioc(args.project))
        _print({
            "ok": True,
            "changes": ioc.add_ip(args.ip, args.mode),
            "path": str(ioc.path),
            "warning": ("CubeMX drops hand-written IP entries on reload; prefer "
                        "script --cmd 'set mode {} <MODE>' which lets CubeMX enable "
                        "the peripheral and assign pins.".format(args.ip)),
        })
        ioc.save()
        return 0

    if args.command == "disable-ip":
        ioc = Ioc(resolve_ioc(args.project))
        _print({"ok": True, "changes": ioc.remove_ip(args.ip), "path": str(ioc.path)})
        ioc.save()
        return 0

    if args.command == "merge":
        ioc = Ioc(resolve_ioc(args.project))
        changes = ioc.merge_from(args.reference, ips=args.ip, pins=args.pin,
                                 nvic=args.nvic,
                                 functionlist=not args.no_functionlist)
        ioc.save()
        _print({"ok": True, "path": str(ioc.path), "keys_written": len(changes)})
        return 0

    if args.command == "normalize":
        cubemx = find_cubemx()
        if not cubemx:
            raise SystemExit("STM32CubeMX not found")
        _print(normalize_ioc(cubemx, resolve_ioc(args.project), work_dir))
        return 0

    if args.command == "script":
        if args.help_console:
            print(CONSOLE_HELP)
            return 0
        if not args.project:
            raise SystemExit("a project path or .ioc file is required")
        cubemx = find_cubemx()
        if not cubemx:
            raise SystemExit("STM32CubeMX not found")
        commands = list(args.cmd)
        if args.file:
            for line in _read_text(Path(args.file)).splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    commands.append(stripped)
        _print(run_console(cubemx, args.project, commands, work_dir,
                           load=not args.no_load, save=not args.no_save))
        return 0

    if args.command == "generate":
        cubemx = find_cubemx()
        if not cubemx:
            raise SystemExit("STM32CubeMX not found")
        _print(generate(cubemx, args.project, work_dir, force=args.force,
                        drop_groups=args.drop_group, drop_include=args.drop_include))
        return 0

    if args.command == "build":
        uv4 = find_keil_uv4()
        if not uv4:
            raise SystemExit("Keil UV4.exe not found")
        _print(build(uv4, args.project, work_dir))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
