#!/usr/bin/env python3
"""MCP stdio server exposing the CubeMX toolkit as tools.

Dependency free on purpose: MCP over stdio is newline-delimited JSON-RPC 2.0, so
the server is a read loop over stdin. Register it through the plugin's .mcp.json.

Every tool returns the same JSON the command line prints, so an agent sees
exactly what a human would.
"""

import json
import sys
from pathlib import Path

# MCP is UTF-8 on the wire. Without this, a Windows console codepage (cp936 here)
# mangles non-ASCII project paths on the way in and out.
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cubemx  # noqa: E402

SERVER_NAME = "stm32cubemx-control"
SERVER_VERSION = "0.4.0"
DEFAULT_PROTOCOL = "2025-06-18"
WORK_DIR = cubemx.DEFAULT_WORK_DIR

PROJECT = {"type": "string",
           "description": "project directory or a .ioc file inside it"}


def _obj(props, required=()):
    return {"type": "object", "properties": props,
            "required": list(required), "additionalProperties": False}


def _needs_cubemx():
    exe = cubemx.find_cubemx()
    if not exe:
        raise RuntimeError("STM32CubeMX not found; set STM32CUBEMX to its exe path")
    return exe


def _db():
    exe = _needs_cubemx()
    return cubemx.CubeDb(exe.parent)


# ---------------------------------------------------------------- tool bodies

def t_env(args):
    return cubemx.env_report(deep=bool(args.get("deep")))


def t_mcu(args):
    return _db().mcu_info(args["ref"])


def t_ip_modes(args):
    return _db().ip_modes(args["ip"], brief=not args.get("full"),
                          name_filter=args.get("filter"))


def t_ip_params(args):
    return _db().ip_params(args["ip"], family=args.get("family", "STM32G4"),
                           brief=not args.get("full"), name_filter=args.get("filter"))


def t_pin_map(args):
    return _db().pin_signals(args["mcu"], filter_re=args.get("filter"))


def t_show(args):
    return cubemx.Ioc(cubemx.resolve_ioc(args["project"])).summary()


def t_verify(args):
    return cubemx.verify(args["project"], expect=args.get("expect") or [])


def t_pin_plan(args):
    result = cubemx.plan_pins(args["project"], args.get("want") or [], _db(),
                              allow_used=bool(args.get("allow_used")))
    if args.get("apply") and result.get("commands"):
        result["applied"] = cubemx.run_console(
            _needs_cubemx(), args["project"], result["commands"], WORK_DIR)
    return result


def t_script(args):
    return cubemx.run_console(_needs_cubemx(), args["project"],
                              args.get("commands") or [], WORK_DIR,
                              load=not args.get("no_load"),
                              save=not args.get("no_save"))


def t_new(args):
    return cubemx.new_project(args["reference"], args["out_dir"], args["name"])


def t_generate(args):
    return cubemx.generate(_needs_cubemx(), args["project"], WORK_DIR,
                           force=bool(args.get("force")),
                           drop_groups=args.get("drop_group") or [],
                           drop_include=args.get("drop_include") or [])


def t_build(args):
    uv4 = cubemx.find_keil_uv4()
    if not uv4:
        raise RuntimeError("Keil UV4.exe not found")
    return cubemx.build(uv4, args["project"], WORK_DIR)


def t_backups(args):
    return {"work_dir": str(WORK_DIR),
            "backups": cubemx.list_backups(WORK_DIR, args.get("label"))}


def t_restore(args):
    project = cubemx.resolve_ioc(args["project"]).parent
    return cubemx.restore_backup(WORK_DIR, project, args["backup"])


# ------------------------------------------------------------------- registry

TOOLS = [
    ("cubemx_env", "Locate STM32CubeMX, Keil UV4, firmware packs and the device database.",
     _obj({"deep": {"type": "boolean", "description": "also scan drives (slow)"}}), t_env),
    ("cubemx_mcu", "Describe an MCU from CubeMX's own database: IPs, memory, package.",
     _obj({"ref": {"type": "string", "description": "e.g. STM32G431CBUx"}}, ["ref"]), t_mcu),
    ("cubemx_ip_modes",
     "Modes (which become virtual pins) and configurable parameters of a peripheral.",
     _obj({"ip": {"type": "string"},
           "filter": {"type": "string", "description": "regex on names"},
           "full": {"type": "boolean"}}, ["ip"]), t_ip_modes),
    ("cubemx_ip_params", "Family level parameters of a peripheral with allowed values.",
     _obj({"ip": {"type": "string"}, "family": {"type": "string"},
           "filter": {"type": "string"}, "full": {"type": "boolean"}}, ["ip"]), t_ip_params),
    ("cubemx_pin_map", "Which pins of an MCU can carry a given signal.",
     _obj({"mcu": {"type": "string"}, "filter": {"type": "string",
           "description": "regex on signal name, e.g. TIM1_CH"}}, ["mcu"]), t_pin_map),
    ("cubemx_show", "Summarise a project: MCU, enabled IPs, virtual modes, pins.",
     _obj({"project": PROJECT}, ["project"]), t_show),
    ("cubemx_verify",
     "Report the pin map and flag gaps: duplicate pins, unlisted pins, missing expected signals.",
     _obj({"project": PROJECT,
           "expect": {"type": "array", "items": {"type": "string"},
                      "description": "signals that must be present"}}, ["project"]), t_verify),
    ("cubemx_pin_plan",
     "Choose a free pin for each wanted signal; optionally apply it through CubeMX.",
     _obj({"project": PROJECT,
           "want": {"type": "array", "items": {"type": "string"}},
           "allow_used": {"type": "boolean"}, "apply": {"type": "boolean"}},
          ["project"]), t_pin_plan),
    ("cubemx_script",
     "Run raw CubeMX console commands (set mode / set pin / set ip parameters / clock "
     "resolution / project generate) between config load and config save.",
     _obj({"project": PROJECT,
           "commands": {"type": "array", "items": {"type": "string"}},
           "no_load": {"type": "boolean"}, "no_save": {"type": "boolean"}},
          ["project", "commands"]), t_script),
    ("cubemx_new",
     "Start a new project by copying a complete reference .ioc (partial projects are rejected by CubeMX).",
     _obj({"reference": {"type": "string"}, "out_dir": {"type": "string"},
           "name": {"type": "string"}}, ["reference", "out_dir", "name"]), t_new),
    ("cubemx_generate",
     "Regenerate code headlessly. Backs up first and refuses while the CubeMX GUI holds the project.",
     _obj({"project": PROJECT, "force": {"type": "boolean"},
           "drop_group": {"type": "array", "items": {"type": "string"}},
           "drop_include": {"type": "array", "items": {"type": "string"}}},
          ["project"]), t_generate),
    ("cubemx_build", "Build with Keil UV4 and parse errors and warnings.",
     _obj({"project": PROJECT}, ["project"]), t_build),
    ("cubemx_backups", "List snapshots taken before generation.",
     _obj({"label": {"type": "string"}}), t_backups),
    ("cubemx_restore", "Copy a snapshot back over the project.",
     _obj({"project": PROJECT, "backup": {"type": "string"}}, ["project", "backup"]), t_restore),
]

BY_NAME = {name: (desc, schema, fn) for name, desc, schema, fn in TOOLS}


# -------------------------------------------------------------------- protocol

def _reply(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id, result):
    _reply({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code, message):
    _reply({"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}})


def handle(request):
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        protocol = params.get("protocolVersion") or DEFAULT_PROTOCOL
        _result(request_id, {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        _result(request_id, {})
        return
    if method == "tools/list":
        _result(request_id, {"tools": [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, desc, schema, _ in TOOLS
        ]})
        return
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        entry = BY_NAME.get(name)
        if entry is None:
            _result(request_id, {"isError": True, "content": [
                {"type": "text", "text": "unknown tool: {}".format(name)}]})
            return
        try:
            payload = entry[2](arguments)
            text = json.dumps(payload, indent=2, ensure_ascii=False)
            failed = isinstance(payload, dict) and payload.get("ok") is False
        except Exception as exc:  # noqa: BLE001
            text = "{}: {}".format(type(exc).__name__, exc)
            failed = True
        _result(request_id, {"isError": failed,
                             "content": [{"type": "text", "text": text}]})
        return
    if request_id is not None:
        _error(request_id, -32601, "method not found: {}".format(method))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(request)
        except Exception as exc:  # noqa: BLE001
            if isinstance(request, dict) and request.get("id") is not None:
                _error(request["id"], -32603, "{}: {}".format(type(exc).__name__, exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
