from __future__ import annotations

import getpass
import json
import zipfile
from io import BytesIO
from html import escape
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, flash, redirect, render_template_string, request, url_for

APP_TITLE = "Technician Command Center"
APP_VERSION = "Step 14.5 - Health Route Hidden From Main Navigation"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "tcc.db"

PORTFOLIO_DEMO_PREFIX = "Portfolio Demo - "

TCC_DEMO_ONLY = os.getenv("TCC_DEMO_ONLY", "0").strip().lower() in {"1", "true", "yes", "on"}
TCC_APP_ENV = os.getenv("TCC_APP_ENV", "local").strip() or "local"

CASE_FIELDS = {
    "case_label": "Case Label",
    "affected_user": "Affected User",
    "affected_device": "Affected Device",
    "snapshot_purpose": "Snapshot Purpose",
    "root_cause": "Root Cause",
    "resolution_status": "Resolution Status",
    "technician_notes": "Technician Notes",
}

app = Flask(__name__)
app.secret_key = "change-this-later-for-production"


# -----------------------------
# Database helpers
# -----------------------------

def get_db_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                profile TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
        extra_columns = {
            "case_label": "TEXT NOT NULL DEFAULT ''",
            "affected_user": "TEXT NOT NULL DEFAULT ''",
            "affected_device": "TEXT NOT NULL DEFAULT ''",
            "snapshot_purpose": "TEXT NOT NULL DEFAULT ''",
            "root_cause": "TEXT NOT NULL DEFAULT ''",
            "resolution_status": "TEXT NOT NULL DEFAULT ''",
            "technician_notes": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in extra_columns.items():
            if column_name not in existing_columns:
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {column_name} {definition}")
        conn.commit()


def insert_snapshot(
    name: str,
    mode: str,
    profile: str,
    status: str,
    summary: str,
    payload: Dict[str, Any],
    case_fields: Optional[Dict[str, str]] = None,
) -> int:
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    case_fields = case_fields or {}
    values = {field: (case_fields.get(field) or "").strip() for field in CASE_FIELDS}
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO snapshots (
                name, mode, profile, status, summary, created_at, payload_json,
                case_label, affected_user, affected_device, snapshot_purpose,
                root_cause, resolution_status, technician_notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                mode,
                profile,
                status,
                summary,
                created_at,
                json.dumps(payload, indent=2),
                values["case_label"],
                values["affected_user"],
                values["affected_device"],
                values["snapshot_purpose"],
                values["root_cause"],
                values["resolution_status"],
                values["technician_notes"],
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def get_snapshots(limit: Optional[int] = None) -> List[sqlite3.Row]:
    query = "SELECT * FROM snapshots ORDER BY id DESC"
    params: Tuple[Any, ...] = ()
    if limit:
        query += " LIMIT ?"
        params = (limit,)

    with get_db_connection() as conn:
        return list(conn.execute(query, params).fetchall())


def get_snapshot(snapshot_id: int) -> Optional[sqlite3.Row]:
    with get_db_connection() as conn:
        return conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()


def snapshot_payload(snapshot_row: sqlite3.Row) -> Dict[str, Any]:
    return json.loads(snapshot_row["payload_json"])


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def get_case_metadata(row: sqlite3.Row) -> Dict[str, str]:
    return {field: clean_text(row[field]) for field in CASE_FIELDS}


def default_case_fields_for_profile(profile: str, label: str, mode: str, name: str = "") -> Dict[str, str]:
    normalized_name = (name or "").lower()
    if "before" in normalized_name:
        purpose = "Before Fix"
    elif "after" in normalized_name or "healthy" in normalized_name or "baseline" in normalized_name:
        purpose = "After Fix / Baseline"
    elif mode == "Local Mode":
        purpose = "Current State"
    else:
        purpose = "Investigation"

    root_causes = {
        "dns_issue": "Suspected DNS resolution or DNS server configuration issue.",
        "printer_issue": "Suspected printer availability, queue, service, or port issue.",
        "slow_pc": "Suspected low disk space, update activity, or endpoint performance issue.",
        "vpn_issue": "Suspected missing internal VPN routes or VPN access issue.",
        "healthy": "No active issue detected in this demo baseline.",
        "local_services": "Pending technician review.",
    }

    resolution_status = "Resolved / Baseline" if profile == "healthy" or "healthy" in normalized_name else "Investigating"
    return {
        "case_label": label,
        "affected_user": "",
        "affected_device": "LAP-DEMO-014" if mode == "Demo Mode" else current_machine_label(),
        "snapshot_purpose": purpose,
        "root_cause": root_causes.get(profile, "Pending technician review."),
        "resolution_status": resolution_status,
        "technician_notes": "",
    }


def build_case_fields_from_form(defaults: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    values = dict(defaults or {})
    for field in CASE_FIELDS:
        form_value = request.form.get(field)
        if form_value is not None and form_value.strip():
            values[field] = form_value.strip()
    return values


def update_snapshot_case(snapshot_id: int, case_fields: Dict[str, str]) -> None:
    values = {field: clean_text(case_fields.get(field)) for field in CASE_FIELDS}
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE snapshots
            SET case_label = ?, affected_user = ?, affected_device = ?, snapshot_purpose = ?,
                root_cause = ?, resolution_status = ?, technician_notes = ?
            WHERE id = ?
            """,
            (
                values["case_label"],
                values["affected_user"],
                values["affected_device"],
                values["snapshot_purpose"],
                values["root_cause"],
                values["resolution_status"],
                values["technician_notes"],
                snapshot_id,
            ),
        )
        conn.commit()


# -----------------------------
# Utility helpers
# -----------------------------

def safe_value(func, default: str = "Unavailable") -> str:
    try:
        value = func()
        if value is None or value == "":
            return default
        return str(value)
    except Exception:
        return default


def current_machine_label() -> str:
    return safe_value(socket.gethostname, "unknown-host")


def get_primary_local_ip() -> str:
    """Best-effort local IP detection without sending data.

    This creates a UDP socket to discover the preferred outbound interface.
    If that fails, the function falls back to hostname resolution.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.connect(("8.8.8.8", 80))
            ip_address = sock.getsockname()[0]
            if ip_address:
                return ip_address
    except Exception:
        pass

    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "Unavailable"


def run_command(command: List[str], timeout: int = 8) -> Dict[str, Any]:
    """Run a safe read-only command and return a structured result."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return {
            "command": " ".join(command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "command": " ".join(command),
            "exit_code": "Timeout",
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds.",
            "timed_out": True,
        }
    except FileNotFoundError:
        return {
            "command": " ".join(command),
            "exit_code": "Unavailable",
            "stdout": "",
            "stderr": f"Command not found: {command[0]}",
            "timed_out": False,
        }
    except Exception as exc:
        return {
            "command": " ".join(command),
            "exit_code": "Error",
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
        }


def short_output(text_value: str, max_lines: int = 6) -> str:
    lines = [line.strip() for line in text_value.splitlines() if line.strip()]
    return "\n".join(lines[:max_lines]) if lines else "No output"


def ping_host(host: str, label: str) -> Dict[str, Any]:
    """Run a small ping test using Windows or Unix syntax."""
    system_name = platform.system().lower()
    if system_name == "windows":
        command = ["ping", "-n", "2", "-w", "1500", host]
    else:
        command = ["ping", "-c", "2", "-W", "2", host]

    result = run_command(command, timeout=8)
    status = "Pass" if result.get("exit_code") == 0 else "Fail"
    return {
        "label": label,
        "target": host,
        "status": status,
        "command": result["command"],
        "exit_code": result["exit_code"],
        "details": short_output(result.get("stdout") or result.get("stderr", "")),
    }


def dns_lookup(hostname: str = "google.com") -> Dict[str, Any]:
    """Resolve a hostname using Python's socket resolver."""
    try:
        results = socket.getaddrinfo(hostname, 80)
        addresses = sorted({item[4][0] for item in results})
        return {
            "target": hostname,
            "status": "Pass",
            "addresses": addresses[:8],
            "details": f"Resolved {hostname} to {len(addresses)} unique address(es).",
        }
    except Exception as exc:
        return {
            "target": hostname,
            "status": "Fail",
            "addresses": [],
            "details": str(exc),
        }


def tcp_connectivity_test(hostname: str = "google.com", port: int = 443) -> Dict[str, Any]:
    """Check whether a TCP connection can be opened to a common public endpoint."""
    try:
        with socket.create_connection((hostname, port), timeout=4):
            return {
                "target": f"{hostname}:{port}",
                "status": "Pass",
                "details": "TCP connection succeeded.",
            }
    except Exception as exc:
        return {
            "target": f"{hostname}:{port}",
            "status": "Fail",
            "details": str(exc),
        }


def looks_like_ip(value: str) -> bool:
    value = value.strip().strip(".")
    if not value:
        return False
    ipv4 = r"^(?:\d{1,3}\.){3}\d{1,3}$"
    ipv6 = r"^[0-9a-fA-F:]+$"
    return bool(re.match(ipv4, value) or (":" in value and re.match(ipv6, value)))


def get_dns_servers_windows() -> List[str]:
    result = run_command(["ipconfig", "/all"], timeout=10)
    output = result.get("stdout", "")
    servers: List[str] = []
    capture_continuation = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            capture_continuation = False
            continue

        if "DNS Servers" in line:
            capture_continuation = True
            if ":" in line:
                candidate = line.split(":", 1)[1].strip()
                if looks_like_ip(candidate):
                    servers.append(candidate)
            continue

        if capture_continuation:
            if ":" in line and not looks_like_ip(stripped):
                capture_continuation = False
                continue
            if looks_like_ip(stripped):
                servers.append(stripped)
            else:
                capture_continuation = False

    return list(dict.fromkeys(servers))


def get_dns_servers_unix() -> List[str]:
    resolv_conf = Path("/etc/resolv.conf")
    servers: List[str] = []
    try:
        for line in resolv_conf.read_text(errors="ignore").splitlines():
            clean = line.strip()
            if clean.startswith("nameserver"):
                parts = clean.split()
                if len(parts) >= 2:
                    servers.append(parts[1])
    except Exception:
        pass
    return list(dict.fromkeys(servers))


def get_dns_servers() -> List[str]:
    if platform.system().lower() == "windows":
        servers = get_dns_servers_windows()
    else:
        servers = get_dns_servers_unix()
    return servers or ["Unavailable"]


def get_default_gateway_windows() -> str:
    result = run_command(["route", "print", "-4", "0.0.0.0"], timeout=8)
    output = result.get("stdout", "")
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            return parts[2]
    return "Unavailable"


def get_default_gateway_unix() -> str:
    result = run_command(["ip", "route", "show", "default"], timeout=5)
    output = result.get("stdout", "")
    for line in output.splitlines():
        parts = line.split()
        if "via" in parts:
            index = parts.index("via")
            if index + 1 < len(parts):
                return parts[index + 1]

    result = run_command(["netstat", "-rn"], timeout=5)
    output = result.get("stdout", "")
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] in {"0.0.0.0", "default"} and len(parts) >= 2:
            return parts[1]
    return "Unavailable"


def get_default_gateway() -> str:
    if platform.system().lower() == "windows":
        return get_default_gateway_windows()
    return get_default_gateway_unix()


def get_adapter_summary() -> Dict[str, Any]:
    """Collect a small read-only adapter summary for troubleshooting context."""
    if platform.system().lower() == "windows":
        result = run_command(["ipconfig"], timeout=8)
        return {
            "source": "ipconfig",
            "command": result["command"],
            "exit_code": result["exit_code"],
            "details": short_output(result.get("stdout") or result.get("stderr", ""), max_lines=14),
        }

    result = run_command(["ip", "addr"], timeout=5)
    if result.get("exit_code") == "Unavailable":
        result = run_command(["ifconfig"], timeout=5)
    return {
        "source": "ip addr / ifconfig",
        "command": result["command"],
        "exit_code": result["exit_code"],
        "details": short_output(result.get("stdout") or result.get("stderr", ""), max_lines=14),
    }


def get_network_diagnostics() -> Dict[str, Any]:
    primary_ip = get_primary_local_ip()
    gateway = get_default_gateway()
    dns_servers = get_dns_servers()

    gateway_ping = {
        "label": "Default Gateway Ping",
        "target": gateway,
        "status": "Skipped",
        "details": "Gateway unavailable, so this ping test was skipped.",
    }
    if gateway != "Unavailable":
        gateway_ping = ping_host(gateway, "Default Gateway Ping")

    internet_ping = ping_host("8.8.8.8", "Internet Ping")
    dns_result = dns_lookup("google.com")
    web_result = tcp_connectivity_test("google.com", 443)

    return {
        "primary_local_ip": primary_ip,
        "default_gateway": gateway,
        "dns_servers": dns_servers,
        "gateway_ping": gateway_ping,
        "internet_ping": internet_ping,
        "dns_lookup": dns_result,
        "web_connectivity": web_result,
        "adapter_summary": get_adapter_summary(),
        "notes": [
            "Ping can be blocked by firewalls, so a failed ping is a clue, not final proof.",
            "DNS lookup uses Python socket resolution, not the interactive nslookup command.",
        ],
    }

def normalize_service_status(raw_status: str) -> str:
    clean = (raw_status or "").strip().lower()
    if clean in {"running", "active"} or "running" in clean or "active (running)" in clean:
        return "Running"
    if clean in {"stopped", "inactive", "dead"} or "stopped" in clean or "inactive" in clean:
        return "Stopped"
    if "not found" in clean or "unavailable" in clean:
        return "Unavailable"
    if not clean:
        return "Unknown"
    return raw_status.strip()


def check_windows_service(service_name: str, display_name: str, recommended_state: str = "Running") -> Dict[str, Any]:
    """Read one Windows service state without changing anything."""
    ps_command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        f"$s = Get-Service -Name '{service_name}' -ErrorAction SilentlyContinue; "
        "if ($null -eq $s) { 'NOT_FOUND' } else { $s.Status.ToString() }",
    ]
    result = run_command(ps_command, timeout=8)
    raw_status = (result.get("stdout") or result.get("stderr") or "").strip().splitlines()
    status = raw_status[-1].strip() if raw_status else "Unavailable"

    if result.get("exit_code") == "Unavailable":
        # Fallback for older/minimal Windows shells where PowerShell is not found.
        result = run_command(["sc", "query", service_name], timeout=8)
        output = result.get("stdout") or result.get("stderr", "")
        status = "Unavailable"
        for line in output.splitlines():
            if "STATE" in line:
                status = "Running" if "RUNNING" in line.upper() else "Stopped"
                break
        if "does not exist" in output.lower():
            status = "Unavailable"

    normalized = normalize_service_status(status)
    is_expected = normalized == recommended_state or recommended_state == "Any"
    return {
        "service_name": service_name,
        "display_name": display_name,
        "status": normalized,
        "recommended_state": recommended_state,
        "result": "Pass" if is_expected else "Review",
        "command": result.get("command", "Unavailable"),
        "details": short_output(result.get("stdout") or result.get("stderr", ""), max_lines=5),
    }


def check_unix_service(service_name: str, display_name: str, recommended_state: str = "Running") -> Dict[str, Any]:
    """Read one Unix/Linux service state without changing anything."""
    result = run_command(["systemctl", "is-active", service_name], timeout=5)
    status_text = (result.get("stdout") or result.get("stderr") or "").strip()
    if result.get("exit_code") == "Unavailable":
        normalized = "Unavailable"
    else:
        normalized = normalize_service_status(status_text)

    is_expected = normalized == recommended_state or recommended_state == "Any"
    return {
        "service_name": service_name,
        "display_name": display_name,
        "status": normalized,
        "recommended_state": recommended_state,
        "result": "Pass" if is_expected else "Review",
        "command": result.get("command", "Unavailable"),
        "details": short_output(result.get("stdout") or result.get("stderr", ""), max_lines=5),
    }


def get_service_diagnostics() -> Dict[str, Any]:
    """Collect safe, read-only service diagnostics.

    Windows checks focus on services a helpdesk technician commonly reviews during
    printer, network, update, and workstation-access troubleshooting. The function
    does not start, stop, or modify any service.
    """
    system_name = platform.system().lower()
    checks: List[Dict[str, Any]] = []

    if system_name == "windows":
        service_targets = [
            ("Spooler", "Print Spooler", "Running"),
            ("Dhcp", "DHCP Client", "Running"),
            ("Dnscache", "DNS Client", "Running"),
            ("LanmanWorkstation", "Workstation", "Running"),
            ("wuauserv", "Windows Update", "Any"),
            ("W32Time", "Windows Time", "Any"),
        ]
        for name, display, expected in service_targets:
            checks.append(check_windows_service(name, display, expected))
    else:
        service_targets = [
            ("NetworkManager", "Network Manager", "Running"),
            ("systemd-resolved", "DNS Resolver", "Any"),
            ("cups", "Print Service", "Any"),
        ]
        for name, display, expected in service_targets:
            checks.append(check_unix_service(name, display, expected))

    review_items = [item for item in checks if item.get("result") == "Review"]
    unavailable_items = [item for item in checks if item.get("status") == "Unavailable"]

    return {
        "platform": platform.system(),
        "checks_are_read_only": True,
        "checks": checks,
        "summary": {
            "total_checked": len(checks),
            "pass_count": sum(1 for item in checks if item.get("result") == "Pass"),
            "review_count": len(review_items),
            "unavailable_count": len(unavailable_items),
        },
        "notes": [
            "Service checks are read-only. This app does not start, stop, or change services.",
            "Some services are allowed to be stopped depending on system policy, so they are marked with recommended state 'Any'.",
        ],
    }


# -----------------------------
# Printer diagnostics
# -----------------------------

def service_status_from_checks(services: Dict[str, Any], display_name: str) -> str:
    for item in services.get("checks", []):
        if item.get("display_name") == display_name:
            return str(item.get("status", "Unavailable"))
    return "Unavailable"


def bool_from_any(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"true", "yes", "1", "on", "enabled"}


def normalize_printer_status(value: Any) -> str:
    status_map = {
        "1": "Other",
        "2": "Unknown",
        "3": "Idle/Ready",
        "4": "Printing",
        "5": "Warmup",
        "6": "Stopped Printing",
        "7": "Offline",
    }
    if value is None or value == "":
        return "Unknown"
    text = str(value).strip()
    return status_map.get(text, text)


def normalize_error_state(value: Any) -> str:
    error_state_map = {
        "0": "Unknown",
        "1": "Other",
        "2": "No Error",
        "3": "Low Paper",
        "4": "No Paper",
        "5": "Low Toner",
        "6": "No Toner",
        "7": "Door Open",
        "8": "Jammed",
        "9": "Offline",
        "10": "Service Requested",
        "11": "Output Bin Full",
    }
    if value is None or value == "":
        return "Unknown"
    text = str(value).strip()
    return error_state_map.get(text, text)


def determine_printer_state(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return a more reliable printer availability result.

    Windows can expose printer offline state through several properties. In Step 5,
    the app only trusted WorkOffline. Step 5.1 also checks PrinterStatus, Status,
    PrinterState, and DetectedErrorState so printers like WSD network printers are
    detected correctly when Windows reports PrinterStatus=Offline.
    """
    work_offline = bool_from_any(record.get("WorkOffline", False))
    printer_status = normalize_printer_status(record.get("PrinterStatus"))
    windows_status = str(record.get("Status", "") or "").strip()
    printer_state_raw = str(record.get("PrinterState", "") or "").strip()
    detected_error_state = normalize_error_state(record.get("DetectedErrorState"))

    reasons: List[str] = []

    if work_offline:
        reasons.append("WorkOffline=True")

    if "offline" in printer_status.lower():
        reasons.append(f"PrinterStatus={printer_status}")

    if "offline" in windows_status.lower():
        reasons.append(f"Status={windows_status}")

    if "offline" in printer_state_raw.lower():
        reasons.append(f"PrinterState={printer_state_raw}")

    if "offline" in detected_error_state.lower():
        reasons.append(f"DetectedErrorState={detected_error_state}")

    is_offline = bool(reasons)

    if is_offline:
        status_display = "Offline"
        availability = "Offline"
        reason_display = "; ".join(reasons)
    else:
        status_display = printer_status if printer_status != "Unknown" else (windows_status or "Unknown")
        availability = "Ready/Available" if status_display.lower() in {"normal", "idle/ready", "ready"} else status_display
        reason_display = "No offline signal detected"

    return {
        "status_display": status_display,
        "availability": availability,
        "is_offline": is_offline,
        "offline_reason": reason_display,
        "raw_printer_status": printer_status,
        "raw_windows_status": windows_status or "Unavailable",
        "raw_printer_state": printer_state_raw or "Unavailable",
        "raw_detected_error_state": detected_error_state,
    }


def printer_type_from_record(record: Dict[str, Any]) -> str:
    name = str(record.get("Name", "") or record.get("name", "")).lower()
    port = str(record.get("PortName", "") or record.get("port", "")).lower()
    driver = str(record.get("DriverName", "") or record.get("driver", "")).lower()
    if "pdf" in name or "pdf" in driver:
        return "Virtual/PDF"
    if "onenote" in name or "xps" in name or "fax" in name:
        return "Virtual"
    if bool_from_any(record.get("Network", False)) or port.startswith("ip_") or port.startswith("tcp"):
        return "Network"
    if "usb" in port:
        return "USB/Local"
    return "Local/Unknown"


def parse_json_maybe(text_value: str) -> Any:
    clean = (text_value or "").strip()
    if not clean:
        return []
    try:
        return json.loads(clean)
    except Exception:
        # PowerShell can sometimes print warnings before JSON. Try from first JSON char.
        for marker in ("[", "{"):
            index = clean.find(marker)
            if index >= 0:
                try:
                    return json.loads(clean[index:])
                except Exception:
                    pass
    return []


def get_windows_printers() -> Dict[str, Any]:
    ps_command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "$printers = Get-CimInstance Win32_Printer | Select-Object Name,Default,WorkOffline,PrinterStatus,Status,PrinterState,DetectedErrorState,PortName,DriverName,Network,Shared; "
        "if ($null -eq $printers) { '[]' } else { $printers | ConvertTo-Json -Depth 4 -Compress }",
    ]
    result = run_command(ps_command, timeout=12)
    raw = parse_json_maybe(result.get("stdout", ""))

    if isinstance(raw, dict):
        raw_printers = [raw]
    elif isinstance(raw, list):
        raw_printers = raw
    else:
        raw_printers = []

    printers: List[Dict[str, Any]] = []
    for item in raw_printers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name", "Unknown Printer"))
        is_default = bool_from_any(item.get("Default", False))
        state = determine_printer_state(item)
        printers.append(
            {
                "name": name,
                "is_default": is_default,
                "status": state["status_display"],
                "availability": state["availability"],
                "is_offline": state["is_offline"],
                "offline_reason": state["offline_reason"],
                "raw_printer_status": state["raw_printer_status"],
                "raw_windows_status": state["raw_windows_status"],
                "raw_printer_state": state["raw_printer_state"],
                "raw_detected_error_state": state["raw_detected_error_state"],
                "port": str(item.get("PortName", "Unavailable")),
                "driver": str(item.get("DriverName", "Unavailable")),
                "is_network": bool_from_any(item.get("Network", False)),
                "is_shared": bool_from_any(item.get("Shared", False)),
                "type": printer_type_from_record(item),
                "queue_count": 0,
                "queue_status": "Not checked yet",
            }
        )

    # Best effort print job count. It can fail on systems without the PrintManagement module.
    queue_command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "$out = @(); "
        "Get-Printer -ErrorAction SilentlyContinue | ForEach-Object { "
        "  $count = 0; "
        "  try { $jobs = @(Get-PrintJob -PrinterName $_.Name -ErrorAction SilentlyContinue); $count = $jobs.Count } catch { $count = 0 }; "
        "  $out += [PSCustomObject]@{Name=$_.Name; JobCount=$count} "
        "}; "
        "if ($out.Count -eq 0) { '[]' } else { $out | ConvertTo-Json -Depth 3 -Compress }",
    ]
    queue_result = run_command(queue_command, timeout=12)
    queue_raw = parse_json_maybe(queue_result.get("stdout", ""))
    if isinstance(queue_raw, dict):
        queue_items = [queue_raw]
    elif isinstance(queue_raw, list):
        queue_items = queue_raw
    else:
        queue_items = []

    queue_by_name = {}
    for item in queue_items:
        if isinstance(item, dict):
            queue_by_name[str(item.get("Name", ""))] = int(item.get("JobCount") or 0)

    for printer in printers:
        if printer["name"] in queue_by_name:
            printer["queue_count"] = queue_by_name[printer["name"]]
            printer["queue_status"] = "Checked"
        elif queue_result.get("exit_code") == 0:
            printer["queue_status"] = "Checked - no jobs found"
        else:
            printer["queue_status"] = "Unavailable"

    return {
        "printers": printers,
        "command": result.get("command", "Unavailable"),
        "queue_command": queue_result.get("command", "Unavailable"),
        "raw_result": short_output(result.get("stdout") or result.get("stderr", ""), max_lines=8),
        "queue_raw_result": short_output(queue_result.get("stdout") or queue_result.get("stderr", ""), max_lines=8),
    }


def get_unix_printers() -> Dict[str, Any]:
    result = run_command(["lpstat", "-p", "-d"], timeout=8)
    output = result.get("stdout") or result.get("stderr", "")
    printers: List[Dict[str, Any]] = []
    default_printer = "None"

    for line in output.splitlines():
        clean = line.strip()
        if not clean:
            continue
        if clean.lower().startswith("system default destination:"):
            default_printer = clean.split(":", 1)[-1].strip()
        elif clean.lower().startswith("printer "):
            parts = clean.split()
            name = parts[1] if len(parts) > 1 else "Unknown Printer"
            is_disabled = "disabled" in clean.lower()
            printers.append(
                {
                    "name": name,
                    "is_default": name == default_printer,
                    "status": "Disabled" if is_disabled else "Ready/Idle",
                    "is_offline": is_disabled,
                    "port": "CUPS",
                    "driver": "Unavailable",
                    "is_network": False,
                    "is_shared": False,
                    "type": "CUPS/Local",
                    "queue_count": 0,
                    "queue_status": "Not checked",
                }
            )

    if result.get("exit_code") == "Unavailable":
        printers = []

    return {
        "printers": printers,
        "command": result.get("command", "Unavailable"),
        "queue_command": "Unavailable",
        "raw_result": short_output(output, max_lines=8),
        "queue_raw_result": "Unavailable",
    }


def get_printer_diagnostics(services: Dict[str, Any]) -> Dict[str, Any]:
    """Collect safe, read-only printer diagnostics.

    This checks printer inventory and queue information where available. It never
    starts, stops, removes, installs, or modifies printers.
    """
    system_name = platform.system().lower()
    print_service_status = service_status_from_checks(services, "Print Spooler")
    if print_service_status == "Unavailable":
        print_service_status = service_status_from_checks(services, "Print Service")

    if system_name == "windows":
        printer_data = get_windows_printers()
    else:
        printer_data = get_unix_printers()

    printers = printer_data.get("printers", [])
    installed_count = len(printers)
    default_printers = [item for item in printers if item.get("is_default")]
    offline_printers = [item for item in printers if item.get("is_offline")]
    queued_jobs_total = sum(int(item.get("queue_count") or 0) for item in printers)
    physical_or_network = [item for item in printers if item.get("type") not in {"Virtual/PDF", "Virtual"}]

    if installed_count == 0:
        result = "Info"
        readiness = "No installed printers detected. Print service can still be healthy without a connected printer."
    elif print_service_status not in {"Running", "Any", "Unavailable"}:
        result = "Review"
        readiness = "Printer inventory exists, but the print service may need review."
    elif offline_printers or queued_jobs_total > 0:
        result = "Review"
        readiness = "One or more printers may be offline or have queued jobs."
    else:
        result = "Pass"
        readiness = "Printer inventory was detected and no obvious queue/offline issue was found."

    return {
        "platform": platform.system(),
        "checks_are_read_only": True,
        "print_service_status": print_service_status,
        "installed_printer_count": installed_count,
        "physical_or_network_printer_count": len(physical_or_network),
        "default_printer": default_printers[0].get("name") if default_printers else "None configured",
        "offline_printer_count": len(offline_printers),
        "queued_jobs_total": queued_jobs_total,
        "result": result,
        "readiness_summary": readiness,
        "printers": printers,
        "commands": {
            "inventory": printer_data.get("command", "Unavailable"),
            "queue": printer_data.get("queue_command", "Unavailable"),
        },
        "raw_output_summary": printer_data.get("raw_result", "Unavailable"),
        "queue_output_summary": printer_data.get("queue_raw_result", "Unavailable"),
        "notes": [
            "Print service health and printer connection/inventory are separate checks.",
            "A running Print Spooler means Windows can process print jobs; it does not prove a printer is connected.",
            "Printer diagnostics are read-only. The app did not add, remove, pause, resume, or clear any printer or job.",
            "Offline detection checks several Windows signals, including WorkOffline, PrinterStatus, Status, PrinterState, and DetectedErrorState.",
        ],
    }


# -----------------------------
# Endpoint performance diagnostics
# -----------------------------

def format_duration_from_seconds(seconds: float) -> str:
    try:
        total = int(float(seconds))
    except Exception:
        return "Unavailable"
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m"


def get_uptime_summary() -> Dict[str, Any]:
    system_name = platform.system().lower()
    if system_name == "windows":
        ps_command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$os = Get-CimInstance Win32_OperatingSystem; "
            "$boot = $os.LastBootUpTime; "
            "$uptime = (Get-Date) - $boot; "
            "[pscustomobject]@{"
            "LastBootTime=$boot.ToString('yyyy-MM-dd HH:mm:ss');"
            "Uptime=('{0}d {1}h {2}m' -f $uptime.Days,$uptime.Hours,$uptime.Minutes);"
            "UptimeDays=[math]::Round($uptime.TotalDays,2)"
            "} | ConvertTo-Json -Compress",
        ]
        result = run_command(ps_command, timeout=10)
        raw = parse_json_maybe(result.get("stdout", ""))
        if isinstance(raw, dict):
            return {
                "last_boot_time": raw.get("LastBootTime", "Unavailable"),
                "uptime": raw.get("Uptime", "Unavailable"),
                "uptime_days": raw.get("UptimeDays", "Unavailable"),
                "command": result.get("command", "Unavailable"),
                "result": "Pass",
            }
        return {
            "last_boot_time": "Unavailable",
            "uptime": "Unavailable",
            "uptime_days": "Unavailable",
            "command": result.get("command", "Unavailable"),
            "result": "Unavailable",
            "details": short_output(result.get("stderr") or result.get("stdout", "")),
        }

    proc_uptime = Path("/proc/uptime")
    try:
        seconds = float(proc_uptime.read_text().split()[0])
        return {
            "last_boot_time": "Unavailable",
            "uptime": format_duration_from_seconds(seconds),
            "uptime_days": round(seconds / 86400, 2),
            "command": "read /proc/uptime",
            "result": "Pass",
        }
    except Exception as exc:
        return {
            "last_boot_time": "Unavailable",
            "uptime": "Unavailable",
            "uptime_days": "Unavailable",
            "command": "read /proc/uptime",
            "result": "Unavailable",
            "details": str(exc),
        }


def get_memory_summary() -> Dict[str, Any]:
    system_name = platform.system().lower()
    if system_name == "windows":
        ps_command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$os = Get-CimInstance Win32_OperatingSystem; "
            "$total = [math]::Round($os.TotalVisibleMemorySize/1024,2); "
            "$free = [math]::Round($os.FreePhysicalMemory/1024,2); "
            "$used = [math]::Round($total-$free,2); "
            "$pct = if ($total -gt 0) { [math]::Round(($free/$total)*100,1) } else { 0 }; "
            "[pscustomobject]@{TotalMB=$total;FreeMB=$free;UsedMB=$used;FreePercent=$pct} | ConvertTo-Json -Compress",
        ]
        result = run_command(ps_command, timeout=10)
        raw = parse_json_maybe(result.get("stdout", ""))
        if isinstance(raw, dict):
            free_percent = raw.get("FreePercent", "Unavailable")
            status = "Healthy"
            try:
                if float(free_percent) < 15:
                    status = "Low Memory"
            except Exception:
                status = "Unknown"
            return {
                "total_mb": raw.get("TotalMB", "Unavailable"),
                "used_mb": raw.get("UsedMB", "Unavailable"),
                "free_mb": raw.get("FreeMB", "Unavailable"),
                "free_percent": free_percent,
                "status": status,
                "command": result.get("command", "Unavailable"),
            }
        return {
            "total_mb": "Unavailable",
            "used_mb": "Unavailable",
            "free_mb": "Unavailable",
            "free_percent": "Unavailable",
            "status": "Unavailable",
            "command": result.get("command", "Unavailable"),
            "details": short_output(result.get("stderr") or result.get("stdout", "")),
        }

    meminfo = Path("/proc/meminfo")
    try:
        values: Dict[str, int] = {}
        for line in meminfo.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                values[parts[0].rstrip(":")] = int(parts[1])
        total_kb = values.get("MemTotal", 0)
        available_kb = values.get("MemAvailable", values.get("MemFree", 0))
        used_kb = max(total_kb - available_kb, 0)
        free_percent = round((available_kb / total_kb) * 100, 1) if total_kb else 0
        status = "Healthy" if free_percent >= 15 else "Low Memory"
        return {
            "total_mb": round(total_kb / 1024, 2),
            "used_mb": round(used_kb / 1024, 2),
            "free_mb": round(available_kb / 1024, 2),
            "free_percent": free_percent,
            "status": status,
            "command": "read /proc/meminfo",
        }
    except Exception as exc:
        return {
            "total_mb": "Unavailable",
            "used_mb": "Unavailable",
            "free_mb": "Unavailable",
            "free_percent": "Unavailable",
            "status": "Unavailable",
            "command": "read /proc/meminfo",
            "details": str(exc),
        }


def get_process_summary() -> Dict[str, Any]:
    system_name = platform.system().lower()
    if system_name == "windows":
        ps_command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$p = Get-Process; "
            "$top = $p | Sort-Object WorkingSet64 -Descending | Select-Object -First 5 ProcessName,Id,@{Name='MemoryMB';Expression={[math]::Round($_.WorkingSet64/1MB,1)}}; "
            "[pscustomobject]@{ProcessCount=$p.Count;TopMemoryProcesses=$top} | ConvertTo-Json -Depth 5 -Compress",
        ]
        result = run_command(ps_command, timeout=12)
        raw = parse_json_maybe(result.get("stdout", ""))
        if isinstance(raw, dict):
            top = raw.get("TopMemoryProcesses", [])
            if isinstance(top, dict):
                top = [top]
            formatted_top = []
            for item in top if isinstance(top, list) else []:
                if isinstance(item, dict):
                    formatted_top.append({
                        "name": item.get("ProcessName", "Unknown"),
                        "pid": item.get("Id", "Unavailable"),
                        "memory_mb": item.get("MemoryMB", "Unavailable"),
                    })
            return {
                "process_count": raw.get("ProcessCount", "Unavailable"),
                "top_memory_processes": formatted_top,
                "command": result.get("command", "Unavailable"),
                "result": "Pass",
            }
        return {
            "process_count": "Unavailable",
            "top_memory_processes": [],
            "command": result.get("command", "Unavailable"),
            "result": "Unavailable",
            "details": short_output(result.get("stderr") or result.get("stdout", "")),
        }

    result = run_command(["ps", "-eo", "pid,comm,rss", "--sort=-rss"], timeout=8)
    lines = [line for line in (result.get("stdout") or "").splitlines() if line.strip()]
    top_processes = []
    for line in lines[1:6]:
        parts = line.split(None, 2)
        if len(parts) >= 3:
            try:
                memory_mb = round(int(parts[2]) / 1024, 1)
            except Exception:
                memory_mb = "Unavailable"
            top_processes.append({"pid": parts[0], "name": parts[1], "memory_mb": memory_mb})
    return {
        "process_count": max(len(lines) - 1, 0) if lines else "Unavailable",
        "top_memory_processes": top_processes,
        "command": result.get("command", "Unavailable"),
        "result": "Pass" if result.get("exit_code") == 0 else "Unavailable",
        "details": short_output(result.get("stderr") or result.get("stdout", ""), max_lines=6),
    }


def get_battery_summary() -> Dict[str, Any]:
    system_name = platform.system().lower()
    if system_name == "windows":
        ps_command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$b = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue; "
            "if ($null -eq $b) { [pscustomobject]@{Present=$false;Status='No battery detected'} } "
            "else { $b | Select-Object @{Name='Present';Expression={$true}},EstimatedChargeRemaining,BatteryStatus,Status | ConvertTo-Json -Compress }",
        ]
        result = run_command(ps_command, timeout=10)
        raw = parse_json_maybe(result.get("stdout", ""))
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if isinstance(raw, dict):
            battery_status_map = {
                "1": "Discharging",
                "2": "AC power / not discharging",
                "3": "Fully charged",
                "4": "Low",
                "5": "Critical",
                "6": "Charging",
                "7": "Charging and high",
                "8": "Charging and low",
                "9": "Charging and critical",
                "10": "Undefined",
                "11": "Partially charged",
            }
            status_code = str(raw.get("BatteryStatus", ""))
            status = raw.get("Status") or battery_status_map.get(status_code, raw.get("BatteryStatus", "Unavailable"))
            return {
                "present": bool_from_any(raw.get("Present", False)),
                "charge_percent": raw.get("EstimatedChargeRemaining", "Unavailable"),
                "status": status,
                "command": result.get("command", "Unavailable"),
            }
        return {
            "present": False,
            "charge_percent": "Unavailable",
            "status": "Unavailable",
            "command": result.get("command", "Unavailable"),
            "details": short_output(result.get("stderr") or result.get("stdout", "")),
        }

    battery_dirs = list(Path("/sys/class/power_supply").glob("BAT*")) if Path("/sys/class/power_supply").exists() else []
    if not battery_dirs:
        return {"present": False, "charge_percent": "Unavailable", "status": "No battery detected", "command": "read /sys/class/power_supply"}
    battery = battery_dirs[0]
    try:
        capacity = (battery / "capacity").read_text().strip() if (battery / "capacity").exists() else "Unavailable"
        status = (battery / "status").read_text().strip() if (battery / "status").exists() else "Unavailable"
        return {"present": True, "charge_percent": capacity, "status": status, "command": "read /sys/class/power_supply"}
    except Exception as exc:
        return {"present": True, "charge_percent": "Unavailable", "status": "Unavailable", "command": "read /sys/class/power_supply", "details": str(exc)}


def get_recent_system_events() -> Dict[str, Any]:
    system_name = platform.system().lower()
    if system_name == "windows":
        ps_command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$events = Get-WinEvent -FilterHashtable @{LogName='System'; Level=2,3; StartTime=(Get-Date).AddHours(-24)} -MaxEvents 10 -ErrorAction SilentlyContinue; "
            "if ($null -eq $events) { '[]' } else { $events | Select-Object TimeCreated,LevelDisplayName,ProviderName,Id,Message | ConvertTo-Json -Depth 4 -Compress }",
        ]
        result = run_command(ps_command, timeout=16)
        raw = parse_json_maybe(result.get("stdout", ""))
        if isinstance(raw, dict):
            raw_events = [raw]
        elif isinstance(raw, list):
            raw_events = raw
        else:
            raw_events = []
        events = []
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            message = str(item.get("Message", "") or "").replace("\r", " ").replace("\n", " ").strip()
            if len(message) > 180:
                message = message[:177] + "..."
            events.append({
                "time": str(item.get("TimeCreated", "Unavailable")),
                "level": str(item.get("LevelDisplayName", "Unavailable")),
                "provider": str(item.get("ProviderName", "Unavailable")),
                "event_id": str(item.get("Id", "Unavailable")),
                "message": message or "No message",
            })
        warning_count = sum(1 for event in events if "warning" in event.get("level", "").lower())
        error_count = sum(1 for event in events if "error" in event.get("level", "").lower())
        return {
            "lookback_hours": 24,
            "event_count": len(events),
            "error_count": error_count,
            "warning_count": warning_count,
            "events": events,
            "command": result.get("command", "Unavailable"),
            "result": "Pass" if result.get("exit_code") == 0 else "Review",
            "details": "Recent System log warnings/errors collected." if events else "No recent System log warnings/errors were returned by the command.",
        }

    # Cross-platform safe fallback. We avoid requiring sudo or changing logging configuration.
    result = run_command(["journalctl", "-p", "warning", "--since", "24 hours ago", "-n", "10", "--no-pager"], timeout=8)
    lines = [line.strip() for line in (result.get("stdout") or "").splitlines() if line.strip()]
    events = [{"time": "Recent", "level": "Warning/Error", "provider": "journalctl", "event_id": "N/A", "message": line[:180]} for line in lines[:10]]
    return {
        "lookback_hours": 24,
        "event_count": len(events),
        "error_count": "Unavailable",
        "warning_count": len(events),
        "events": events,
        "command": result.get("command", "Unavailable"),
        "result": "Pass" if result.get("exit_code") == 0 else "Unavailable",
        "details": "journalctl output collected." if events else "Recent event check unavailable or no warnings returned.",
    }


def get_endpoint_health_diagnostics() -> Dict[str, Any]:
    """Collect read-only endpoint performance indicators for local snapshots."""
    uptime = get_uptime_summary()
    memory = get_memory_summary()
    processes = get_process_summary()
    battery = get_battery_summary()
    recent_events = get_recent_system_events()
    return {
        "checks_are_read_only": True,
        "uptime": uptime,
        "memory": memory,
        "processes": processes,
        "battery": battery,
        "recent_system_events": recent_events,
        "notes": [
            "Endpoint performance checks are read-only and intended to provide context for slow-PC or recurring-issue troubleshooting.",
            "Recent event log results are clues for technician review, not automatic proof of root cause.",
            "Top memory process data helps explain resource pressure but should be interpreted with user workload context.",
        ],
    }

def get_storage_summary() -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(BASE_DIR.anchor or "/")
        total_gb = round(usage.total / (1024 ** 3), 2)
        free_gb = round(usage.free / (1024 ** 3), 2)
        used_gb = round(usage.used / (1024 ** 3), 2)
        free_percent = round((usage.free / usage.total) * 100, 1) if usage.total else 0
        status = "Healthy" if free_percent >= 15 else "Low Disk Space"
        return {
            "drive_checked": BASE_DIR.anchor or "/",
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "free_percent": free_percent,
            "status": status,
        }
    except Exception as exc:
        return {
            "drive_checked": "Unavailable",
            "total_gb": "Unavailable",
            "used_gb": "Unavailable",
            "free_gb": "Unavailable",
            "free_percent": "Unavailable",
            "status": "Unavailable",
            "error": str(exc),
        }


def get_basic_local_payload() -> Dict[str, Any]:
    hostname = current_machine_label()
    storage = get_storage_summary()
    network = get_network_diagnostics()
    services = get_service_diagnostics()
    printers = get_printer_diagnostics(services)
    endpoint = get_endpoint_health_diagnostics()

    findings: List[str] = []
    recommended_steps: List[str] = []
    health_score = 100

    local_ip = network.get("primary_local_ip", "Unavailable")
    gateway = network.get("default_gateway", "Unavailable")
    gateway_ping_status = network.get("gateway_ping", {}).get("status", "Unknown")
    internet_ping_status = network.get("internet_ping", {}).get("status", "Unknown")
    dns_lookup_status = network.get("dns_lookup", {}).get("status", "Unknown")
    web_status = network.get("web_connectivity", {}).get("status", "Unknown")

    if local_ip == "Unavailable" or str(local_ip).startswith("127."):
        findings.append("Local IP address could not be detected clearly.")
        recommended_steps.append("Check network adapter status and verify the machine is connected to the network.")
        health_score -= 15

    if gateway == "Unavailable":
        findings.append("Default gateway could not be detected.")
        recommended_steps.append("Check DHCP configuration or adapter default gateway settings.")
        health_score -= 10
    elif gateway_ping_status == "Fail":
        findings.append("Default gateway ping failed. This may indicate a local network, Wi-Fi, cable, or gateway reachability issue.")
        recommended_steps.append("Verify Wi-Fi/cable connection and test whether other devices can reach the gateway.")
        health_score -= 20

    if internet_ping_status == "Fail":
        findings.append("Internet ping to 8.8.8.8 failed. This may indicate internet connectivity issues or blocked ICMP.")
        recommended_steps.append("Confirm internet access with a browser and check whether ICMP/ping is blocked by firewall policy.")
        health_score -= 15

    if dns_lookup_status == "Fail":
        findings.append("DNS lookup for google.com failed.")
        if internet_ping_status == "Pass":
            findings.append("Internet ping passed while DNS lookup failed, which strongly suggests a DNS/name-resolution problem.")
        recommended_steps.append("Review DNS server settings and test with nslookup google.com.")
        health_score -= 25

    if web_status == "Fail":
        findings.append("TCP connectivity test to google.com:443 failed.")
        recommended_steps.append("Check firewall, proxy, VPN, or browser/network restrictions.")
        health_score -= 10

    if storage.get("status") == "Low Disk Space":
        findings.append("System drive free space is below the recommended threshold.")
        recommended_steps.append("Free disk space before continuing deeper troubleshooting.")
        health_score -= 20

    memory_status = endpoint.get("memory", {}).get("status", "Unknown")
    if memory_status == "Low Memory":
        findings.append("Available memory is below the recommended threshold.")
        recommended_steps.append("Review top memory processes and close unnecessary applications before deeper troubleshooting.")
        health_score -= 15

    try:
        uptime_days = float(endpoint.get("uptime", {}).get("uptime_days", 0))
    except Exception:
        uptime_days = 0
    if uptime_days >= 14:
        findings.append(f"System uptime is high ({endpoint.get('uptime', {}).get('uptime', 'Unavailable')}). A reboot may be worth considering if updates or performance issues are present.")
        recommended_steps.append("Consider scheduling a reboot if the user reports slowness and business impact allows it.")
        health_score -= 5

    recent_events = endpoint.get("recent_system_events", {})
    try:
        recent_error_count = int(recent_events.get("error_count", 0))
    except Exception:
        recent_error_count = 0
    if recent_error_count > 0:
        findings.append(f"Recent System event log errors were found in the last {recent_events.get('lookback_hours', 24)} hours.")
        recommended_steps.append("Review recent System event errors for hardware, driver, service, or update clues.")
        health_score -= min(10, recent_error_count * 2)

    service_review_items = [item for item in services.get("checks", []) if item.get("result") == "Review"]
    critical_service_names = {"Print Spooler", "DHCP Client", "DNS Client", "Workstation", "Network Manager"}
    for item in service_review_items:
        display_name = item.get("display_name", "Unknown service")
        current_status = item.get("status", "Unknown")
        expected_status = item.get("recommended_state", "Unknown")
        findings.append(f"Service check needs review: {display_name} is {current_status}; expected {expected_status}.")
        if display_name in critical_service_names:
            health_score -= 12
        else:
            health_score -= 5

    if service_review_items:
        recommended_steps.append("Review services marked as 'Review' before troubleshooting deeper application issues.")
        recommended_steps.append("Do not restart services without confirming user impact and change-control expectations.")

    printer_result = printers.get("result", "Unknown")
    if printer_result == "Review":
        findings.append(f"Printer diagnostics need review: {printers.get('readiness_summary', 'Printer issue detected.')}")
        recommended_steps.append("Review installed printers, default printer, offline state, and queued jobs before escalating a print issue.")
        health_score -= 10
    elif printer_result == "Info":
        findings.append(printers.get("readiness_summary", "No installed printers detected."))

    if not findings:
        findings.append("Local system and network diagnostic snapshot completed successfully.")
        findings.append("Hostname, OS, user, disk, IP, gateway, DNS, ping, and web connectivity checks were collected.")

    if not recommended_steps:
        recommended_steps.append("Save this snapshot as a local baseline.")
        recommended_steps.append("Create another snapshot after a fix to compare what changed.")

    health_score = max(0, health_score)
    status = "Healthy" if health_score >= 85 else "Needs Attention"

    return {
        "health_score": health_score,
        "status": status,
        "system": {
            "hostname": hostname,
            "logged_in_user": safe_value(getpass.getuser),
            "operating_system": platform.platform(),
            "os_name": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "machine_type": platform.machine(),
            "processor": platform.processor() or "Unavailable",
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "current_working_directory": os.getcwd(),
            "snapshot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "network": network,
        "storage": storage,
        "endpoint": endpoint,
        "services": services,
        "printers": printers,
        "findings": findings,
        "recommended_next_steps": recommended_steps,
    }


# -----------------------------
# Demo diagnostic data
# -----------------------------

def build_demo_payload(profile: str) -> Dict[str, Any]:
    hostname = current_machine_label()
    base: Dict[str, Any] = {
        "health_score": 96,
        "status": "Healthy",
        "system": {
            "hostname": hostname,
            "os": "Windows 11 Pro - demo data",
            "python_version": platform.python_version(),
            "uptime": "Demo data - real uptime will be added later",
            "logged_in_user": "demo.user",
            "asset_tag": "LAP-DEMO-014",
        },
        "network": {
            "adapter": "Wi-Fi",
            "ip_address": "192.168.1.44",
            "subnet_mask": "255.255.255.0",
            "gateway": "192.168.1.1",
            "dns_servers": ["192.168.1.1", "8.8.8.8"],
            "gateway_ping": "Pass",
            "internet_ping": "Pass",
            "dns_lookup": "Pass",
        },
        "services": {
            "print_spooler": "Running",
            "windows_update": "Running",
            "workstation": "Running",
            "dhcp_client": "Running",
        },
        "printers": {
            "default_printer": "PRN-Office-01",
            "printer_status": "Ready",
            "queue_depth": 0,
            "last_test_page": "Successful",
        },
        "storage": {
            "system_drive_free": "126 GB",
            "system_drive_status": "Healthy",
        },
        "findings": [
            "Endpoint appears healthy in demo data.",
            "Network, DNS, printer, and core service checks are passing.",
        ],
        "recommended_next_steps": [
            "Save this as a baseline snapshot.",
            "Compare it with a later snapshot if an issue appears.",
        ],
    }

    if profile == "healthy":
        return base

    if profile == "dns_issue":
        base["network"]["dns_servers"] = ["10.10.10.10"]
        base["network"]["dns_lookup"] = "Fail"
        base["findings"] = [
            "Internet ping succeeds, but DNS lookup fails.",
            "This suggests a name resolution problem rather than total internet failure.",
        ]
        base["recommended_next_steps"] = [
            "Verify DNS server settings.",
            "Try switching adapter DNS back to DHCP or a known internal DNS server.",
            "Escalate to network team if multiple users are affected.",
        ]
        base["health_score"] = 62
        base["status"] = "Needs Attention"
        return base

    if profile == "printer_issue":
        base["services"]["print_spooler"] = "Stopped"
        base["printers"]["printer_status"] = "Offline"
        base["printers"]["queue_depth"] = 7
        base["printers"]["last_test_page"] = "Failed"
        base["findings"] = [
            "Print spooler is stopped.",
            "Default printer appears offline and has queued jobs.",
        ]
        base["recommended_next_steps"] = [
            "Restart the print spooler service.",
            "Clear stuck jobs if approved by the user.",
            "Verify printer IP address and print a test page.",
        ]
        base["health_score"] = 58
        base["status"] = "Needs Attention"
        return base

    if profile == "slow_pc":
        base["storage"]["system_drive_free"] = "4 GB"
        base["storage"]["system_drive_status"] = "Low Disk Space"
        base["services"]["windows_update"] = "Running - High Activity"
        base["findings"] = [
            "System drive has very low free space.",
            "Windows Update activity may be contributing to slowness.",
        ]
        base["recommended_next_steps"] = [
            "Free disk space and clear temporary files.",
            "Review startup apps and recent software installs.",
            "Reboot after updates complete if appropriate.",
        ]
        base["health_score"] = 54
        base["status"] = "Needs Attention"
        return base

    if profile == "vpn_issue":
        base["network"]["adapter"] = "VPN Adapter"
        base["network"]["ip_address"] = "10.8.0.24"
        base["network"]["internal_route_check"] = "Fail"
        base["findings"] = [
            "VPN appears connected, but internal route check fails.",
            "This may explain why shared drives or internal apps are unavailable.",
        ]
        base["recommended_next_steps"] = [
            "Disconnect and reconnect VPN.",
            "Check route table for internal subnets.",
            "Escalate to network/VPN team if the route is missing after reconnect.",
        ]
        base["health_score"] = 68
        base["status"] = "Needs Attention"
        return base

    base["health_score"] = random.randint(80, 96)
    return base


def create_demo_snapshot(profile: str, custom_name: Optional[str] = None, case_fields: Optional[Dict[str, str]] = None) -> int:
    payload = build_demo_payload(profile)
    profile_labels = {
        "healthy": "Healthy Endpoint",
        "dns_issue": "DNS Issue",
        "printer_issue": "Printer Issue",
        "slow_pc": "Slow PC",
        "vpn_issue": "VPN Issue",
    }
    label = profile_labels.get(profile, "Demo Snapshot")
    name = custom_name.strip() if custom_name and custom_name.strip() else f"{label} - {datetime.now().strftime('%H:%M:%S')}"
    status = payload.get("status", "Unknown")
    score = payload.get("health_score", "N/A")
    summary = f"{label} demo snapshot. Health score: {score}. Status: {status}."
    defaults = default_case_fields_for_profile(profile, label, "Demo Mode", name)
    if case_fields:
        defaults.update({key: value for key, value in case_fields.items() if clean_text(value)})
    return insert_snapshot(name=name, mode="Demo Mode", profile=profile, status=status, summary=summary, payload=payload, case_fields=defaults)


def create_local_snapshot(custom_name: Optional[str] = None, case_fields: Optional[Dict[str, str]] = None) -> int:
    payload = get_basic_local_payload()
    hostname = payload.get("system", {}).get("hostname", "Local Endpoint")
    name = custom_name.strip() if custom_name and custom_name.strip() else f"Local Snapshot - {hostname} - {datetime.now().strftime('%H:%M:%S')}"
    status = payload.get("status", "Unknown")
    score = payload.get("health_score", "N/A")
    summary = f"Local endpoint, network, storage, service, and printer diagnostic snapshot. Health score: {score}. Status: {status}."
    defaults = default_case_fields_for_profile("local_services", "Local Endpoint Review", "Local Mode", name)
    if case_fields:
        defaults.update({key: value for key, value in case_fields.items() if clean_text(value)})
    return insert_snapshot(name=name, mode="Local Mode", profile="local_services", status=status, summary=summary, payload=payload, case_fields=defaults)

def get_portfolio_demo_snapshots() -> List[sqlite3.Row]:
    with get_db_connection() as conn:
        return list(
            conn.execute(
                "SELECT * FROM snapshots WHERE name LIKE ? ORDER BY id ASC",
                (f"{PORTFOLIO_DEMO_PREFIX}%",),
            ).fetchall()
        )


def get_portfolio_demo_snapshot_by_name(name: str) -> Optional[sqlite3.Row]:
    with get_db_connection() as conn:
        return conn.execute("SELECT * FROM snapshots WHERE name = ? ORDER BY id DESC LIMIT 1", (name,)).fetchone()


def seed_portfolio_demo_snapshots() -> Dict[str, Any]:
    scenarios = [
        ("healthy", f"{PORTFOLIO_DEMO_PREFIX}Healthy Baseline"),
        ("dns_issue", f"{PORTFOLIO_DEMO_PREFIX}DNS Failure Before Fix"),
        ("printer_issue", f"{PORTFOLIO_DEMO_PREFIX}Printer Failure Before Fix"),
        ("slow_pc", f"{PORTFOLIO_DEMO_PREFIX}Slow PC Investigation"),
        ("vpn_issue", f"{PORTFOLIO_DEMO_PREFIX}VPN Route Issue"),
    ]
    created_ids: List[int] = []
    skipped_names: List[str] = []

    for profile, name in scenarios:
        if get_portfolio_demo_snapshot_by_name(name):
            skipped_names.append(name)
            continue
        created_ids.append(create_demo_snapshot(profile=profile, custom_name=name))

    return {
        "created_count": len(created_ids),
        "created_ids": created_ids,
        "skipped_count": len(skipped_names),
        "skipped_names": skipped_names,
    }


def clear_portfolio_demo_snapshots() -> int:
    with get_db_connection() as conn:
        cursor = conn.execute("DELETE FROM snapshots WHERE name LIKE ?", (f"{PORTFOLIO_DEMO_PREFIX}%",))
        conn.commit()
        return int(cursor.rowcount or 0)


def clear_local_snapshots() -> int:
    """Delete only snapshots created from Local Mode diagnostics."""
    with get_db_connection() as conn:
        cursor = conn.execute("DELETE FROM snapshots WHERE mode = ?", ("Local Mode",))
        conn.commit()
        return int(cursor.rowcount or 0)


def clear_all_snapshots() -> int:
    """Delete every saved snapshot and reset SQLite auto-increment IDs.

    This is useful before publishing a clean GitHub portfolio version.
    It does not delete the application code; it only clears saved diagnostic records.
    """
    with get_db_connection() as conn:
        cursor = conn.execute("DELETE FROM snapshots")
        deleted = int(cursor.rowcount or 0)
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", ("snapshots",))
        conn.commit()
        return deleted


def portfolio_demo_pair(profile_name: str, healthy_name: str = f"{PORTFOLIO_DEMO_PREFIX}Healthy Baseline") -> Tuple[Optional[int], Optional[int]]:
    before = get_portfolio_demo_snapshot_by_name(profile_name)
    after = get_portfolio_demo_snapshot_by_name(healthy_name)
    before_id = int(before["id"]) if before else None
    after_id = int(after["id"]) if after else None
    return before_id, after_id


# -----------------------------
# Compare helpers
# -----------------------------



def profile_label(profile: str) -> str:
    labels = {
        "healthy": "Healthy Endpoint",
        "dns_issue": "DNS Issue",
        "printer_issue": "Printer Issue",
        "slow_pc": "Slow PC",
        "vpn_issue": "VPN Issue",
        "local_services": "Local Mode Snapshot",
    }
    return labels.get(profile or "", humanize_key(profile or "Unknown"))


def is_flexible_comparison_profile(profile: str, snapshot_name: str = "") -> bool:
    """Profiles that should remain available as after snapshots for most comparisons.

    Healthy, local, and baseline snapshots are useful for validating that a system returned
    to a good state or for comparing an issue snapshot with a general machine baseline.
    """
    normalized_profile = (profile or "").lower()
    normalized_name = (snapshot_name or "").lower()
    return (
        normalized_profile in {"healthy", "local_services", "baseline"}
        or "baseline" in normalized_name
        or "healthy" in normalized_name
    )


def should_show_as_after_option(before_row: sqlite3.Row, candidate_row: sqlite3.Row) -> bool:
    """Smart filter for the After snapshot dropdown.

    If the before snapshot is already healthy/local/baseline, the technician may be doing
    full-system comparison, so all snapshots remain available. Otherwise, the after list
    focuses on the same issue type plus healthy/local/baseline states.
    """
    before_profile = before_row["profile"]
    candidate_profile = candidate_row["profile"]

    if is_flexible_comparison_profile(before_profile, before_row["name"]):
        return True

    if candidate_row["id"] == before_row["id"]:
        return False

    return (
        candidate_profile == before_profile
        or is_flexible_comparison_profile(candidate_profile, candidate_row["name"])
    )


def smart_after_options(rows: List[sqlite3.Row], before_row: Optional[sqlite3.Row]) -> List[sqlite3.Row]:
    if before_row is None:
        return rows
    filtered = [row for row in rows if should_show_as_after_option(before_row, row)]
    return filtered if filtered else rows


def comparison_context_warning(left_row: Optional[sqlite3.Row], right_row: Optional[sqlite3.Row]) -> Optional[str]:
    if not left_row or not right_row:
        return None

    left_profile = left_row["profile"]
    right_profile = right_row["profile"]

    if left_profile == right_profile:
        return None

    if is_flexible_comparison_profile(right_profile, right_row["name"]):
        return None

    if is_flexible_comparison_profile(left_profile, left_row["name"]):
        return None

    return (
        "These snapshots are from different issue categories. This can still be useful for "
        "full-system change analysis, but it may not validate one specific troubleshooting fix."
    )


def snapshot_display_label(row: sqlite3.Row) -> str:
    case_label = clean_text(row["case_label"])
    case_part = f" | Case: {case_label}" if case_label else ""
    purpose = clean_text(row["snapshot_purpose"])
    purpose_part = f" | {purpose}" if purpose else ""
    return f"#{row['id']} - {row['name']} - {profile_label(row['profile'])} - {row['mode']}{case_part}{purpose_part}"


def snapshot_option_payload(row: sqlite3.Row, selected_before_id: Optional[int]) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "label": snapshot_display_label(row),
        "profile": row["profile"],
        "isFlexible": is_flexible_comparison_profile(row["profile"], row["name"]),
        "isSameAsBefore": bool(selected_before_id and int(row["id"]) == selected_before_id),
        "name": row["name"],
        "mode": row["mode"],
        "caseLabel": clean_text(row["case_label"]),
        "purpose": clean_text(row["snapshot_purpose"]),
    }

def humanize_key(key: str) -> str:
    """Convert machine-style keys into readable labels."""
    cleaned = str(key).strip().replace("_", " ").replace("-", " ")
    if not cleaned:
        return "Value"
    special = {
        "ip": "IP",
        "dns": "DNS",
        "dhcp": "DHCP",
        "os": "OS",
        "url": "URL",
        "id": "ID",
    }
    words = []
    for word in cleaned.split():
        words.append(special.get(word.lower(), word.capitalize()))
    return " ".join(words)


def format_named_count_list(items: List[Dict[str, Any]], name_key: str, count_key: str, unit: str) -> str:
    parts: List[str] = []
    for item in items:
        name = item.get(name_key, "Unknown")
        count = item.get(count_key, 0)
        try:
            count_number = int(count)
            count_text = f"{count_number} {unit}" if count_number == 1 else f"{count_number} {unit}s"
        except Exception:
            count_text = f"{count} {unit}s"
        parts.append(f"{name}: {count_text}")
    return "; ".join(parts) if parts else "None"


def format_printer_list(items: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for printer in items:
        name = printer.get("name") or printer.get("Name") or "Unknown printer"
        availability = printer.get("availability") or printer.get("PrinterStatus") or printer.get("Status") or "Unknown"
        queue = printer.get("queue_count", printer.get("JobCount", 0))
        offline_value = printer.get("is_offline", printer.get("WorkOffline", False))
        offline = "offline" if bool(offline_value) else "online/ready"
        parts.append(f"{name}: {availability}, {offline}, queue {queue}")
    return "; ".join(parts) if parts else "None"


def format_service_list(items: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for service in items:
        name = service.get("display_name") or service.get("service_name") or service.get("Name") or "Unknown service"
        status = service.get("status") or service.get("Status") or "Unknown"
        result = service.get("result") or service.get("Result")
        parts.append(f"{name}: {status}" + (f" ({result})" if result else ""))
    return "; ".join(parts) if parts else "None"


def display_value(value: Any) -> str:
    """Return technician-friendly text for values shown in the main UI and reports.

    Raw JSON is still preserved in the advanced payload and JSON export.
    """
    if value is None or value == "":
        return "Unavailable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "None"
        if all(isinstance(item, dict) for item in value):
            dict_items = [item for item in value if isinstance(item, dict)]
            keys = set().union(*(item.keys() for item in dict_items)) if dict_items else set()

            if {"Name", "JobCount"}.issubset(keys):
                return format_named_count_list(dict_items, "Name", "JobCount", "job")
            if {"name", "queue_count"}.issubset(keys) or {"Name", "PrinterStatus"}.issubset(keys):
                return format_printer_list(dict_items)
            if {"display_name", "status"}.issubset(keys) or {"service_name", "status"}.issubset(keys):
                return format_service_list(dict_items)

            formatted_items = []
            for item in dict_items:
                formatted_items.append(
                    ", ".join(f"{humanize_key(k)}: {display_value(v)}" for k, v in item.items())
                )
            return "; ".join(formatted_items)

        return "; ".join(display_value(item) for item in value)
    if isinstance(value, dict):
        if "status" in value:
            status = display_value(value.get("status"))
            detail_parts = []
            for detail_key in ("target", "host", "server", "message", "reason", "error"):
                if detail_key in value and value.get(detail_key):
                    detail_parts.append(f"{humanize_key(detail_key)}: {display_value(value.get(detail_key))}")
            return status if not detail_parts else f"{status} ({'; '.join(detail_parts)})"

        preferred_keys = [
            "name", "Name", "display_name", "service_name", "availability", "PrinterStatus",
            "Status", "queue_count", "JobCount", "offline_reason", "result", "message",
        ]
        parts = []
        used = set()
        for key in preferred_keys:
            if key in value:
                parts.append(f"{humanize_key(key)}: {display_value(value[key])}")
                used.add(key)
        for key, item_value in value.items():
            if key not in used:
                parts.append(f"{humanize_key(key)}: {display_value(item_value)}")
        return "; ".join(parts) if parts else "Unavailable"
    return str(value)



def get_network_ip_display(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "network.primary_local_ip", get_nested_value(payload, "network.ip_address", "Unavailable"))


def get_adapter_display(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "network.adapter", get_nested_value(payload, "network.adapter_summary.source", "Unavailable"))


def get_windows_update_status(payload: Dict[str, Any]) -> Any:
    return get_service_status(payload, "Windows Update", "windows_update")


def get_dhcp_client_status(payload: Dict[str, Any]) -> Any:
    return get_service_status(payload, "DHCP Client", "dhcp_client")


def get_dns_client_status(payload: Dict[str, Any]) -> Any:
    return get_service_status(payload, "DNS Client", "dns_client")


def get_workstation_status(payload: Dict[str, Any]) -> Any:
    return get_service_status(payload, "Workstation", "workstation")


def get_printer_readiness(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "printers.readiness_summary", get_nested_value(payload, "printers.last_test_page", "Unavailable"))


def get_installed_printer_count(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "printers.installed_printer_count", "Unavailable")


def get_vpn_route_check(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "network.internal_route_check", "Unavailable")




def get_endpoint_uptime(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "endpoint.uptime.uptime", get_nested_value(payload, "system.uptime", "Unavailable"))


def get_memory_status(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "endpoint.memory.status", "Unavailable")


def get_memory_free(payload: Dict[str, Any]) -> Any:
    free_mb = get_nested_value(payload, "endpoint.memory.free_mb", "Unavailable")
    free_percent = get_nested_value(payload, "endpoint.memory.free_percent", "Unavailable")
    if free_mb == "Unavailable" and free_percent == "Unavailable":
        return "Unavailable"
    return f"{free_mb} MB free ({free_percent}%)"


def get_process_count(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "endpoint.processes.process_count", "Unavailable")


def get_recent_event_display(payload: Dict[str, Any]) -> Any:
    event_count = get_nested_value(payload, "endpoint.recent_system_events.event_count", "Unavailable")
    error_count = get_nested_value(payload, "endpoint.recent_system_events.error_count", "Unavailable")
    warning_count = get_nested_value(payload, "endpoint.recent_system_events.warning_count", "Unavailable")
    if event_count == "Unavailable":
        return "Unavailable"
    return f"{event_count} recent warnings/errors ({error_count} errors, {warning_count} warnings)"


def get_battery_display(payload: Dict[str, Any]) -> Any:
    battery = get_nested_value(payload, "endpoint.battery", {})
    if not isinstance(battery, dict):
        return "Unavailable"
    status = battery.get("status", "Unavailable")
    charge = battery.get("charge_percent", "Unavailable")
    if charge == "Unavailable":
        return status
    return f"{status} ({charge}%)"

def get_issue_focus(profile: str, payload: Dict[str, Any], mode: str = "") -> Dict[str, Any]:
    """Build the technician-facing focus section for a snapshot.

    The app still captures a full endpoint snapshot. This helper decides which
    checks should be promoted near the top based on why the snapshot was taken.
    """
    normalized = (profile or "").lower()

    focus_map: Dict[str, Dict[str, Any]] = {
        "dns_issue": {
            "title": "DNS Issue Focus",
            "description": "These checks help separate a DNS/name-resolution problem from a wider internet or gateway problem.",
            "checks": [
                ("Local IP", get_network_ip_display(payload)),
                ("Default Gateway", get_nested_value(payload, "network.default_gateway", get_nested_value(payload, "network.gateway"))),
                ("DNS Servers", get_dns_server_display(payload)),
                ("Gateway Ping", get_gateway_ping(payload)),
                ("Internet Ping", get_internet_ping(payload)),
                ("DNS Lookup", get_dns_lookup(payload)),
                ("Web Connectivity", get_web_connectivity(payload)),
                ("DNS Client Service", get_dns_client_status(payload)),
            ],
            "interpretation": [
                "If internet ping passes but DNS lookup fails, focus on DNS server settings or DNS service behavior.",
                "If gateway ping fails too, investigate local network connection before treating it as a DNS-only issue.",
            ],
        },
        "printer_issue": {
            "title": "Printer Issue Focus",
            "description": "These checks show whether printing is blocked by the OS print service, printer availability, offline state, or stuck jobs.",
            "checks": [
                ("Print Service", get_print_service(payload)),
                ("Printer Result", get_printer_result(payload)),
                ("Default Printer", get_default_printer(payload)),
                ("Installed Printers", get_installed_printer_count(payload)),
                ("Offline Printers", get_offline_printer_count(payload)),
                ("Queued Print Jobs", get_queue_total(payload)),
                ("Printer Readiness", get_printer_readiness(payload)),
            ],
            "interpretation": [
                "A running Print Spooler means the OS print service is available; it does not prove a printer is connected or reachable.",
                "Offline printers or queued jobs should be reviewed before escalating to a print server or network team.",
            ],
        },
        "slow_pc": {
            "title": "Slow PC Focus",
            "description": "These checks highlight common endpoint causes of slowness while preserving the full system snapshot for context.",
            "checks": [
                ("Health Score", payload.get("health_score", "Unavailable")),
                ("Overall Status", payload.get("status", "Unavailable")),
                ("Storage Status", get_storage_status(payload)),
                ("Storage Free", get_storage_free(payload)),
                ("Memory Status", get_memory_status(payload)),
                ("Memory Free", get_memory_free(payload)),
                ("Uptime", get_endpoint_uptime(payload)),
                ("Process Count", get_process_count(payload)),
                ("Recent System Events", get_recent_event_display(payload)),
                ("Windows Update", get_windows_update_status(payload)),
                ("Service Review Items", get_service_review_count(payload)),
                ("Web Connectivity", get_web_connectivity(payload)),
            ],
            "interpretation": [
                "Low disk space, update activity, or service review items can explain user-reported slowness.",
                "If storage and services look healthy, compare with a later snapshot to identify what changed over time.",
            ],
        },
        "vpn_issue": {
            "title": "VPN Issue Focus",
            "description": "These checks help confirm whether the device has general connectivity but is missing internal VPN access or routes.",
            "checks": [
                ("Adapter", get_adapter_display(payload)),
                ("Local/VPN IP", get_network_ip_display(payload)),
                ("Default Gateway", get_nested_value(payload, "network.default_gateway", get_nested_value(payload, "network.gateway"))),
                ("DNS Servers", get_dns_server_display(payload)),
                ("DNS Lookup", get_dns_lookup(payload)),
                ("Web Connectivity", get_web_connectivity(payload)),
                ("Internal Route Check", get_vpn_route_check(payload)),
                ("Workstation Service", get_workstation_status(payload)),
            ],
            "interpretation": [
                "If public internet works but internal routes fail, the VPN may be connected without the routes needed for internal resources.",
                "Missing routes or internal DNS issues are good escalation evidence for a network/VPN team.",
            ],
        },
        "healthy": {
            "title": "Healthy / Baseline Focus",
            "description": "This snapshot is useful as a comparison point before or after troubleshooting.",
            "checks": [
                ("Health Score", payload.get("health_score", "Unavailable")),
                ("Overall Status", payload.get("status", "Unavailable")),
                ("Local IP", get_network_ip_display(payload)),
                ("DNS Lookup", get_dns_lookup(payload)),
                ("Storage Status", get_storage_status(payload)),
                ("Print Service", get_print_service(payload)),
                ("Printer Result", get_printer_result(payload)),
                ("Service Review Items", get_service_review_count(payload)),
            ],
            "interpretation": [
                "Use this as a baseline or after-fix snapshot to prove the endpoint returned to a healthy state.",
                "Compare it with an issue snapshot to show what improved after troubleshooting.",
            ],
        },
        "local_services": {
            "title": "Local Endpoint Focus",
            "description": "This local snapshot captures the current machine state for real technician troubleshooting.",
            "checks": [
                ("Hostname", get_nested_value(payload, "system.hostname")),
                ("Logged-in User", get_nested_value(payload, "system.logged_in_user")),
                ("Health Score", payload.get("health_score", "Unavailable")),
                ("Uptime", get_endpoint_uptime(payload)),
                ("Memory Status", get_memory_status(payload)),
                ("Local IP", get_network_ip_display(payload)),
                ("Default Gateway", get_nested_value(payload, "network.default_gateway", get_nested_value(payload, "network.gateway"))),
                ("DNS Lookup", get_dns_lookup(payload)),
                ("Storage Status", get_storage_status(payload)),
                ("Print Service", get_print_service(payload)),
                ("Printer Result", get_printer_result(payload)),
                ("Service Review Items", get_service_review_count(payload)),
            ],
            "interpretation": [
                "Use this as a real endpoint snapshot before a fix, after a fix, or as a baseline for future comparison.",
                "The full sections below still include network, service, printer, storage, and raw diagnostic details.",
            ],
        },
    }

    focus = focus_map.get(normalized, focus_map["local_services"] if mode == "Local Mode" else focus_map["healthy"])
    return {
        "title": focus["title"],
        "description": focus["description"],
        "checks": [(label, display_value(value)) for label, value in focus["checks"]],
        "interpretation": focus["interpretation"],
    }

def flatten_payload(data: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
    flattened: Dict[str, str] = {}
    for key, value in data.items():
        new_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flattened.update(flatten_payload(value, new_key))
        else:
            flattened[new_key] = display_value(value)
    return flattened


def compare_payloads(left: Dict[str, Any], right: Dict[str, Any]) -> List[Dict[str, str]]:
    left_flat = flatten_payload(left)
    right_flat = flatten_payload(right)
    keys = sorted(set(left_flat.keys()) | set(right_flat.keys()))

    differences = []
    for key in keys:
        left_value = left_flat.get(key, "Missing")
        right_value = right_flat.get(key, "Missing")
        if str(left_value) != str(right_value):
            differences.append({"field": key, "left": display_value(left_value), "right": display_value(right_value)})
    return differences


def numeric_value(value: Any) -> Optional[float]:
    if value is None or value == "" or value == "Missing":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def status_rank(value: Any) -> Optional[int]:
    text_value = display_value(value).strip().lower()
    if text_value in {"missing", "unavailable", "unknown", "not checked", "not checked yet"}:
        return None
    good_terms = [
        "pass",
        "healthy",
        "running",
        "ready",
        "available",
        "normal",
        "successful",
        "idle",
        "no offline signal",
    ]
    bad_terms = [
        "fail",
        "failed",
        "needs attention",
        "review",
        "stopped",
        "offline",
        "low disk",
        "error",
        "disabled",
        "jammed",
    ]
    if any(term in text_value for term in bad_terms):
        return 0
    if any(term in text_value for term in good_terms):
        return 2
    if text_value in {"0", "none", "no"}:
        return 2
    return 1


def get_nested_value(payload: Dict[str, Any], path: str, default: Any = "Unavailable") -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def extract_status(payload: Dict[str, Any], path: str, default: Any = "Unavailable") -> Any:
    value = get_nested_value(payload, path, default)
    if isinstance(value, dict):
        return value.get("status", default)
    return value


def get_service_status(payload: Dict[str, Any], display_name: str, demo_key: str = "") -> Any:
    services = payload.get("services", {})
    if isinstance(services, dict) and isinstance(services.get("checks"), list):
        for item in services.get("checks", []):
            if isinstance(item, dict) and item.get("display_name") == display_name:
                return item.get("status", "Unavailable")
    if demo_key and isinstance(services, dict):
        return services.get(demo_key, "Unavailable")
    return "Unavailable"


def get_service_review_count(payload: Dict[str, Any]) -> Any:
    services = payload.get("services", {})
    if isinstance(services, dict) and isinstance(services.get("checks"), list):
        return sum(1 for item in services.get("checks", []) if isinstance(item, dict) and item.get("result") == "Review")
    return "Unavailable"


def get_dns_server_display(payload: Dict[str, Any]) -> Any:
    network = payload.get("network", {})
    if not isinstance(network, dict):
        return "Unavailable"
    value = network.get("dns_servers", "Unavailable")
    return value


def get_gateway_ping(payload: Dict[str, Any]) -> Any:
    return extract_status(payload, "network.gateway_ping")


def get_internet_ping(payload: Dict[str, Any]) -> Any:
    return extract_status(payload, "network.internet_ping")


def get_dns_lookup(payload: Dict[str, Any]) -> Any:
    return extract_status(payload, "network.dns_lookup")


def get_web_connectivity(payload: Dict[str, Any]) -> Any:
    return extract_status(payload, "network.web_connectivity")


def get_storage_status(payload: Dict[str, Any]) -> Any:
    value = get_nested_value(payload, "storage.status", None)
    if value is not None:
        return value
    return get_nested_value(payload, "storage.system_drive_status", "Unavailable")


def get_storage_free(payload: Dict[str, Any]) -> Any:
    value = get_nested_value(payload, "storage.free_gb", None)
    if value is not None:
        return f"{value} GB"
    return get_nested_value(payload, "storage.system_drive_free", "Unavailable")


def get_print_service(payload: Dict[str, Any]) -> Any:
    printers = payload.get("printers", {})
    if isinstance(printers, dict) and "print_service_status" in printers:
        return printers.get("print_service_status")
    return get_service_status(payload, "Print Spooler", "print_spooler")


def get_printer_result(payload: Dict[str, Any]) -> Any:
    printers = payload.get("printers", {})
    if isinstance(printers, dict) and "result" in printers:
        return printers.get("result")
    if isinstance(printers, dict):
        return printers.get("printer_status", "Unavailable")
    return "Unavailable"


def get_offline_printer_count(payload: Dict[str, Any]) -> Any:
    printers = payload.get("printers", {})
    if isinstance(printers, dict) and "offline_printer_count" in printers:
        return printers.get("offline_printer_count")
    if isinstance(printers, dict) and str(printers.get("printer_status", "")).lower() == "offline":
        return 1
    return "Unavailable"


def get_queue_total(payload: Dict[str, Any]) -> Any:
    printers = payload.get("printers", {})
    if isinstance(printers, dict) and "queued_jobs_total" in printers:
        return printers.get("queued_jobs_total")
    if isinstance(printers, dict) and "queue_depth" in printers:
        return printers.get("queue_depth")
    return "Unavailable"


def get_default_printer(payload: Dict[str, Any]) -> Any:
    return get_nested_value(payload, "printers.default_printer", "Unavailable")


def comparison_outcome(label: str, left: Any, right: Any, kind: str) -> Tuple[str, str]:
    left_text = display_value(left)
    right_text = display_value(right)
    if left_text == right_text:
        return "No Change", "No meaningful change detected for this check."

    if kind == "score_higher_better":
        left_num = numeric_value(left)
        right_num = numeric_value(right)
        if left_num is None or right_num is None:
            return "Changed", "Health score changed, but one side could not be converted to a number."
        diff = right_num - left_num
        if diff > 0:
            return "Improved", f"Health score increased by {diff:g} points."
        if diff < 0:
            return "Worsened", f"Health score decreased by {abs(diff):g} points."
        return "No Change", "Health score stayed the same."

    if kind == "count_lower_better":
        left_num = numeric_value(left)
        right_num = numeric_value(right)
        if left_num is None or right_num is None:
            return "Changed", "Count changed or became unavailable."
        if right_num < left_num:
            return "Improved", f"{label} decreased from {left_text} to {right_text}."
        if right_num > left_num:
            return "Worsened", f"{label} increased from {left_text} to {right_text}."
        return "No Change", f"{label} stayed the same."

    left_rank = status_rank(left)
    right_rank = status_rank(right)
    if left_rank is not None and right_rank is not None:
        if right_rank > left_rank:
            return "Improved", f"{label} moved from {left_text} to {right_text}."
        if right_rank < left_rank:
            return "Worsened", f"{label} moved from {left_text} to {right_text}."

    return "Changed", f"{label} changed from {left_text} to {right_text}."


def build_key_comparison(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    metric_definitions = [
        ("Health Score", lambda p: p.get("health_score", "Unavailable"), "score_higher_better"),
        ("Overall Status", lambda p: p.get("status", "Unavailable"), "status"),
        ("Local IP", lambda p: get_nested_value(p, "network.primary_local_ip", get_nested_value(p, "network.ip_address", "Unavailable")), "status"),
        ("Default Gateway", lambda p: get_nested_value(p, "network.default_gateway", get_nested_value(p, "network.gateway", "Unavailable")), "status"),
        ("DNS Servers", get_dns_server_display, "status"),
        ("Gateway Ping", get_gateway_ping, "status"),
        ("Internet Ping", get_internet_ping, "status"),
        ("DNS Lookup", get_dns_lookup, "status"),
        ("Web Connectivity", get_web_connectivity, "status"),
        ("Storage Status", get_storage_status, "status"),
        ("Storage Free", get_storage_free, "status"),
        ("Print Service", get_print_service, "status"),
        ("Printer Result", get_printer_result, "status"),
        ("Default Printer", get_default_printer, "status"),
        ("Offline Printers", get_offline_printer_count, "count_lower_better"),
        ("Queued Print Jobs", get_queue_total, "count_lower_better"),
        ("Service Review Items", get_service_review_count, "count_lower_better"),
    ]

    rows: List[Dict[str, str]] = []
    for label, extractor, kind in metric_definitions:
        left_value = extractor(left)
        right_value = extractor(right)
        outcome, note = comparison_outcome(label, left_value, right_value, kind)
        rows.append(
            {
                "label": label,
                "left": display_value(left_value),
                "right": display_value(right_value),
                "outcome": outcome,
                "note": note,
            }
        )

    improved = [row for row in rows if row["outcome"] == "Improved"]
    worsened = [row for row in rows if row["outcome"] == "Worsened"]
    changed = [row for row in rows if row["outcome"] == "Changed"]
    no_change = [row for row in rows if row["outcome"] == "No Change"]

    interpretation: List[str] = []
    if improved and not worsened:
        interpretation.append("The second snapshot looks better overall. Key checks improved and no tracked check worsened.")
    elif worsened and not improved:
        interpretation.append("The second snapshot looks worse overall. Review the worsened checks before closing the issue.")
    elif improved and worsened:
        interpretation.append("The comparison is mixed. Some checks improved, but at least one check worsened and should be reviewed.")
    else:
        interpretation.append("No major health improvement or decline was detected in the key checks.")

    for row in improved[:4]:
        interpretation.append(f"Improved: {row['label']} - {row['note']}")
    for row in worsened[:4]:
        interpretation.append(f"Needs review: {row['label']} - {row['note']}")
    if not improved and not worsened and changed:
        for row in changed[:3]:
            interpretation.append(f"Changed: {row['label']} - {row['note']}")

    return {
        "rows": rows,
        "interpretation": interpretation,
        "counts": {
            "improved": len(improved),
            "worsened": len(worsened),
            "changed": len(changed),
            "no_change": len(no_change),
        },
    }



# -----------------------------
# Report export helpers
# -----------------------------

def safe_filename(value: str, fallback: str = "snapshot") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    cleaned = cleaned.strip("._")
    return cleaned[:80] or fallback


def build_report_sections(row: sqlite3.Row, payload: Dict[str, Any]) -> Dict[str, Any]:
    metadata = [
        ("Snapshot ID", row["id"]),
        ("Name", row["name"]),
        ("Mode", row["mode"]),
        ("Profile", row["profile"]),
        ("Status", row["status"]),
        ("Created", row["created_at"]),
        ("Health Score", payload.get("health_score", "Unavailable")),
        ("Case Label", row["case_label"] or profile_label(row["profile"])),
        ("Affected User", row["affected_user"] or "Not set"),
        ("Affected Device", row["affected_device"] or "Not set"),
        ("Snapshot Purpose", row["snapshot_purpose"] or "Not set"),
        ("Resolution Status", row["resolution_status"] or "Not set"),
        ("Root Cause", row["root_cause"] or "Not set"),
        ("Technician Notes", row["technician_notes"] or "No notes recorded"),
    ]

    key_checks = [
        ("Hostname", get_nested_value(payload, "system.hostname")),
        ("Operating System", get_nested_value(payload, "system.operating_system", get_nested_value(payload, "system.os"))),
        ("Logged-in User", get_nested_value(payload, "system.logged_in_user")),
        ("Local IP", get_nested_value(payload, "network.primary_local_ip", get_nested_value(payload, "network.ip_address"))),
        ("Default Gateway", get_nested_value(payload, "network.default_gateway", get_nested_value(payload, "network.gateway"))),
        ("DNS Servers", display_value(get_dns_server_display(payload))),
        ("Gateway Ping", display_value(get_gateway_ping(payload))),
        ("Internet Ping", display_value(get_internet_ping(payload))),
        ("DNS Lookup", display_value(get_dns_lookup(payload))),
        ("Web Connectivity", display_value(get_web_connectivity(payload))),
        ("Storage Status", display_value(get_storage_status(payload))),
        ("Storage Free", display_value(get_storage_free(payload))),
        ("Print Service", display_value(get_print_service(payload))),
        ("Printer Result", display_value(get_printer_result(payload))),
        ("Default Printer", display_value(get_default_printer(payload))),
        ("Offline Printers", display_value(get_offline_printer_count(payload))),
        ("Queued Print Jobs", display_value(get_queue_total(payload))),
        ("Service Review Items", display_value(get_service_review_count(payload))),
    ]

    service_checks = []
    services = payload.get("services", {})
    if isinstance(services, dict) and isinstance(services.get("checks"), list):
        service_checks = services.get("checks", [])

    printer_inventory = []
    printers = payload.get("printers", {})
    if isinstance(printers, dict) and isinstance(printers.get("printers"), list):
        printer_inventory = printers.get("printers", [])

    issue_focus = get_issue_focus(row["profile"], payload, row["mode"])

    return {
        "metadata": metadata,
        "summary": row["summary"],
        "findings": payload.get("findings", []),
        "recommended_next_steps": payload.get("recommended_next_steps", []),
        "key_checks": key_checks,
        "issue_focus": issue_focus,
        "service_checks": service_checks,
        "printer_inventory": printer_inventory,
        "payload": payload,
    }


def build_text_report(row: sqlite3.Row, payload: Dict[str, Any]) -> str:
    sections = build_report_sections(row, payload)
    lines = [
        APP_TITLE,
        "=" * len(APP_TITLE),
        APP_VERSION,
        "",
        "Snapshot Metadata",
        "-----------------",
    ]

    for label, value in sections["metadata"]:
        lines.append(f"{label}: {display_value(value)}")

    lines.extend(["", "Summary", "-------", sections["summary"]])

    issue_focus = sections["issue_focus"]
    lines.extend(["", issue_focus["title"], "-" * len(issue_focus["title"]), issue_focus["description"]])
    for label, value in issue_focus["checks"]:
        lines.append(f"{label}: {display_value(value)}")
    if issue_focus["interpretation"]:
        lines.extend(["", "Issue-Focused Interpretation", "----------------------------"])
        for item in issue_focus["interpretation"]:
            lines.append(f"- {item}")

    lines.extend(["", "Key Checks", "----------"])
    for label, value in sections["key_checks"]:
        lines.append(f"{label}: {display_value(value)}")

    lines.extend(["", "Key Findings", "------------"])
    for finding in sections["findings"] or ["No findings were recorded."]:
        lines.append(f"- {finding}")

    lines.extend(["", "Recommended Next Steps", "----------------------"])
    for step in sections["recommended_next_steps"] or ["No next steps were recorded."]:
        lines.append(f"- {step}")

    if sections["service_checks"]:
        lines.extend(["", "Service Diagnostics", "-------------------"])
        for service in sections["service_checks"]:
            name = service.get("display_name", service.get("service_name", "Unknown"))
            status = service.get("status", "Unknown")
            expected = service.get("recommended_state", "Unknown")
            result = service.get("result", "Unknown")
            lines.append(f"- {name}: {status} | Expected: {expected} | Result: {result}")

    if sections["printer_inventory"]:
        lines.extend(["", "Printer Inventory", "-----------------"])
        for printer in sections["printer_inventory"]:
            name = printer.get("name", "Unknown")
            availability = printer.get("availability", "Unknown")
            offline = "Yes" if printer.get("is_offline") else "No"
            queue = printer.get("queue_count", 0)
            reason = printer.get("offline_reason", "Unavailable")
            lines.append(f"- {name}: Availability={availability} | Offline={offline} | Queue={queue} | Reason={reason}")

    lines.extend(["", "Advanced Raw Diagnostic Payload", "-------------------------------", json.dumps(payload, indent=2)])
    return "\n".join(lines)


def build_json_report(row: sqlite3.Row, payload: Dict[str, Any]) -> str:
    document = {
        "application": APP_TITLE,
        "version": APP_VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot": {
            "id": row["id"],
            "name": row["name"],
            "mode": row["mode"],
            "profile": row["profile"],
            "status": row["status"],
            "summary": row["summary"],
            "created_at": row["created_at"],
            "case_label": row["case_label"],
            "affected_user": row["affected_user"],
            "affected_device": row["affected_device"],
            "snapshot_purpose": row["snapshot_purpose"],
            "root_cause": row["root_cause"],
            "resolution_status": row["resolution_status"],
            "technician_notes": row["technician_notes"],
        },
        "issue_focus": get_issue_focus(row["profile"], payload, row["mode"]),
        "payload": payload,
    }
    return json.dumps(document, indent=2)


def build_html_report(row: sqlite3.Row, payload: Dict[str, Any]) -> str:
    sections = build_report_sections(row, payload)

    def table_rows(items: List[Tuple[str, Any]]) -> str:
        return "\n".join(
            f"<tr><th>{escape(str(label))}</th><td>{escape(display_value(value))}</td></tr>"
            for label, value in items
        )

    findings_html = "".join(f"<li>{escape(str(item))}</li>" for item in sections["findings"])
    if not findings_html:
        findings_html = "<li>No findings were recorded.</li>"

    steps_html = "".join(f"<li>{escape(str(item))}</li>" for item in sections["recommended_next_steps"])
    if not steps_html:
        steps_html = "<li>No next steps were recorded.</li>"

    services_html = ""
    if sections["service_checks"]:
        service_rows = []
        for service in sections["service_checks"]:
            service_rows.append(
                "<tr>"
                f"<td>{escape(str(service.get('display_name', service.get('service_name', 'Unknown'))))}</td>"
                f"<td>{escape(str(service.get('status', 'Unknown')))}</td>"
                f"<td>{escape(str(service.get('recommended_state', 'Unknown')))}</td>"
                f"<td>{escape(str(service.get('result', 'Unknown')))}</td>"
                "</tr>"
            )
        services_html = """
        <section>
            <h2>Service Diagnostics</h2>
            <table>
                <thead><tr><th>Service</th><th>Status</th><th>Expected</th><th>Result</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </section>
        """.format(rows="".join(service_rows))

    printers_html = ""
    if sections["printer_inventory"]:
        printer_rows = []
        for printer in sections["printer_inventory"]:
            printer_rows.append(
                "<tr>"
                f"<td>{escape(str(printer.get('name', 'Unknown')))}</td>"
                f"<td>{escape(str(printer.get('availability', 'Unknown')))}</td>"
                f"<td>{'Yes' if printer.get('is_offline') else 'No'}</td>"
                f"<td>{escape(str(printer.get('queue_count', 0)))}</td>"
                f"<td>{escape(str(printer.get('offline_reason', 'Unavailable')))}</td>"
                "</tr>"
            )
        printers_html = """
        <section>
            <h2>Printer Inventory</h2>
            <table>
                <thead><tr><th>Printer</th><th>Availability</th><th>Offline</th><th>Queue</th><th>Offline Reason</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </section>
        """.format(rows="".join(printer_rows))

    payload_html = escape(json.dumps(payload, indent=2))
    return f"""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{escape(row['name'])} - Technician Report</title>
    <style>
        body {{ font-family: Arial, Helvetica, sans-serif; margin: 32px; color: #172033; }}
        h1 {{ margin-bottom: 4px; }}
        .muted {{ color: #637083; }}
        section {{ margin-top: 24px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ border: 1px solid #dde3ee; padding: 9px; text-align: left; vertical-align: top; }}
        th {{ background: #f0f4fa; }}
        pre {{ background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 8px; white-space: pre-wrap; }}
        @media print {{ body {{ margin: 18px; }} .no-print {{ display: none; }} }}
    </style>
</head>
<body>
    <h1>{escape(APP_TITLE)}</h1>
    <p class="muted">{escape(APP_VERSION)} | Exported {escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>
    <button class="no-print" onclick="window.print()">Print / Save as PDF</button>

    <section>
        <h2>Snapshot Metadata</h2>
        <table>{table_rows(sections['metadata'])}</table>
    </section>

    <section>
        <h2>Summary</h2>
        <p>{escape(sections['summary'])}</p>
    </section>

    <section>
        <h2>{escape(sections['issue_focus']['title'])}</h2>
        <p>{escape(sections['issue_focus']['description'])}</p>
        <table>{table_rows(sections['issue_focus']['checks'])}</table>
        <ul>{''.join(f"<li>{escape(str(item))}</li>" for item in sections['issue_focus']['interpretation'])}</ul>
    </section>

    <section>
        <h2>Key Checks</h2>
        <table>{table_rows(sections['key_checks'])}</table>
    </section>

    <section>
        <h2>Key Findings</h2>
        <ul>{findings_html}</ul>
    </section>

    <section>
        <h2>Recommended Next Steps</h2>
        <ul>{steps_html}</ul>
    </section>

    {services_html}
    {printers_html}

    <section>
        <h2>Advanced Raw Diagnostic Payload</h2>
        <pre>{payload_html}</pre>
    </section>
</body>
</html>
""".strip()


def bundle_filename(row: sqlite3.Row) -> str:
    return f"snapshot_{row['id']}_{safe_filename(row['name'])}"

# -----------------------------
# HTML templates
# -----------------------------

BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{{ title or app_title }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --bg: #f5f7fb;
            --panel: #ffffff;
            --text: #172033;
            --muted: #637083;
            --border: #dde3ee;
            --accent: #1f5eff;
            --good: #147a3e;
            --warn: #a15c00;
            --bad: #a52828;
            --soft: #eef3ff;
            --dark: #111827;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: Arial, Helvetica, sans-serif;
            line-height: 1.5;
        }
        header {
            background: var(--dark);
            color: white;
            padding: 18px 28px;
        }
        header h1 { margin: 0; font-size: 22px; }
        header p { margin: 4px 0 0; color: #cbd5e1; }
        nav {
            background: white;
            border-bottom: 1px solid var(--border);
            padding: 10px 28px;
            display: flex;
            gap: 14px;
            flex-wrap: wrap;
        }
        nav a {
            color: #172033;
            text-decoration: none;
            font-weight: 700;
            font-size: 14px;
        }
        nav a:hover { color: var(--accent); }
        main {
            max-width: 1120px;
            margin: 24px auto;
            padding: 0 18px 40px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 16px;
        }
        .card {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 18px;
            box-shadow: 0 6px 18px rgba(15, 23, 42, .05);
        }
        .card h2, .card h3 { margin-top: 0; }
        .metric { font-size: 32px; font-weight: 800; margin: 8px 0; }
        .muted { color: var(--muted); }
        .status {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 800;
        }
        .status-good { background: #e7f7ed; color: var(--good); }
        .status-warn { background: #fff4df; color: var(--warn); }
        .status-bad { background: #fdecec; color: var(--bad); }
        .pill {
            display: inline-block;
            background: #eef3ff;
            color: #2447a6;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 800;
        }
        .button, button {
            display: inline-block;
            background: var(--accent);
            color: white;
            padding: 10px 14px;
            border: 0;
            border-radius: 10px;
            text-decoration: none;
            font-weight: 800;
            cursor: pointer;
        }
        .button.secondary { background: #e7ebf3; color: #172033; }
        table {
            width: 100%;
            border-collapse: collapse;
            background: white;
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
        }
        th, td {
            padding: 12px;
            border-bottom: 1px solid var(--border);
            text-align: left;
            vertical-align: top;
        }
        th { background: #f0f4fa; font-size: 13px; }
        tr:last-child td { border-bottom: 0; }
        label { display: block; font-weight: 800; margin: 12px 0 6px; }
        input, select {
            width: 100%;
            max-width: 560px;
            padding: 10px;
            border: 1px solid var(--border);
            border-radius: 10px;
            font-size: 15px;
        }
        pre {
            white-space: pre-wrap;
            word-break: break-word;
            background: #0f172a;
            color: #e2e8f0;
            padding: 14px;
            border-radius: 12px;
            overflow-x: auto;
        }
        .flash {
            background: var(--soft);
            border: 1px solid #c8d7ff;
            padding: 12px 14px;
            border-radius: 12px;
            margin-bottom: 16px;
        }
        .split {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 16px;
        }
        .small { font-size: 13px; }
        .comparison-note {
            background: #f8fafc;
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 12px 14px;
        }
        .outcome-good { background: #e7f7ed; color: var(--good); }
        .outcome-warn { background: #fff4df; color: var(--warn); }
        .outcome-bad { background: #fdecec; color: var(--bad); }
        .outcome-info { background: #eef3ff; color: #2447a6; }
        details summary {
            cursor: pointer;
            font-weight: 800;
            margin: 8px 0;
        }
    </style>
</head>
<body>
<header>
    <h1>{{ app_title }}</h1>
    <p>{{ app_version }} | Local Flask + SQLite technician diagnostic foundation{% if tcc_demo_only %} | Public Demo Only{% endif %}</p>
</header>

<nav>
    <a href="{{ url_for('dashboard') }}">Dashboard</a>
    <a href="{{ url_for('portfolio_demo') }}">Portfolio Demo</a>
    <a href="{{ url_for('run_snapshot') }}">Run Snapshot</a>
    <a href="{{ url_for('snapshots') }}">Snapshot History</a>
    <a href="{{ url_for('compare') }}">Compare</a>
    <a href="{{ url_for('reports') }}">Reports</a>
    <a href="{{ url_for('deployment_guide') }}">Deployment Guide</a>
    <a href="{{ url_for('portfolio_readiness') }}">Portfolio Readiness</a>
</nav>

<main>
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            {% for message in messages %}
                <div class="flash">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    {{ content|safe }}
</main>
</body>
</html>
"""


def page(content: str, **context: Any) -> str:
    return render_template_string(
        BASE_TEMPLATE,
        content=content,
        app_title=APP_TITLE,
        app_version=APP_VERSION,
        tcc_demo_only=TCC_DEMO_ONLY,
        app_environment=TCC_APP_ENV,
        **context,
    )


def status_class(status: str) -> str:
    normalized = (status or "").lower()
    if "healthy" in normalized:
        return "status-good"
    if "attention" in normalized or "warning" in normalized:
        return "status-warn"
    return "status-bad"


def comparison_class(outcome: str) -> str:
    normalized = (outcome or "").lower()
    if "improved" in normalized or "no change" in normalized:
        return "outcome-good"
    if "worsened" in normalized:
        return "outcome-bad"
    if "changed" in normalized:
        return "outcome-warn"
    return "outcome-info"




# -----------------------------
# Dashboard insight helpers
# -----------------------------

def get_nested_value(data: Dict[str, Any], *path: Any, default: Any = "Unavailable") -> Any:
    """Read nested values from diagnostic payloads.

    This helper supports both formats used in the app:
    - get_nested_value(payload, "system.hostname", "Unavailable")
    - get_nested_value(payload, "services", "summary", "review_count", default=0)

    Step 12.1 fix: the dashboard helper previously replaced the dotted-path
    helper, so focus cards could show values as Unavailable even when the
    Local Snapshot Summary had collected them correctly.
    """
    if not path:
        return default

    # Backward-compatible style: dotted path plus positional default.
    if len(path) == 2 and isinstance(path[0], str) and "." in path[0]:
        keys = path[0].split(".")
        default_value = path[1]
    else:
        keys = []
        for item in path:
            if isinstance(item, str) and "." in item:
                keys.extend(item.split("."))
            else:
                keys.append(item)
        default_value = default

    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default_value
        current = current[key]
    return current


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).strip())
    except Exception:
        return default


def status_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("status", "result", "availability", "readiness_summary"):
            if key in value and value.get(key) not in (None, ""):
                return str(value.get(key))
        return friendly_format_value(value)
    if value in (None, ""):
        return "Unknown"
    return str(value)


def is_problem_status(value: Any) -> bool:
    text = status_text(value).strip().lower()
    if not text or text in {"unknown", "unavailable", "not checked", "not tested"}:
        return False
    healthy_words = ["pass", "healthy", "running", "ready", "available", "normal", "successful", "no issue"]
    if any(word in text for word in healthy_words):
        return False
    review_words = ["fail", "failed", "review", "attention", "offline", "stopped", "low", "error", "missing", "unreachable"]
    return any(word in text for word in review_words)


def dashboard_payload_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    printers = payload.get("printers", {}) if isinstance(payload.get("printers", {}), dict) else {}
    network = payload.get("network", {}) if isinstance(payload.get("network", {}), dict) else {}
    services = payload.get("services", {}) if isinstance(payload.get("services", {}), dict) else {}
    storage = payload.get("storage", {}) if isinstance(payload.get("storage", {}), dict) else {}

    dns_status = status_text(network.get("dns_lookup", "Unknown"))
    gateway_status = status_text(network.get("gateway_ping", "Unknown"))
    internet_status = status_text(network.get("internet_ping", "Unknown"))
    web_status = status_text(network.get("web_connectivity", "Unknown"))

    offline_printers = to_int(printers.get("offline_printer_count"))
    queued_jobs = to_int(printers.get("queued_jobs_total"))
    service_reviews = to_int(get_nested_value(services, "summary", "review_count", default=0))

    free_percent = storage.get("free_percent", "Unavailable")
    storage_status = status_text(storage.get("status", "Unknown"))
    low_disk = "low" in storage_status.lower()
    if not low_disk and free_percent != "Unavailable":
        low_disk = to_float(free_percent, 100.0) < 15.0

    return {
        "dns_status": dns_status,
        "gateway_status": gateway_status,
        "internet_status": internet_status,
        "web_status": web_status,
        "offline_printers": offline_printers,
        "queued_jobs": queued_jobs,
        "service_reviews": service_reviews,
        "storage_status": storage_status,
        "free_percent": free_percent,
        "low_disk": low_disk,
        "health_score": payload.get("health_score", "N/A"),
        "has_network_review": any(is_problem_status(item) for item in [dns_status, gateway_status, internet_status, web_status]),
        "has_printer_review": offline_printers > 0 or queued_jobs > 0 or is_problem_status(printers.get("result", "Unknown")),
        "has_service_review": service_reviews > 0,
    }


def build_dashboard_insights(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    totals = {
        "total": len(rows),
        "healthy": 0,
        "attention": 0,
        "local": 0,
        "demo": 0,
        "offline_printer_snapshots": 0,
        "queued_job_snapshots": 0,
        "network_review_snapshots": 0,
        "service_review_snapshots": 0,
        "low_disk_snapshots": 0,
        "open_cases": 0,
    }
    profile_counts: Dict[str, int] = {}
    recent_attention: List[sqlite3.Row] = []
    latest_local: Optional[sqlite3.Row] = None
    latest_demo: Optional[sqlite3.Row] = None

    for row in rows:
        status = clean_text(row["status"]).lower()
        mode = clean_text(row["mode"])
        profile = clean_text(row["profile"])
        resolution_status = clean_text(row["resolution_status"]).lower()
        profile_counts[profile] = profile_counts.get(profile, 0) + 1

        if status == "healthy":
            totals["healthy"] += 1
        else:
            totals["attention"] += 1
            if len(recent_attention) < 5:
                recent_attention.append(row)

        if mode == "Local Mode":
            totals["local"] += 1
            if latest_local is None:
                latest_local = row
        elif mode == "Demo Mode":
            totals["demo"] += 1
            if latest_demo is None:
                latest_demo = row

        if resolution_status and not any(word in resolution_status for word in ["resolved", "baseline", "closed"]):
            totals["open_cases"] += 1
        elif not resolution_status and status != "healthy":
            totals["open_cases"] += 1

        try:
            summary = dashboard_payload_summary(snapshot_payload(row))
        except Exception:
            summary = {}

        if to_int(summary.get("offline_printers")) > 0:
            totals["offline_printer_snapshots"] += 1
        if to_int(summary.get("queued_jobs")) > 0:
            totals["queued_job_snapshots"] += 1
        if summary.get("has_network_review"):
            totals["network_review_snapshots"] += 1
        if summary.get("has_service_review"):
            totals["service_review_snapshots"] += 1
        if summary.get("low_disk"):
            totals["low_disk_snapshots"] += 1

    profile_breakdown = [
        {"label": profile_label(profile), "count": count}
        for profile, count in sorted(profile_counts.items(), key=lambda item: (-item[1], profile_label(item[0])))
    ]

    return {
        "totals": totals,
        "profile_breakdown": profile_breakdown,
        "recent": rows[:6],
        "recent_attention": recent_attention,
        "latest_local": latest_local,
        "latest_demo": latest_demo,
    }


# -----------------------------
# Routes
# -----------------------------

@app.route("/health")
def health() -> Dict[str, str]:
    return {
        "app": APP_TITLE,
        "version": APP_VERSION,
        "status": "ok",
        "database": str(DB_PATH),
        "python": platform.python_version(),
        "demo_only": str(TCC_DEMO_ONLY),
        "environment": TCC_APP_ENV,
    }


@app.route("/deployment-guide")
def deployment_guide() -> str:
    content = render_template_string(
        """
        <div class="card">
            <h2>Deployment Guide</h2>
            <p>
                This app is designed with two safe operating modes: a real local technician mode and a public portfolio demo mode.
            </p>
            <div class="grid">
                <div class="card">
                    <h3>Local Technician Use</h3>
                    <p class="muted">Run the app on your own computer to collect real endpoint, network, service, printer, storage, and performance diagnostics.</p>
                    <span class="pill">Local Mode enabled</span>
                </div>
                <div class="card">
                    <h3>Public Portfolio Demo</h3>
                    <p class="muted">Use seeded sample snapshots online so employers can test the workflow without scanning their device or the hosting server.</p>
                    <span class="pill">Safe demo data</span>
                </div>
                <div class="card">
                    <h3>Current Environment</h3>
                    <p class="muted">Demo-only mode: <strong>{{ 'On' if tcc_demo_only else 'Off' }}</strong></p>
                    <p class="muted">App environment: <strong>{{ app_environment }}</strong></p>
                </div>
            </div>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>How to Get the Code</h3>
            <p>
                To run the local technician version, first get the project files from the GitHub repository. The recommended method is to clone the repository:
            </p>
            <pre>git clone https://github.com/EngJasmine/technician-command-center.git
cd technician-command-center</pre>
            <p>
                If Git is not installed, you can also open the GitHub repository in a browser, choose <strong>Code &gt; Download ZIP</strong>, extract the folder, and open it in PyCharm or VS Code.
            </p>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Recommended Local Run Commands</h3>
            <p>
                After cloning or downloading the project, run these commands from inside the project folder:
            </p>
            <pre>python -m venv venv
venv\\Scripts\\activate
python -m pip install -r requirements.txt
python app.py</pre>
            <p class="small muted">SQLite is created automatically in the data folder. No external database is required for this portfolio version.</p>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Recommended Public Demo Setting</h3>
            <p>
                For internet hosting, set this environment variable so the public website does not attempt to run local diagnostics on the hosting server:
            </p>
            <pre>TCC_DEMO_ONLY=1</pre>
            <p>
                When this is enabled, Local Mode is disabled and the app directs visitors to the portfolio demo workflow.
            </p>
            <p class="small muted">
                This is intentional: a browser-hosted app cannot inspect a visitor's laptop. The online version should demonstrate the workflow with sample snapshots, while the local version performs real technician checks.
            </p>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Portfolio Deployment Checklist</h3>
            <ul>
                <li>Keep <strong>app.py</strong> and <strong>requirements.txt</strong> in the project root.</li>
                <li>Use <strong>Flask==3.0.3</strong> and <strong>gunicorn==22.0.0</strong> in requirements.txt.</li>
                <li>Set <strong>TCC_DEMO_ONLY=1</strong> for the public hosted version.</li>
                <li>Open the Portfolio Demo page and seed demo data before taking screenshots.</li>
                <li>Show in the README that Local Mode collects real diagnostics only when run on the technician's machine.</li>
                <li>Do not present public Demo Mode as if it scans the visitor's computer.</li>
            </ul>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Suggested README Summary</h3>
            <pre>Technician Command Center is a Flask + SQLite diagnostic toolkit for IT support workflows. It captures endpoint snapshots, network checks, service status, printer state, performance indicators, before/after comparisons, technician notes, and exportable support bundles. The public demo uses seeded sample data for safety; the local version performs real diagnostics on the machine where it runs.</pre>
        </div>
        """,
        tcc_demo_only=TCC_DEMO_ONLY,
        app_environment=TCC_APP_ENV,
    )
    return page(content, title="Deployment Guide")




def build_readme_markdown() -> str:
    """Generate a portfolio-ready README draft for the project."""
    return """# Technician Command Center

Technician Command Center is a Flask + SQLite diagnostic toolkit for IT support workflows. It captures endpoint snapshots, network checks, service status, printer state, storage and performance indicators, before/after comparisons, technician notes, and exportable support bundles.

## Why I Built This

In IT support, technicians often need to collect evidence before making changes, document what changed after troubleshooting, and prepare clear escalation notes. This project demonstrates a structured way to capture that evidence and turn it into reports that are useful for users, technicians, and escalation teams.

## Key Features

- Local Mode for real endpoint diagnostics when the app runs on a technician machine
- Portfolio Demo Mode with safe seeded sample data for online employer demonstrations
- SQLite snapshot history
- Network diagnostics including local IP, gateway, DNS, ping, and web connectivity checks
- Windows service diagnostics including Print Spooler, DHCP Client, DNS Client, Workstation, Windows Update, and Windows Time
- Printer diagnostics including installed printers, default printer, offline state, and queue count
- Endpoint performance diagnostics including uptime, memory, process count, and recent system events
- Issue-focused snapshot views for DNS, printer, VPN, slow PC, healthy/baseline, and local endpoint scenarios
- Smart before/after comparison with guided filtering
- Technician case details, root cause, resolution status, and notes
- TXT, JSON, printable HTML, and ZIP support bundle exports
- Deployment-safe demo-only mode for public hosting

## Technology Used

- Python
- Flask
- SQLite
- HTML/CSS with Flask templates
- Windows PowerShell / system commands for local diagnostics
- JSON-based diagnostic payloads

## Local Setup

```powershell
python -m venv venv
venv\\Scripts\\activate
python -m pip install Flask==3.0.3
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Public Demo Mode

For a hosted public demo, set:

```text
TCC_DEMO_ONLY=1
```

This disables Local Mode and keeps the app safe for employers to test online with seeded sample data.

## Important Safety Note

A public web app cannot inspect a visitor's computer from the browser. The online portfolio version uses sample data to demonstrate the workflow. Real diagnostics run only when the app is launched locally on the technician's machine.

## Suggested Demo Walkthrough

1. Open Portfolio Demo.
2. Seed portfolio demo data.
3. Open a DNS failure snapshot.
4. Compare DNS failure with a healthy endpoint snapshot.
5. Open a printer failure snapshot.
6. Download the ZIP support bundle.
7. Explain that Local Mode performs real diagnostics when run on the technician machine.

## What This Project Demonstrates

- Practical IT troubleshooting methodology
- Endpoint evidence collection
- Network, service, printer, and performance diagnostics
- Before/after validation
- Technical reporting
- SQLite data persistence
- Flask web application development
- Portfolio-safe deployment design
"""


@app.route("/portfolio-readiness")
def portfolio_readiness() -> str:
    content = render_template_string(
        """
        <div class="card">
            <h2>Portfolio Readiness</h2>
            <p>
                Use this page as the final checklist before publishing Technician Command Center on your portfolio.
                It explains how to present the app to employers and which screenshots to capture.
            </p>
            <div class="grid">
                <div class="card" style="background:#fbfcff;">
                    <h3>Employer Message</h3>
                    <p class="muted">
                        This is not a generic dashboard. It is an IT support evidence tool that captures before/after endpoint snapshots,
                        highlights issue-focused diagnostics, and exports escalation-ready support bundles.
                    </p>
                </div>
                <div class="card" style="background:#fbfcff;">
                    <h3>Public Demo Safety</h3>
                    <p class="muted">
                        Hosted demos should use Demo Mode only. Local Mode is for the technician's own machine, not for scanning visitors' devices.
                    </p>
                </div>
                <div class="card" style="background:#fbfcff;">
                    <h3>Final Version</h3>
                    <p class="muted">
                        {{ app_version }}. This version is ready for screenshots, GitHub documentation, and portfolio linking.
                    </p>
                </div>
            </div>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Recommended Portfolio Description</h3>
            <pre>Technician Command Center is a Flask + SQLite diagnostic toolkit for IT support workflows. It captures endpoint, network, service, printer, storage, and performance snapshots, compares before/after troubleshooting states, and generates exportable support bundles for escalation.</pre>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Resume Bullet Options</h3>
            <ul>
                <li>Built a Flask + SQLite technician diagnostic toolkit with local endpoint snapshots, network checks, service status, printer diagnostics, and exportable support bundles.</li>
                <li>Implemented before/after troubleshooting comparison with issue-focused views for DNS, printer, VPN, slow PC, and healthy baseline scenarios.</li>
                <li>Designed a safe public demo mode using seeded sample data while preserving real local diagnostics for technician use.</li>
            </ul>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Screenshot Checklist</h3>
            <table>
                <thead>
                    <tr><th>Screenshot</th><th>What it proves</th></tr>
                </thead>
                <tbody>
                    <tr><td>Dashboard</td><td>Shows overall diagnostic insights, open cases, and issue mix.</td></tr>
                    <tr><td>Portfolio Demo page</td><td>Shows safe employer-friendly demo workflow.</td></tr>
                    <tr><td>Run Snapshot page</td><td>Shows Local Mode vs Demo Mode design.</td></tr>
                    <tr><td>Local snapshot detail</td><td>Shows real endpoint, network, service, printer, storage, and performance data.</td></tr>
                    <tr><td>Issue focus section</td><td>Shows that selected scenarios highlight the most relevant checks.</td></tr>
                    <tr><td>Compare page</td><td>Shows before/after troubleshooting validation.</td></tr>
                    <tr><td>Printable HTML report</td><td>Shows professional reporting and escalation readiness.</td></tr>
                    <tr><td>ZIP support bundle</td><td>Shows exportable evidence for escalation.</td></tr>
                </tbody>
            </table>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Suggested Live Demo Script</h3>
            <ol>
                <li>Open the dashboard and explain that this app captures technician evidence.</li>
                <li>Open Portfolio Demo and seed sample data.</li>
                <li>Open a DNS failure snapshot and point out the issue-focused diagnostics.</li>
                <li>Compare DNS failure with a healthy snapshot to show before/after validation.</li>
                <li>Open a printer issue snapshot and show offline printer/queue checks.</li>
                <li>Download a ZIP support bundle and explain how it supports escalation.</li>
                <li>Explain that Local Mode runs real diagnostics only on the technician's own machine.</li>
            </ol>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>README Draft</h3>
            <p>
                Download a ready-to-edit README draft for GitHub or your portfolio project page.
            </p>
            <a class="button" href="{{ url_for('download_readme') }}">Download README.md</a>
        </div>
        """,
        app_version=APP_VERSION,
    )
    return page(content, title="Portfolio Readiness")


@app.route("/portfolio-readiness/readme.md")
def download_readme() -> Response:
    markdown = build_readme_markdown()
    return Response(
        markdown,
        mimetype="text/markdown",
        headers={"Content-Disposition": "attachment; filename=README.md"},
    )


@app.route("/")
def dashboard() -> str:
    all_snapshots = get_snapshots()
    latest = all_snapshots[0] if all_snapshots else None
    insights = build_dashboard_insights(all_snapshots)
    totals = insights["totals"]

    content = render_template_string(
        """
        <div class="grid">
            <div class="card">
                <h3>Total Snapshots</h3>
                <div class="metric">{{ totals.total }}</div>
                <p class="muted">Saved diagnostic records in SQLite.</p>
            </div>
            <div class="card">
                <h3>Needs Attention</h3>
                <div class="metric">{{ totals.attention }}</div>
                <p class="muted">Snapshots with findings, warnings, or open review items.</p>
            </div>
            <div class="card">
                <h3>Local / Demo</h3>
                <div class="metric">{{ totals.local }} / {{ totals.demo }}</div>
                <p class="muted">Real endpoint snapshots versus safe portfolio samples.</p>
            </div>
            <div class="card">
                <h3>Open Cases</h3>
                <div class="metric">{{ totals.open_cases }}</div>
                <p class="muted">Snapshots still marked as investigating, monitoring, or escalated.</p>
            </div>
        </div>

        <div class="card" style="margin-top:16px;">
            <h2>Technician Command Center</h2>
            <p>
                A local diagnostic toolkit that captures endpoint, network, service, printer, and storage evidence,
                then turns it into before/after comparisons and support-ready reports.
            </p>
            <div class="grid">
                <div class="card" style="background:#fbfcff;">
                    <h3>For Technicians</h3>
                    <p class="muted">Run Local Mode to collect real diagnostics from the current computer.</p>
                    <a class="button" href="{{ url_for('run_snapshot') }}" target="_blank" rel="noopener noreferrer">Run Snapshot</a>
                </div>
                <div class="card" style="background:#fbfcff;">
                    <h3>For Employers</h3>
                    <p class="muted">Use Portfolio Demo Mode to see safe sample incidents online.</p>
                    <a class="button secondary" href="{{ url_for('portfolio_demo') }}" target="_blank" rel="noopener noreferrer">Open Portfolio Demo</a>
                </div>
                <div class="card" style="background:#fbfcff;">
                    <h3>For Evidence</h3>
                    <p class="muted">Compare before/after snapshots and export TXT, JSON, HTML, or ZIP bundles.</p>
                    <a class="button secondary" href="{{ url_for('compare') }}" target="_blank" rel="noopener noreferrer">Compare Snapshots</a>
                </div>
            </div>
        </div>

        <div class="grid" style="margin-top:16px;">
            <div class="card">
                <h3>Network Reviews</h3>
                <div class="metric">{{ totals.network_review_snapshots }}</div>
                <p class="muted">Snapshots with DNS, gateway, internet, or web-connectivity findings.</p>
            </div>
            <div class="card">
                <h3>Printer Reviews</h3>
                <div class="metric">{{ totals.offline_printer_snapshots }}</div>
                <p class="muted">Snapshots where at least one printer was detected as offline.</p>
            </div>
            <div class="card">
                <h3>Service Reviews</h3>
                <div class="metric">{{ totals.service_review_snapshots }}</div>
                <p class="muted">Snapshots with one or more services needing technician review.</p>
            </div>
            <div class="card">
                <h3>Low Disk</h3>
                <div class="metric">{{ totals.low_disk_snapshots }}</div>
                <p class="muted">Snapshots where storage may contribute to performance issues.</p>
            </div>
        </div>

        <div class="split" style="margin-top:16px;">
            <div class="card">
                <h3>Recent Snapshots</h3>
                {% if recent %}
                    <table>
                        <thead>
                            <tr>
                                <th>Snapshot</th>
                                <th>Case</th>
                                <th>Status</th>
                                <th>Open</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for row in recent %}
                            <tr>
                                <td>
                                    <strong>#{{ row['id'] }} {{ row['name'] }}</strong><br>
                                    <span class="small muted">{{ row['created_at'] }} | {{ row['mode'] }} | {{ profile_label(row['profile']) }}</span>
                                </td>
                                <td>{{ row['case_label'] or profile_label(row['profile']) }}</td>
                                <td><span class="status {{ status_class(row['status']) }}">{{ row['status'] }}</span></td>
                                <td><a href="{{ url_for('snapshot_detail', snapshot_id=row['id']) }}" target="_blank" rel="noopener noreferrer">View</a></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p>No snapshots yet.</p>
                    <a class="button" href="{{ url_for('run_snapshot') }}" target="_blank" rel="noopener noreferrer">Create First Snapshot</a>
                {% endif %}
            </div>

            <div class="card">
                <h3>Issue Mix</h3>
                {% if profile_breakdown %}
                    <table>
                        <thead><tr><th>Issue Category</th><th>Snapshots</th></tr></thead>
                        <tbody>
                        {% for item in profile_breakdown %}
                            <tr><td>{{ item.label }}</td><td>{{ item.count }}</td></tr>
                        {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p class="muted">Create snapshots to see the issue breakdown.</p>
                {% endif %}
            </div>
        </div>

        <div class="split" style="margin-top:16px;">
            <div class="card">
                <h3>Latest Local Snapshot</h3>
                {% if latest_local %}
                    <p><strong>{{ latest_local['name'] }}</strong></p>
                    <p><span class="status {{ status_class(latest_local['status']) }}">{{ latest_local['status'] }}</span> <span class="muted">{{ latest_local['created_at'] }}</span></p>
                    <a class="button secondary" href="{{ url_for('snapshot_detail', snapshot_id=latest_local['id']) }}" target="_blank" rel="noopener noreferrer">Open Local Snapshot</a>
                {% else %}
                    <p class="muted">No Local Mode snapshots yet. Run one to capture real diagnostics from this computer.</p>
                    <a class="button secondary" href="{{ url_for('run_snapshot') }}" target="_blank" rel="noopener noreferrer">Run Local Snapshot</a>
                {% endif %}
            </div>

            <div class="card">
                <h3>Recent Attention Items</h3>
                {% if recent_attention %}
                    <ul>
                    {% for row in recent_attention %}
                        <li>
                            <a href="{{ url_for('snapshot_detail', snapshot_id=row['id']) }}" target="_blank" rel="noopener noreferrer">#{{ row['id'] }} {{ row['case_label'] or row['name'] }}</a>
                            <span class="muted">- {{ profile_label(row['profile']) }} - {{ row['status'] }}</span>
                        </li>
                    {% endfor %}
                    </ul>
                {% else %}
                    <p class="muted">No recent attention items. Healthy/baseline snapshots can still be used for comparison.</p>
                {% endif %}
            </div>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Portfolio Explanation</h3>
            <p>
                <strong>Local Mode</strong> collects real diagnostic data from the machine running the app.
                <strong>Portfolio Demo Mode</strong> uses realistic sample data so the public internet version can safely demonstrate the workflow without scanning a visitor's computer.
            </p>
            <p>
                Recommended employer walkthrough: seed demo data, open a failure snapshot, compare it with a healthy snapshot,
                then download the support bundle ZIP.
            </p>
        </div>
        """,
        totals=totals,
        latest=latest,
        latest_local=insights["latest_local"],
        latest_demo=insights["latest_demo"],
        recent=insights["recent"],
        recent_attention=insights["recent_attention"],
        profile_breakdown=insights["profile_breakdown"],
        status_class=status_class,
        profile_label=profile_label,
    )
    return page(content, title="Dashboard")


@app.route("/portfolio-demo")
def portfolio_demo() -> str:
    demo_rows = get_portfolio_demo_snapshots()
    dns_before, dns_after = portfolio_demo_pair(f"{PORTFOLIO_DEMO_PREFIX}DNS Failure Before Fix")
    printer_before, printer_after = portfolio_demo_pair(f"{PORTFOLIO_DEMO_PREFIX}Printer Failure Before Fix")
    vpn_before, vpn_after = portfolio_demo_pair(f"{PORTFOLIO_DEMO_PREFIX}VPN Route Issue")

    content = render_template_string(
        """
        <div class="card">
            <h2>Portfolio Demo Mode</h2>
            <p>
                This page prepares a safe online demo for employers. It uses sample snapshots so the public version
                can show the app's purpose without trying to inspect a visitor's computer.
            </p>
            <p>
                The same app still supports <strong>Local Mode</strong> when you run it on your own laptop or a technician workstation.
            </p>
            {% if tcc_demo_only %}
            <div class="flash">
                Public demo-only mode is active. Local diagnostics are disabled so the hosted version only uses safe sample data.
            </div>
            {% endif %}
            <div class="grid">
                <div class="card">
                    <h3>Public Demo</h3>
                    <p class="muted">Uses realistic saved snapshots. Safe for internet hosting.</p>
                    <span class="pill">No endpoint scan</span>
                </div>
                <div class="card">
                    <h3>Local Technician Mode</h3>
                    <p class="muted">Collects real endpoint, network, service, and printer diagnostics.</p>
                    <span class="pill">Run locally</span>
                </div>
                <div class="card">
                    <h3>Employer Story</h3>
                    <p class="muted">Shows before/after evidence, support bundles, and technician reports.</p>
                    <span class="pill">Portfolio ready</span>
                </div>
            </div>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Demo Data</h3>
            <p class="muted">Seed five realistic sample snapshots for an employer walkthrough.</p>
            <form method="post" action="{{ url_for('seed_demo_data') }}" style="display:inline-block; margin-right:8px;">
                <button type="submit">Seed Portfolio Demo Data</button>
            </form>
            <form method="post" action="{{ url_for('clear_demo_data') }}" style="display:inline-block;">
                <button type="submit" style="background:#a52828;">Clear Portfolio Demo Data</button>
            </form>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Clean Data Before GitHub</h3>
            <p class="muted">
                Use these controls when you want to remove saved test snapshots before publishing or recording a clean portfolio demo.
                The safest GitHub practice is still to avoid committing the <code>data/</code> folder or any <code>.db</code> file.
            </p>
            <form method="post" action="{{ url_for('clear_local_data') }}" style="display:inline-block; margin-right:8px;" onsubmit="return confirm('Delete all Local Mode snapshots? Demo snapshots will remain.');">
                <button type="submit" style="background:#a15c00;">Clear Local Snapshots</button>
            </form>
            <form method="post" action="{{ url_for('clear_all_data') }}" style="display:inline-block;" onsubmit="return confirm('Delete ALL snapshots, including local and demo data? This cannot be undone.');">
                <button type="submit" style="background:#7f1d1d;">Clear All Snapshots</button>
            </form>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Recommended Employer Walkthrough</h3>
            {% if demo_rows %}
                <ol>
                    <li>Open the healthy baseline snapshot.</li>
                    <li>Open a failure snapshot such as DNS or Printer failure.</li>
                    <li>Compare the failure snapshot against the healthy baseline.</li>
                    <li>Download the support bundle ZIP from any snapshot detail page.</li>
                </ol>
                <p>
                    {% if dns_before and dns_after %}
                    <a class="button secondary" href="{{ url_for('compare', left=dns_before, right=dns_after) }}" target="_blank" rel="noopener noreferrer">Compare DNS Failure → Healthy</a>
                    {% endif %}
                    {% if printer_before and printer_after %}
                    <a class="button secondary" href="{{ url_for('compare', left=printer_before, right=printer_after) }}" target="_blank" rel="noopener noreferrer">Compare Printer Failure → Healthy</a>
                    {% endif %}
                    {% if vpn_before and vpn_after %}
                    <a class="button secondary" href="{{ url_for('compare', left=vpn_before, right=vpn_after) }}" target="_blank" rel="noopener noreferrer">Compare VPN Issue → Healthy</a>
                    {% endif %}
                </p>
            {% else %}
                <p>No portfolio demo snapshots found yet. Click <strong>Seed Portfolio Demo Data</strong> above.</p>
            {% endif %}
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Portfolio Demo Snapshots</h3>
            {% if demo_rows %}
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Name</th>
                            <th>Status</th>
                            <th>Created</th>
                            <th>Open</th>
                            <th>Bundle</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in demo_rows %}
                        <tr>
                            <td>{{ row['id'] }}</td>
                            <td>{{ row['name'] }}</td>
                            <td><span class="status {{ status_class(row['status']) }}">{{ row['status'] }}</span></td>
                            <td>{{ row['created_at'] }}</td>
                            <td><a href="{{ url_for('snapshot_detail', snapshot_id=row['id']) }}" target="_blank" rel="noopener noreferrer">View</a></td>
                            <td><a href="{{ url_for('download_support_bundle', snapshot_id=row['id']) }}">ZIP</a></td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <p class="muted">No demo snapshots yet.</p>
            {% endif %}
        </div>
        """,
        demo_rows=demo_rows,
        dns_before=dns_before,
        dns_after=dns_after,
        printer_before=printer_before,
        printer_after=printer_after,
        vpn_before=vpn_before,
        vpn_after=vpn_after,
        status_class=status_class,
        tcc_demo_only=TCC_DEMO_ONLY,
    )
    return page(content, title="Portfolio Demo")


@app.route("/portfolio-demo/seed", methods=["POST"])
def seed_demo_data() -> str:
    result = seed_portfolio_demo_snapshots()
    if result["created_count"]:
        flash(f"Created {result['created_count']} portfolio demo snapshot(s).")
    else:
        flash("Portfolio demo snapshots already exist. No duplicates were created.")
    return redirect(url_for("portfolio_demo"))


@app.route("/portfolio-demo/clear", methods=["POST"])
def clear_demo_data() -> str:
    deleted = clear_portfolio_demo_snapshots()
    flash(f"Deleted {deleted} portfolio demo snapshot(s).")
    return redirect(url_for("portfolio_demo"))


@app.route("/portfolio-demo/clear-local", methods=["POST"])
def clear_local_data() -> str:
    deleted = clear_local_snapshots()
    flash(f"Deleted {deleted} Local Mode snapshot(s).")
    return redirect(url_for("portfolio_demo"))


@app.route("/portfolio-demo/clear-all", methods=["POST"])
def clear_all_data() -> str:
    deleted = clear_all_snapshots()
    flash(f"Deleted {deleted} total snapshot(s). The snapshot ID counter was reset.")
    return redirect(url_for("portfolio_demo"))


@app.route("/run-snapshot", methods=["GET", "POST"])
def run_snapshot() -> str:
    if request.method == "POST":
        snapshot_mode = request.form.get("snapshot_mode", "demo")
        profile = request.form.get("profile", "healthy")
        name = request.form.get("name", "")

        if snapshot_mode == "local" and TCC_DEMO_ONLY:
            flash("Local diagnostics are disabled in public demo-only mode. Use Portfolio Demo sample snapshots online, or run the app locally for real endpoint diagnostics.")
            return redirect(url_for("portfolio_demo"))

        if snapshot_mode == "local":
            defaults = default_case_fields_for_profile("local_services", "Local Endpoint Review", "Local Mode", name)
            case_fields = build_case_fields_from_form(defaults)
            snapshot_id = create_local_snapshot(custom_name=name, case_fields=case_fields)
            flash("Local snapshot created successfully.")
        else:
            demo_label = profile_label(profile)
            defaults = default_case_fields_for_profile(profile, demo_label, "Demo Mode", name)
            case_fields = build_case_fields_from_form(defaults)
            snapshot_id = create_demo_snapshot(profile=profile, custom_name=name, case_fields=case_fields)
            flash("Demo snapshot created successfully.")

        return redirect(url_for("snapshot_detail", snapshot_id=snapshot_id))

    content = render_template_string(
        """
        <div class="card">
            <h2>Run Snapshot</h2>
            <p class="muted">
                Choose Demo Mode for safe sample data, or Local Mode to collect real system and network diagnostics from this computer.
            </p>
            {% if tcc_demo_only %}
            <div class="flash">
                Public demo-only mode is active. Local Mode is disabled for hosted demos; use Demo Mode or the Portfolio Demo page.
            </div>
            {% endif %}

            <form method="post">
                <label for="name">Snapshot Name</label>
                <input id="name" name="name" placeholder="Example: Baseline before DNS fix">

                <label for="snapshot_mode">Snapshot Mode</label>
                <select id="snapshot_mode" name="snapshot_mode">
                    <option value="demo">Demo Mode - sample portfolio data</option>
                    <option value="local" {% if tcc_demo_only %}disabled{% endif %}>Local Mode - real system and network snapshot from this computer{% if tcc_demo_only %} (disabled in public demo){% endif %}</option>
                </select>

                <label for="profile">Demo Scenario</label>
                <select id="profile" name="profile">
                    <option value="healthy">Healthy endpoint</option>
                    <option value="dns_issue">DNS issue</option>
                    <option value="printer_issue">Printer issue</option>
                    <option value="slow_pc">Slow PC</option>
                    <option value="vpn_issue">VPN connected, internal routes missing</option>
                </select>
                <p class="small muted">
                    Demo Scenario is ignored when Local Mode is selected.
                </p>

                <div class="card" style="margin-top:16px; background:#fbfcff;">
                    <h3>Technician Case Details</h3>
                    <p class="small muted">Optional, but recommended. These fields make reports and before/after comparisons look like real support documentation.</p>
                    <label for="case_label">Case Label</label>
                    <input id="case_label" name="case_label" placeholder="Example: HP printer offline - Accounting laptop">

                    <label for="affected_user">Affected User</label>
                    <input id="affected_user" name="affected_user" placeholder="Example: Sarah M. or Accounting team">

                    <label for="affected_device">Affected Device</label>
                    <input id="affected_device" name="affected_device" placeholder="Example: LAP-023 or PRN-Accounting-02">

                    <label for="snapshot_purpose">Snapshot Purpose</label>
                    <select id="snapshot_purpose" name="snapshot_purpose">
                        <option value="">Use automatic purpose</option>
                        <option value="Before Fix">Before Fix</option>
                        <option value="After Fix">After Fix</option>
                        <option value="Baseline">Baseline</option>
                        <option value="Escalation Evidence">Escalation Evidence</option>
                        <option value="Current State">Current State</option>
                    </select>

                    <label for="resolution_status">Resolution Status</label>
                    <select id="resolution_status" name="resolution_status">
                        <option value="">Use automatic status</option>
                        <option value="Investigating">Investigating</option>
                        <option value="Resolved">Resolved</option>
                        <option value="Escalated">Escalated</option>
                        <option value="Monitoring">Monitoring</option>
                        <option value="Resolved / Baseline">Resolved / Baseline</option>
                    </select>

                    <label for="root_cause">Root Cause</label>
                    <textarea id="root_cause" name="root_cause" rows="3" placeholder="Known or suspected root cause. You can edit this later."></textarea>

                    <label for="technician_notes">Technician Notes</label>
                    <textarea id="technician_notes" name="technician_notes" rows="4" placeholder="Commands run, user symptoms, observations, escalation context..."></textarea>
                </div>

                <p style="margin-top:18px;">
                    <button type="submit">Create Snapshot</button>
                </p>
            </form>
        </div>
        """,
        tcc_demo_only=TCC_DEMO_ONLY,
    )
    return page(content, title="Run Snapshot")


@app.route("/snapshots")
def snapshots() -> str:
    rows = get_snapshots()
    content = render_template_string(
        """
        <div class="card">
            <h2>Snapshot History</h2>
            {% if rows %}
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Name</th>
                            <th>Case</th>
                            <th>Purpose</th>
                            <th>Mode</th>
                            <th>Status</th>
                            <th>Created</th>
                            <th>Open</th>
                        </tr>
                    </thead>
                    <tbody>
                    {% for row in rows %}
                        <tr>
                            <td>{{ row['id'] }}</td>
                            <td>{{ row['name'] }}</td>
                            <td>{{ row['case_label'] or profile_label(row['profile']) }}</td>
                            <td>{{ row['snapshot_purpose'] or 'Not set' }}</td>
                            <td><span class="pill">{{ row['mode'] }}</span></td>
                            <td><span class="status {{ status_class(row['status']) }}">{{ row['status'] }}</span></td>
                            <td>{{ row['created_at'] }}</td>
                            <td><a href="{{ url_for('snapshot_detail', snapshot_id=row['id']) }}" target="_blank" rel="noopener noreferrer">View</a></td>
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <p>No snapshots yet.</p>
                <a class="button" href="{{ url_for('run_snapshot') }}" target="_blank" rel="noopener noreferrer">Create First Snapshot</a>
            {% endif %}
        </div>
        """,
        rows=rows,
        status_class=status_class,
        profile_label=profile_label,
    )
    return page(content, title="Snapshot History")


@app.route("/snapshots/<int:snapshot_id>/case", methods=["POST"])
def update_snapshot_case_route(snapshot_id: int) -> str:
    row = get_snapshot(snapshot_id)
    if row is None:
        flash("Snapshot not found.")
        return redirect(url_for("snapshots"))

    case_fields = {field: request.form.get(field, "") for field in CASE_FIELDS}
    update_snapshot_case(snapshot_id, case_fields)
    flash("Technician case details updated successfully.")
    return redirect(url_for("snapshot_detail", snapshot_id=snapshot_id))


@app.route("/snapshots/<int:snapshot_id>")
def snapshot_detail(snapshot_id: int):
    row = get_snapshot(snapshot_id)
    if row is None:
        return page("<div class='card'><h2>Snapshot not found</h2><p>The requested snapshot does not exist.</p></div>"), 404

    payload = snapshot_payload(row)
    content = render_template_string(
        """
        <div class="card">
            <h2>{{ row['name'] }}</h2>
            <p>
                <span class="status {{ status_class(row['status']) }}">{{ row['status'] }}</span>
                <span class="pill">{{ row['mode'] }}</span>
                <span class="muted"> {{ row['created_at'] }}</span>
            </p>
            <p>{{ row['summary'] }}</p>
            <a class="button secondary" href="{{ url_for('download_report', snapshot_id=row['id']) }}">Download TXT Report</a>
            <a class="button secondary" href="{{ url_for('download_json_report', snapshot_id=row['id']) }}">Download JSON</a>
            <a class="button secondary" href="{{ url_for('download_html_report', snapshot_id=row['id']) }}" target="_blank" rel="noopener noreferrer">Printable HTML</a>
            <a class="button secondary" href="{{ url_for('download_support_bundle', snapshot_id=row['id']) }}">Support Bundle ZIP</a>
            <a class="button secondary" href="{{ url_for('compare', left=row['id']) }}" target="_blank" rel="noopener noreferrer">Compare This Snapshot</a>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Technician Case Details</h3>
            <table>
                <tr><th>Case Label</th><td>{{ case_metadata.get('case_label') or profile_label(row['profile']) }}</td></tr>
                <tr><th>Affected User</th><td>{{ case_metadata.get('affected_user') or 'Not set' }}</td></tr>
                <tr><th>Affected Device</th><td>{{ case_metadata.get('affected_device') or 'Not set' }}</td></tr>
                <tr><th>Snapshot Purpose</th><td>{{ case_metadata.get('snapshot_purpose') or 'Not set' }}</td></tr>
                <tr><th>Resolution Status</th><td>{{ case_metadata.get('resolution_status') or 'Not set' }}</td></tr>
                <tr><th>Root Cause</th><td>{{ case_metadata.get('root_cause') or 'Not set' }}</td></tr>
                <tr><th>Technician Notes</th><td>{{ case_metadata.get('technician_notes') or 'No notes yet' }}</td></tr>
            </table>

            <details style="margin-top:14px;">
                <summary><strong>Edit Technician Case Details</strong></summary>
                <form method="post" action="{{ url_for('update_snapshot_case_route', snapshot_id=row['id']) }}" style="margin-top:12px;">
                    <label for="case_label_edit">Case Label</label>
                    <input id="case_label_edit" name="case_label" value="{{ case_metadata.get('case_label') }}">

                    <label for="affected_user_edit">Affected User</label>
                    <input id="affected_user_edit" name="affected_user" value="{{ case_metadata.get('affected_user') }}">

                    <label for="affected_device_edit">Affected Device</label>
                    <input id="affected_device_edit" name="affected_device" value="{{ case_metadata.get('affected_device') }}">

                    <label for="snapshot_purpose_edit">Snapshot Purpose</label>
                    <input id="snapshot_purpose_edit" name="snapshot_purpose" value="{{ case_metadata.get('snapshot_purpose') }}">

                    <label for="resolution_status_edit">Resolution Status</label>
                    <input id="resolution_status_edit" name="resolution_status" value="{{ case_metadata.get('resolution_status') }}">

                    <label for="root_cause_edit">Root Cause</label>
                    <textarea id="root_cause_edit" name="root_cause" rows="3">{{ case_metadata.get('root_cause') }}</textarea>

                    <label for="technician_notes_edit">Technician Notes</label>
                    <textarea id="technician_notes_edit" name="technician_notes" rows="4">{{ case_metadata.get('technician_notes') }}</textarea>

                    <p style="margin-top:14px;"><button type="submit">Save Case Details</button></p>
                </form>
            </details>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>{{ issue_focus.title }}</h3>
            <p class="muted">{{ issue_focus.description }}</p>
            <table>
                {% for label, value in issue_focus.checks %}
                <tr><th>{{ label }}</th><td>{{ value }}</td></tr>
                {% endfor %}
            </table>
            {% if issue_focus.interpretation %}
            <h4>How to read this focus</h4>
            <ul>
                {% for item in issue_focus.interpretation %}
                <li>{{ item }}</li>
                {% endfor %}
            </ul>
            {% endif %}
            <p class="small muted">The app still captures the full endpoint snapshot below. This section only highlights the checks most relevant to the selected scenario.</p>
        </div>

        <div class="split" style="margin-top:16px;">
            <div class="card">
                <h3>Key Findings</h3>
                <ul>
                    {% for item in payload.get('findings', []) %}
                    <li>{{ item }}</li>
                    {% endfor %}
                </ul>
            </div>
            <div class="card">
                <h3>Recommended Next Steps</h3>
                <ul>
                    {% for item in payload.get('recommended_next_steps', []) %}
                    <li>{{ item }}</li>
                    {% endfor %}
                </ul>
            </div>
        </div>

        {% if row['mode'] == 'Local Mode' %}
        <div class="card" style="margin-top:16px;">
            <h3>Local Snapshot Summary</h3>
            <table>
                <tr><th>Hostname</th><td>{{ payload.get('system', {}).get('hostname', 'Unavailable') }}</td></tr>
                <tr><th>User</th><td>{{ payload.get('system', {}).get('logged_in_user', 'Unavailable') }}</td></tr>
                <tr><th>Operating System</th><td>{{ payload.get('system', {}).get('operating_system', 'Unavailable') }}</td></tr>
                <tr><th>Local IP</th><td>{{ payload.get('network', {}).get('primary_local_ip', 'Unavailable') }}</td></tr>
                <tr><th>Default Gateway</th><td>{{ payload.get('network', {}).get('default_gateway', 'Unavailable') }}</td></tr>
                <tr><th>DNS Servers</th><td>{{ payload.get('network', {}).get('dns_servers', ['Unavailable'])|join(', ') }}</td></tr>
                <tr><th>Gateway Ping</th><td>{{ payload.get('network', {}).get('gateway_ping', {}).get('status', 'Unavailable') }}</td></tr>
                <tr><th>Internet Ping</th><td>{{ payload.get('network', {}).get('internet_ping', {}).get('status', 'Unavailable') }}</td></tr>
                <tr><th>DNS Lookup</th><td>{{ payload.get('network', {}).get('dns_lookup', {}).get('status', 'Unavailable') }}</td></tr>
                <tr><th>Web Connectivity</th><td>{{ payload.get('network', {}).get('web_connectivity', {}).get('status', 'Unavailable') }}</td></tr>
                <tr><th>Disk Free</th><td>{{ payload.get('storage', {}).get('free_gb', 'Unavailable') }} GB</td></tr>
                <tr><th>Uptime</th><td>{{ payload.get('endpoint', {}).get('uptime', {}).get('uptime', 'Unavailable') }}</td></tr>
                <tr><th>Memory</th><td>{{ payload.get('endpoint', {}).get('memory', {}).get('free_mb', 'Unavailable') }} MB free / {{ payload.get('endpoint', {}).get('memory', {}).get('free_percent', 'Unavailable') }}%</td></tr>
            </table>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Endpoint Performance Diagnostics</h3>
            {% set endpoint_data = payload.get('endpoint', {}) %}
            <table>
                <tr><th>Uptime</th><td>{{ endpoint_data.get('uptime', {}).get('uptime', 'Unavailable') }}</td></tr>
                <tr><th>Last Boot Time</th><td>{{ endpoint_data.get('uptime', {}).get('last_boot_time', 'Unavailable') }}</td></tr>
                <tr><th>Memory Status</th><td>{{ endpoint_data.get('memory', {}).get('status', 'Unavailable') }}</td></tr>
                <tr><th>Memory Free</th><td>{{ endpoint_data.get('memory', {}).get('free_mb', 'Unavailable') }} MB / {{ endpoint_data.get('memory', {}).get('free_percent', 'Unavailable') }}%</td></tr>
                <tr><th>Process Count</th><td>{{ endpoint_data.get('processes', {}).get('process_count', 'Unavailable') }}</td></tr>
                <tr><th>Battery</th><td>{{ endpoint_data.get('battery', {}).get('status', 'Unavailable') }}{% if endpoint_data.get('battery', {}).get('charge_percent', 'Unavailable') != 'Unavailable' %} ({{ endpoint_data.get('battery', {}).get('charge_percent') }}%){% endif %}</td></tr>
                <tr><th>Recent System Events</th><td>{{ endpoint_data.get('recent_system_events', {}).get('event_count', 'Unavailable') }} warnings/errors in last {{ endpoint_data.get('recent_system_events', {}).get('lookback_hours', 24) }} hours</td></tr>
            </table>

            {% set top_processes = endpoint_data.get('processes', {}).get('top_memory_processes', []) %}
            {% if top_processes %}
                <h4>Top Memory Processes</h4>
                <table>
                    <thead><tr><th>Process</th><th>PID</th><th>Memory MB</th></tr></thead>
                    <tbody>
                    {% for process in top_processes %}
                        <tr>
                            <td>{{ process.get('name', 'Unknown') }}</td>
                            <td>{{ process.get('pid', 'Unavailable') }}</td>
                            <td>{{ process.get('memory_mb', 'Unavailable') }}</td>
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            {% endif %}

            {% set recent_events = endpoint_data.get('recent_system_events', {}).get('events', []) %}
            {% if recent_events %}
                <h4>Recent System Event Log Warnings/Errors</h4>
                <table>
                    <thead><tr><th>Time</th><th>Level</th><th>Source</th><th>ID</th><th>Message</th></tr></thead>
                    <tbody>
                    {% for event in recent_events %}
                        <tr>
                            <td>{{ event.get('time', 'Unavailable') }}</td>
                            <td>{{ event.get('level', 'Unavailable') }}</td>
                            <td>{{ event.get('provider', 'Unavailable') }}</td>
                            <td>{{ event.get('event_id', 'Unavailable') }}</td>
                            <td>{{ event.get('message', 'No message') }}</td>
                        </tr>
                    {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <p>No recent System event warnings/errors were collected, or event logging was unavailable.</p>
            {% endif %}
            <p class="small muted">Endpoint performance checks are read-only. They collect context for slow-PC, stability, and recurring-issue troubleshooting.</p>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Service Diagnostics</h3>
            {% set service_checks = payload.get('services', {}).get('checks', []) %}
            {% if service_checks %}
                <table>
                    <thead>
                        <tr>
                            <th>Service</th>
                            <th>Status</th>
                            <th>Expected</th>
                            <th>Result</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for service in service_checks %}
                        <tr>
                            <td>{{ service.get('display_name', service.get('service_name', 'Unknown')) }}</td>
                            <td>{{ service.get('status', 'Unknown') }}</td>
                            <td>{{ service.get('recommended_state', 'Unknown') }}</td>
                            <td>{{ service.get('result', 'Unknown') }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                <p class="small muted">These checks are read-only. The app did not start, stop, or modify any service.</p>
            {% else %}
                <p>No service diagnostics are available in this snapshot.</p>
            {% endif %}
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Printer Diagnostics</h3>
            {% set printer_data = payload.get('printers', {}) %}
            <table>
                <tr><th>Print Service Status</th><td>{{ printer_data.get('print_service_status', 'Unavailable') }}</td></tr>
                <tr><th>Installed Printers</th><td>{{ printer_data.get('installed_printer_count', 'Unavailable') }}</td></tr>
                <tr><th>Physical/Network Printers</th><td>{{ printer_data.get('physical_or_network_printer_count', 'Unavailable') }}</td></tr>
                <tr><th>Default Printer</th><td>{{ printer_data.get('default_printer', 'Unavailable') }}</td></tr>
                <tr><th>Offline Printers</th><td>{{ printer_data.get('offline_printer_count', 'Unavailable') }}</td></tr>
                <tr><th>Total Queued Jobs</th><td>{{ printer_data.get('queued_jobs_total', 'Unavailable') }}</td></tr>
                <tr><th>Result</th><td>{{ printer_data.get('result', 'Unavailable') }}</td></tr>
                <tr><th>Readiness Summary</th><td>{{ printer_data.get('readiness_summary', 'Unavailable') }}</td></tr>
            </table>

            {% set printer_list = printer_data.get('printers', []) %}
            {% if printer_list %}
                <h4>Installed Printer Inventory</h4>
                <table>
                    <thead>
                        <tr>
                            <th>Name</th>
                            <th>Default</th>
                            <th>Type</th>
                            <th>Status</th>
                            <th>Availability</th>
                            <th>Offline</th>
                            <th>Offline Reason</th>
                            <th>Queued Jobs</th>
                            <th>Port</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for printer in printer_list %}
                        <tr>
                            <td>{{ printer.get('name', 'Unknown') }}</td>
                            <td>{{ 'Yes' if printer.get('is_default') else 'No' }}</td>
                            <td>{{ printer.get('type', 'Unknown') }}</td>
                            <td>{{ printer.get('status', 'Unknown') }}</td>
                            <td>{{ printer.get('availability', 'Unknown') }}</td>
                            <td>{{ 'Yes' if printer.get('is_offline') else 'No' }}</td>
                            <td>{{ printer.get('offline_reason', 'Unavailable') }}</td>
                            <td>{{ printer.get('queue_count', 0) }}</td>
                            <td>{{ printer.get('port', 'Unavailable') }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <p>No installed printers were detected by the local printer inventory check.</p>
            {% endif %}
            <p class="small muted">Printer checks are read-only. The app did not add, remove, pause, resume, or clear any printer or print job.</p>
        </div>
        {% endif %}

        <div class="card" style="margin-top:16px;">
            <details>
                <summary><strong>Advanced Raw Snapshot Payload</strong> - preserved for troubleshooting and JSON export</summary>
                <p class="small muted">The main page formats diagnostic values for technicians. This advanced section keeps the original raw data.</p>
                <pre>{{ payload_pretty }}</pre>
            </details>
        </div>
        """,
        row=row,
        payload=payload,
        payload_pretty=json.dumps(payload, indent=2),
        status_class=status_class,
        case_metadata=get_case_metadata(row),
        issue_focus=get_issue_focus(row["profile"], payload, row["mode"]),
        profile_label=profile_label,
    )
    return page(content, title=row["name"])


@app.route("/compare")
def compare() -> str:
    rows = get_snapshots()
    left_id = request.args.get("left", type=int)
    right_id = request.args.get("right", type=int)

    differences: List[Dict[str, Any]] = []
    key_comparison: Optional[Dict[str, Any]] = None
    left_row = get_snapshot(left_id) if left_id else None
    right_row = get_snapshot(right_id) if right_id else None
    after_rows = smart_after_options(rows, left_row)
    if right_row and all(row["id"] != right_row["id"] for row in after_rows):
        # Keep a deliberately selected cross-category snapshot visible so the technician
        # understands what is being compared. The warning below explains the context.
        after_rows = after_rows + [right_row]
    warning_message = comparison_context_warning(left_row, right_row)

    if left_row and right_row:
        left_payload = snapshot_payload(left_row)
        right_payload = snapshot_payload(right_row)
        differences = compare_payloads(left_payload, right_payload)
        key_comparison = build_key_comparison(left_payload, right_payload)

    snapshot_options = [snapshot_option_payload(row, left_id) for row in rows]
    selected_right_id = right_id or ""

    content = render_template_string(
        """
        <div class="card">
            <h2>Compare Snapshots</h2>
            <p class="muted">
                Choose a before snapshot first. The after list will guide the technician toward
                the same issue type, while still keeping Healthy, Local, and Baseline snapshots available
                for full-system validation.
            </p>

            <form method="get" id="compareForm">
                <label for="left">Before / Left Snapshot</label>
                <select id="left" name="left">
                    <option value="">Select snapshot</option>
                    {% for row in rows %}
                        <option value="{{ row['id'] }}" {% if left_id == row['id'] %}selected{% endif %}>
                            {{ snapshot_display_label(row) }}
                        </option>
                    {% endfor %}
                </select>
                <p class="small muted">
                    First list shows every existing snapshot because the technician decides the starting point.
                </p>

                <label for="right">After / Right Snapshot</label>
                <select id="right" name="right">
                    <option value="">Select after snapshot</option>
                    {% for row in after_rows %}
                        <option value="{{ row['id'] }}" {% if right_id == row['id'] %}selected{% endif %}>
                            {{ snapshot_display_label(row) }}
                        </option>
                    {% endfor %}
                </select>
                <p id="afterHelp" class="small muted">
                    {% if left_row %}
                        Showing snapshots related to <strong>{{ profile_label(left_row['profile']) }}</strong>, plus Healthy, Local, and Baseline snapshots.
                    {% else %}
                        Select a before snapshot to focus the after list.
                    {% endif %}
                </p>

                <p style="margin-top:18px;"><button type="submit">Compare</button></p>
            </form>
        </div>

        <script>
            const snapshotOptions = {{ snapshot_options|tojson }};
            const selectedRightId = "{{ selected_right_id }}";
            const leftSelect = document.getElementById("left");
            const rightSelect = document.getElementById("right");
            const afterHelp = document.getElementById("afterHelp");

            function profileLabel(profile) {
                const labels = {
                    healthy: "Healthy Endpoint",
                    dns_issue: "DNS Issue",
                    printer_issue: "Printer Issue",
                    slow_pc: "Slow PC",
                    vpn_issue: "VPN Issue",
                    local_services: "Local Mode Snapshot"
                };
                return labels[profile] || String(profile || "Unknown").replaceAll("_", " ");
            }

            function isFlexible(option) {
                const profile = String(option.profile || "").toLowerCase();
                const name = String(option.name || "").toLowerCase();
                return profile === "healthy" || profile === "local_services" || profile === "baseline" || name.includes("baseline") || name.includes("healthy");
            }

            function allowedAfterOptions(beforeOption) {
                if (!beforeOption) {
                    return snapshotOptions;
                }
                if (isFlexible(beforeOption)) {
                    return snapshotOptions;
                }
                const filtered = snapshotOptions.filter((option) => {
                    if (option.id === beforeOption.id) {
                        return false;
                    }
                    return option.profile === beforeOption.profile || isFlexible(option);
                });
                return filtered.length ? filtered : snapshotOptions;
            }

            function updateAfterDropdown() {
                const beforeId = Number(leftSelect.value || 0);
                const beforeOption = snapshotOptions.find((option) => option.id === beforeId);
                let allowed = allowedAfterOptions(beforeOption);
                const currentRight = rightSelect.value || selectedRightId;
                if (currentRight) {
                    const selectedOption = snapshotOptions.find((option) => String(option.id) === String(currentRight));
                    const alreadyAllowed = allowed.some((option) => String(option.id) === String(currentRight));
                    if (selectedOption && !alreadyAllowed) {
                        allowed = [...allowed, selectedOption];
                    }
                }

                rightSelect.innerHTML = "";
                const placeholder = document.createElement("option");
                placeholder.value = "";
                placeholder.textContent = "Select after snapshot";
                rightSelect.appendChild(placeholder);

                allowed.forEach((option) => {
                    const item = document.createElement("option");
                    item.value = option.id;
                    item.textContent = option.label;
                    if (String(option.id) === String(currentRight)) {
                        item.selected = true;
                    }
                    rightSelect.appendChild(item);
                });

                if (!beforeOption) {
                    afterHelp.innerHTML = "Select a before snapshot to focus the after list.";
                } else if (isFlexible(beforeOption)) {
                    afterHelp.innerHTML = "Before snapshot is Healthy, Local, or Baseline, so all after snapshots remain available for full-system comparison.";
                } else {
                    afterHelp.innerHTML = "Showing " + profileLabel(beforeOption.profile) + " snapshots plus Healthy, Local, and Baseline snapshots.";
                }
            }

            leftSelect.addEventListener("change", updateAfterDropdown);
            updateAfterDropdown();
        </script>

        {% if warning_message %}
            <div class="card" style="margin-top:16px; border-color:#f4c46a; background:#fff8e8;">
                <h3>Comparison Context Warning</h3>
                <p>{{ warning_message }}</p>
            </div>
        {% endif %}

        {% if left_row and right_row and key_comparison %}
            <div class="card" style="margin-top:16px;">
                <h3>Technician Interpretation</h3>
                <p>
                    Comparing <strong>{{ left_row['name'] }}</strong>
                    with <strong>{{ right_row['name'] }}</strong>.
                </p>
                <p class="small muted">
                    Before category: <strong>{{ profile_label(left_row['profile']) }}</strong> |
                    After category: <strong>{{ profile_label(right_row['profile']) }}</strong>
                </p>
                <div class="grid">
                    <div class="comparison-note">
                        <strong>Improved</strong>
                        <div class="metric">{{ key_comparison['counts']['improved'] }}</div>
                    </div>
                    <div class="comparison-note">
                        <strong>Worsened</strong>
                        <div class="metric">{{ key_comparison['counts']['worsened'] }}</div>
                    </div>
                    <div class="comparison-note">
                        <strong>Changed</strong>
                        <div class="metric">{{ key_comparison['counts']['changed'] }}</div>
                    </div>
                    <div class="comparison-note">
                        <strong>No Change</strong>
                        <div class="metric">{{ key_comparison['counts']['no_change'] }}</div>
                    </div>
                </div>

                <h4>Summary</h4>
                <ul>
                    {% for item in key_comparison['interpretation'] %}
                    <li>{{ item }}</li>
                    {% endfor %}
                </ul>
            </div>

            <div class="card" style="margin-top:16px;">
                <h3>Key Before/After Checks</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Check</th>
                            <th>Before</th>
                            <th>After</th>
                            <th>Outcome</th>
                            <th>Technician Note</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in key_comparison['rows'] %}
                        <tr>
                            <td>{{ row['label'] }}</td>
                            <td>{{ row['left'] }}</td>
                            <td>{{ row['right'] }}</td>
                            <td><span class="status {{ comparison_class(row['outcome']) }}">{{ row['outcome'] }}</span></td>
                            <td>{{ row['note'] }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>

            <div class="card" style="margin-top:16px;">
                <details>
                    <summary>Advanced Field Differences - {{ differences|length }} formatted changes</summary>
                    {% if differences %}
                        <table>
                            <thead>
                                <tr>
                                    <th>Field</th>
                                    <th>Before Value</th>
                                    <th>After Value</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for diff in differences %}
                                <tr>
                                    <td>{{ diff['field'] }}</td>
                                    <td>{{ diff['left'] }}</td>
                                    <td>{{ diff['right'] }}</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    {% else %}
                        <p>No raw field differences found.</p>
                    {% endif %}
                </details>
            </div>
        {% endif %}
        """,
        rows=rows,
        after_rows=after_rows,
        left_id=left_id,
        right_id=right_id,
        left_row=left_row,
        right_row=right_row,
        differences=differences,
        key_comparison=key_comparison,
        comparison_class=comparison_class,
        profile_label=profile_label,
        warning_message=warning_message,
        snapshot_options=snapshot_options,
        selected_right_id=selected_right_id,
        snapshot_display_label=snapshot_display_label,
    )
    return page(content, title="Compare")


@app.route("/reports")
def reports() -> str:
    rows = get_snapshots()
    content = render_template_string(
        """
        <div class="card">
            <h2>Reports</h2>
            <p class="muted">Download technician-friendly reports as TXT, JSON, printable HTML, or a ZIP support bundle.</p>

            {% if rows %}
                <table>
                    <thead>
                        <tr>
                            <th>Snapshot</th>
                            <th>Case</th>
                            <th>Purpose</th>
                            <th>Mode</th>
                            <th>Status</th>
                            <th>Created</th>
                            <th>Exports</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in rows %}
                        <tr>
                            <td>{{ row['name'] }}</td>
                            <td>{{ row['case_label'] or profile_label(row['profile']) }}</td>
                            <td>{{ row['snapshot_purpose'] or 'Not set' }}</td>
                            <td><span class="pill">{{ row['mode'] }}</span></td>
                            <td><span class="status {{ status_class(row['status']) }}">{{ row['status'] }}</span></td>
                            <td>{{ row['created_at'] }}</td>
                            <td>
                                <a href="{{ url_for('download_report', snapshot_id=row['id']) }}">TXT</a> |
                                <a href="{{ url_for('download_json_report', snapshot_id=row['id']) }}">JSON</a> |
                                <a href="{{ url_for('download_html_report', snapshot_id=row['id']) }}" target="_blank" rel="noopener noreferrer">HTML</a> |
                                <a href="{{ url_for('download_support_bundle', snapshot_id=row['id']) }}">ZIP</a>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% else %}
                <p>No reports are available yet. Create a snapshot first.</p>
            {% endif %}
        </div>
        """,
        rows=rows,
        status_class=status_class,
        profile_label=profile_label,
    )
    return page(content, title="Reports")


@app.route("/reports/<int:snapshot_id>.txt")
def download_report(snapshot_id: int) -> Response:
    row = get_snapshot(snapshot_id)
    if row is None:
        return Response("Snapshot not found.", status=404, mimetype="text/plain")

    payload = snapshot_payload(row)
    report_text = build_text_report(row, payload)
    filename = f"{bundle_filename(row)}_report.txt"

    return Response(
        report_text,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/reports/<int:snapshot_id>.json")
def download_json_report(snapshot_id: int) -> Response:
    row = get_snapshot(snapshot_id)
    if row is None:
        return Response("Snapshot not found.", status=404, mimetype="text/plain")

    payload = snapshot_payload(row)
    report_json = build_json_report(row, payload)
    filename = f"{bundle_filename(row)}_data.json"

    return Response(
        report_json,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/reports/<int:snapshot_id>.html")
def download_html_report(snapshot_id: int) -> Response:
    row = get_snapshot(snapshot_id)
    if row is None:
        return Response("Snapshot not found.", status=404, mimetype="text/plain")

    payload = snapshot_payload(row)
    report_html = build_html_report(row, payload)
    filename = f"{bundle_filename(row)}_printable.html"

    return Response(
        report_html,
        mimetype="text/html",
        headers={"Content-Disposition": f"inline; filename={filename}"},
    )


@app.route("/reports/<int:snapshot_id>.zip")
def download_support_bundle(snapshot_id: int) -> Response:
    row = get_snapshot(snapshot_id)
    if row is None:
        return Response("Snapshot not found.", status=404, mimetype="text/plain")

    payload = snapshot_payload(row)
    base_name = bundle_filename(row)
    json_report = build_json_report(row, payload)
    text_report = build_text_report(row, payload)
    html_report = build_html_report(row, payload)
    readme = "\n".join(
        [
            APP_TITLE,
            "Support Bundle",
            "",
            f"Snapshot: {row['name']}",
            f"Created: {row['created_at']}",
            f"Status: {row['status']}",
            "",
            "Files included:",
            f"- {base_name}_report.txt: technician-readable report",
            f"- {base_name}_data.json: structured diagnostic data",
            f"- {base_name}_printable.html: printable report that can be saved as PDF",
            "",
            "This bundle is read-only evidence. It does not modify the endpoint.",
        ]
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme)
        archive.writestr(f"{base_name}_report.txt", text_report)
        archive.writestr(f"{base_name}_data.json", json_report)
        archive.writestr(f"{base_name}_printable.html", html_report)

    buffer.seek(0)
    filename = f"{base_name}_support_bundle.zip"
    return Response(
        buffer.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

@app.errorhandler(404)
def not_found(error: Exception) -> Tuple[str, int]:
    content = "<div class='card'><h2>Page not found</h2><p>The page you requested does not exist.</p></div>"
    return page(content, title="Page Not Found"), 404


@app.errorhandler(500)
def server_error(error: Exception) -> Tuple[str, int]:
    content = "<div class='card'><h2>Server error</h2><p>Something went wrong. Check the terminal for details.</p></div>"
    return page(content, title="Server Error"), 500


# Initialize SQLite when the app is imported by production servers such as Gunicorn.
# Local execution also uses the same initialized database.
init_db()


if __name__ == "__main__":
    app.run(debug=True)
