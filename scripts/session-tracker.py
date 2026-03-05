#!/usr/bin/env python3
"""Claude Code Session Tracker - A floating GUI showing all active Claude Code sessions.

Scans ~/.claude/projects/ for session data and cross-references with running processes
to show real-time session status across all projects.

Includes a file-based command server for remote control from the terminal.
Send commands via: session-ctl <command>
"""

import json
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import tkinter as tk
import uuid as _uuid
from tkinter import simpledialog
from datetime import datetime, timezone
from pathlib import Path

try:
    from AppKit import (
        NSApplication, NSApplicationActivationPolicyAccessory,
        NSStatusBar, NSVariableStatusItemLength,
        NSMenu, NSMenuItem,
    )
    from Foundation import NSObject
    from objc import super as objc_super
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False

# --- Configuration ---
CLAUDE_DIR = os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
CMD_FILE = os.path.join(CLAUDE_DIR, ".session-cmd")
CMD_RESULT_FILE = os.path.join(CLAUDE_DIR, ".session-cmd-result")
SCREENSHOT_FILE = os.path.join(CLAUDE_DIR, "session-screenshot.png")
SESSION_NAMES_FILE = os.path.join(CLAUDE_DIR, ".session-names.json")
SCAN_INTERVAL_MS = 5000  # 5 seconds
CMD_POLL_INTERVAL_MS = 500
DOCKER_SCAN_INTERVAL = 15  # seconds between container session scans
DOCKER_DISCOVER_INTERVAL = 60  # seconds between container discovery

# Colors (Catppuccin Mocha)
COLOR_BG = "#1e1e2e"
COLOR_CARD_BG = "#313244"
COLOR_CARD_BG_INACTIVE = "#262637"
COLOR_FG = "#cdd6f4"
COLOR_FG_DIM = "#9399b2"
COLOR_GREEN = "#a6e3a1"
COLOR_YELLOW = "#f9e2af"
COLOR_RED = "#f38ba8"
COLOR_BLUE = "#89b4fa"
COLOR_MAUVE = "#cba6f7"

COLOR_ORANGE = "#fab387"

# Status colors
STATUS_WORKING = COLOR_ORANGE    # Claude is busy generating a response
STATUS_WAITING = COLOR_GREEN     # Awaiting user input
STATUS_RECENT = COLOR_BLUE       # Active within the last hour
STATUS_IDLE = COLOR_FG_DIM       # No recent activity


def decode_project_path(encoded):
    """Decode a project directory name back to a readable path."""
    # e.g. "-Users-johnmoody-Documents-Ork" -> "Ork"
    # Take the last meaningful segment
    parts = encoded.strip("-").split("-")
    # Skip common path prefixes
    skip = {"Users", Path.home().name, "WorkDir", "Documents", "private", "tmp"}
    meaningful = [p for p in parts if p not in skip]
    if meaningful:
        return "-".join(meaningful)
    return encoded.strip("-").split("-")[-1]


def resolve_project_path(encoded):
    """Decode an encoded project dir name back to the real filesystem path.

    Claude CLI encodes paths by replacing '/' with '-', but directory names
    themselves can contain hyphens.  We greedily resolve from left to right,
    preferring the longest existing directory at each step.
    """
    parts = encoded.strip("-").split("-")
    path = "/"
    i = 0
    while i < len(parts):
        # Try joining progressively more segments with hyphens
        # Check longest possible match first (greedy)
        matched = False
        for j in range(len(parts), i, -1):
            candidate = "-".join(parts[i:j])
            test_path = os.path.join(path, candidate)
            if os.path.exists(test_path):
                path = test_path
                i = j
                matched = True
                break
        if not matched:
            # No existing path found — just use the single segment
            path = os.path.join(path, parts[i])
            i += 1
    return path


def format_tokens(count):
    """Format token count with K/M suffix."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def format_relative_time(timestamp_str):
    """Format an ISO timestamp as relative time."""
    if not timestamp_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(timestamp_str)
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"{h}h {m}m ago"
        return f"{seconds // 86400}d ago"
    except (ValueError, TypeError):
        return "unknown"


def format_duration(start_str, end_str=None):
    """Format duration between two timestamps."""
    if not start_str:
        return ""
    try:
        start = datetime.fromisoformat(start_str)
        end = datetime.fromisoformat(end_str) if end_str else datetime.now(timezone.utc)
        delta = end - start
        total = int(delta.total_seconds())
        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m"
        h = total // 3600
        m = (total % 3600) // 60
        return f"{h}h {m}m"
    except (ValueError, TypeError):
        return ""


class SessionScanner:
    """Scans ~/.claude/projects/ and running processes to build a session list."""

    # Files larger than this use head+tail parsing instead of full read
    LARGE_FILE_THRESHOLD = 500_000  # 500KB

    def __init__(self):
        self._process_cwds = {}  # pid -> cwd
        self._pid_ttys = {}  # pid -> tty (e.g. "/dev/ttys002")
        self._cache = {}  # jsonl_path -> {"mtime": float, "size": int, "result": dict}

    def scan(self):
        """Return a list of session dicts, sorted by last activity (most recent first)."""
        running_cwds = self._scan_processes()
        sessions = []

        if not os.path.isdir(PROJECTS_DIR):
            return sessions

        for proj_name in os.listdir(PROJECTS_DIR):
            proj_path = os.path.join(PROJECTS_DIR, proj_name)
            if not os.path.isdir(proj_path) or proj_name.startswith("."):
                continue

            project_display = decode_project_path(proj_name)
            # Decode the actual filesystem path from the project name
            actual_path = resolve_project_path(proj_name)

            seen_ids = set()

            for item in os.listdir(proj_path):
                item_path = os.path.join(proj_path, item)

                # Skip non-session items
                if item in ("memory",) or item.startswith(".") or item.startswith("agent-"):
                    continue

                # Session directories = potentially active
                if os.path.isdir(item_path):
                    jsonl_path = os.path.join(proj_path, item + ".jsonl")
                    if os.path.exists(jsonl_path):
                        meta = self._parse_session(jsonl_path, item)
                        if meta:
                            meta["project"] = project_display
                            meta["project_path"] = actual_path
                            meta["_proj_name"] = proj_name
                            meta["is_dir"] = True
                            meta["_jsonl_path"] = jsonl_path
                            # Check if truly running
                            meta["status"] = self._classify_status(
                                meta, proj_name, running_cwds
                            )
                            sessions.append(meta)
                            seen_ids.add(item)

            # Also pick up JSONL files without session directories —
            # newer Claude Code versions may not always create them.
            # Only check projects that have running Claude processes.
            has_running = any(
                cwd.replace("/", "-") == proj_name
                for cwd in running_cwds
            )
            if has_running:
                for item in os.listdir(proj_path):
                    if not item.endswith(".jsonl") or item.startswith("agent-"):
                        continue
                    session_id = item[:-6]  # strip .jsonl
                    if session_id in seen_ids:
                        continue
                    item_path = os.path.join(proj_path, item)
                    if not os.path.isfile(item_path):
                        continue
                    # Quick mtime check — skip files older than 24h
                    try:
                        mtime = os.stat(item_path).st_mtime
                    except OSError:
                        continue
                    if time.time() - mtime > 86400:
                        continue
                    meta = self._parse_session(item_path, session_id)
                    if meta:
                        meta["project"] = project_display
                        meta["project_path"] = actual_path
                        meta["_proj_name"] = proj_name
                        meta["is_dir"] = False
                        meta["_jsonl_path"] = item_path
                        meta["status"] = self._classify_status(
                            meta, proj_name, running_cwds
                        )
                        sessions.append(meta)

        # Post-process: for each project with running processes, only the N
        # most recently active sessions keep working/waiting status + get TTYs.
        # The rest are downgraded to recent/idle based on age.
        proj_groups = {}  # (proj_name_encoded) -> {pids, sessions}
        for s in sessions:
            pids = s.pop("_matched_pids", None)
            if pids:
                key = s.get("_proj_name", "")
                proj_groups.setdefault(key, {"pids": pids, "sessions": []})
                proj_groups[key]["sessions"].append(s)
        now = time.time()
        for info in proj_groups.values():
            pids = info["pids"]
            group = info["sessions"]
            # Collect available TTYs from the matched PIDs
            ttys = []
            for pid in pids:
                tty = self._pid_ttys.get(pid)
                if tty:
                    ttys.append(tty)
            n_active = max(len(pids), 1)
            # Sort by last activity (most recent first)
            ranked = sorted(
                group,
                key=lambda s: s.get("last_activity_ts", 0),
                reverse=True,
            )
            # Top N keep their working/waiting status + get TTYs
            for i, s in enumerate(ranked[:n_active]):
                if i < len(ttys):
                    s["tty"] = ttys[i]
            # Remaining sessions: downgrade to recent/idle
            for s in ranked[n_active:]:
                age = now - s.get("last_activity_ts", 0)
                s["status"] = "recent" if age < 3600 else "idle"

        # Sort by last activity, most recent first
        sessions.sort(key=lambda s: s.get("last_activity_ts", 0), reverse=True)
        return sessions

    def _scan_processes(self):
        """Find running claude CLI processes and their working directories."""
        cwds = {}  # cwd -> list of pids
        try:
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 11:
                    continue
                cmd = " ".join(parts[10:])
                # Match 'claude' process but not Claude.app, helpers, or this script
                if parts[10].endswith("/claude") or parts[10] == "claude":
                    pid = parts[1]
                    tty_field = parts[6] if len(parts) > 6 else "??"
                    if tty_field not in ("??", "?"):
                        # ps aux truncates ttys002 → s002; reconstruct full path
                        if tty_field.startswith("s") and tty_field[1:].isdigit():
                            self._pid_ttys[pid] = f"/dev/tty{tty_field}"
                        else:
                            self._pid_ttys[pid] = f"/dev/{tty_field}"
                    try:
                        lsof = subprocess.run(
                            ["lsof", "-p", pid],
                            capture_output=True, text=True, timeout=3
                        )
                        for lline in lsof.stdout.splitlines():
                            if "cwd" in lline:
                                cwd = lline.split()[-1]
                                cwds.setdefault(cwd, []).append(pid)
                                break
                    except (subprocess.TimeoutExpired, OSError):
                        pass
        except (subprocess.TimeoutExpired, OSError):
            pass
        return cwds

    @staticmethod
    def _extract_text(rec):
        """Extract user message text from a JSONL record."""
        msg = rec.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = ""
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c["text"]
                    break
        else:
            text = ""
        if text and not text.startswith("<"):
            return text
        return None

    def _detect_activity(self, jsonl_path):
        """Read the tail of a JSONL to determine if Claude is working or waiting.

        Returns 'working' if the last user/assistant record is a user message
        (meaning Claude is generating), or 'waiting' if it's an assistant message
        (meaning the session is idle, awaiting user input).
        """
        try:
            size = os.path.getsize(jsonl_path)
            # Read last ~8KB to find recent records
            read_size = min(size, 8192)
            with open(jsonl_path, "rb") as f:
                f.seek(size - read_size)
                tail = f.read().decode("utf-8", errors="replace")

            last_type = None
            for line in tail.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    rtype = rec.get("type")
                    if rtype in ("user", "assistant"):
                        last_type = rtype
                    elif rtype == "progress":
                        last_type = "user"  # progress = actively working
                except json.JSONDecodeError:
                    continue

            if last_type == "user":
                return "working"
            return "waiting"
        except (OSError, IOError):
            return "waiting"

    def _classify_status(self, meta, proj_name, running_cwds):
        """Determine session status: working, waiting, recent, or idle.

        Returns a preliminary status. For projects with running processes,
        scan() will post-process to limit 'working'/'waiting' to the N most
        recently active sessions (N = number of running processes).
        """
        # Match by encoding the CWD the same way Claude CLI encodes project dirs
        has_process = False
        matched_pids = []
        for cwd, pids in running_cwds.items():
            encoded_cwd = cwd.replace("/", "-")
            if encoded_cwd == proj_name:
                has_process = True
                matched_pids = pids
                break

        age_seconds = time.time() - meta.get("last_activity_ts", 0)

        # Store matched PIDs for post-processing in scan()
        if has_process:
            meta["_matched_pids"] = matched_pids
            # Detect working/waiting from JSONL tail — may be overridden by post-processing
            return self._detect_activity(meta.get("_jsonl_path", ""))

        # No running process — fallback to very recent activity
        if age_seconds < 60:
            return self._detect_activity(meta.get("_jsonl_path", ""))
        if age_seconds < 3600:
            return "recent"
        return "idle"

    def _parse_session(self, jsonl_path, session_id):
        """Parse session JSONL for metadata. Uses cache + head/tail for large files."""
        try:
            stat = os.stat(jsonl_path)
            size = stat.st_size
            mtime = stat.st_mtime
            if size == 0:
                return None

            # Check cache — return cached result if file unchanged
            cached = self._cache.get(jsonl_path)
            if cached and cached["mtime"] == mtime and cached["size"] == size:
                return cached["result"]

            first_ts = None
            last_ts = None
            model = None
            total_input = 0
            total_output = 0
            msg_count = 0
            first_prompt = None
            last_prompt = None

            if size > self.LARGE_FILE_THRESHOLD:
                # Large file: read head (4KB) + tail (16KB) only
                with open(jsonl_path, "r") as f:
                    head = f.read(4096)
                for line in head.splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("timestamp")
                    rt = rec.get("type")
                    if rt in ("user", "assistant") and ts and not first_ts:
                        first_ts = ts
                    if rt == "user" and first_prompt is None and not rec.get("isMeta"):
                        text = self._extract_text(rec)
                        if text:
                            first_prompt = text[:120].strip()

                read_tail = min(size, 16384)
                with open(jsonl_path, "rb") as f:
                    f.seek(size - read_tail)
                    tail = f.read().decode("utf-8", errors="replace")
                for line in tail.splitlines():
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("timestamp")
                    rt = rec.get("type")
                    if rt in ("user", "assistant") and ts:
                        last_ts = ts
                        msg_count += 1
                        msg = rec.get("message", {})
                        if rt == "user" and not rec.get("isMeta"):
                            text = self._extract_text(rec)
                            if text:
                                last_prompt = text[:120].strip()
                        if rt == "assistant":
                            m = msg.get("model")
                            if m and m != "<synthetic>":
                                model = m
                            usage = msg.get("usage", {})
                            total_input += usage.get("input_tokens", 0)
                            total_input += usage.get("cache_read_input_tokens", 0)
                            total_input += usage.get("cache_creation_input_tokens", 0)
                            total_output += usage.get("output_tokens", 0)
                # Estimate message count from file size
                msg_count = max(msg_count, size // 3000)
            else:
                # Small file: full parse
                with open(jsonl_path, "r") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = rec.get("timestamp")
                        rec_type = rec.get("type")
                        if rec_type in ("user", "assistant") and ts:
                            if first_ts is None:
                                first_ts = ts
                            last_ts = ts
                            msg_count += 1
                            msg = rec.get("message", {})
                            if rec_type == "user" and not rec.get("isMeta"):
                                text = self._extract_text(rec)
                                if text:
                                    if first_prompt is None:
                                        first_prompt = text[:120].strip()
                                    last_prompt = text[:120].strip()
                            if rec_type == "assistant":
                                m = msg.get("model")
                                if m and m != "<synthetic>":
                                    model = m
                                usage = msg.get("usage", {})
                                total_input += usage.get("input_tokens", 0)
                                total_input += usage.get("cache_read_input_tokens", 0)
                                total_input += usage.get("cache_creation_input_tokens", 0)
                                total_output += usage.get("output_tokens", 0)

            if not first_ts:
                return None

            last_activity_ts = 0
            if last_ts:
                try:
                    last_activity_ts = datetime.fromisoformat(last_ts).timestamp()
                except (ValueError, TypeError):
                    last_activity_ts = mtime

            result = {
                "session_id": session_id,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "last_activity_ts": last_activity_ts,
                "model": _short_model(model),
                "model_full": model,
                "total_input": total_input,
                "total_output": total_output,
                "total_tokens": total_input + total_output,
                "msg_count": msg_count,
                "first_prompt": first_prompt,
                "last_prompt": last_prompt,
            }
            # Cache the result
            self._cache[jsonl_path] = {"mtime": mtime, "size": size, "result": result}
            return result
        except (OSError, IOError):
            return None


def _short_model(model):
    """Shorten model name for display."""
    if not model:
        return "?"
    if "opus" in model:
        return "opus"
    if "sonnet" in model:
        return "sonnet"
    if "haiku" in model:
        return "haiku"
    return model.split("-")[0]


# Python script executed inside Docker containers to scan Claude sessions.
# Reads head + tail of each JSONL for efficiency with large files.
_DOCKER_SCANNER_SCRIPT = r"""
import json, os, sys, subprocess

claude_home = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.claude")
projects_dir = os.path.join(claude_home, "projects")

has_running = False
try:
    r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=3)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 11 and (parts[10].endswith("/claude") or parts[10] == "claude"):
            has_running = True
            break
except Exception:
    pass

results = []
if not os.path.isdir(projects_dir):
    print(json.dumps(results))
    sys.exit(0)

for proj_name in os.listdir(projects_dir):
    proj_path = os.path.join(projects_dir, proj_name)
    if not os.path.isdir(proj_path) or proj_name.startswith("."):
        continue
    for item in os.listdir(proj_path):
        if item in ("memory",) or item.startswith(".") or item.startswith("agent-"):
            continue
        item_path = os.path.join(proj_path, item)
        if not os.path.isdir(item_path):
            continue
        jsonl = os.path.join(proj_path, item + ".jsonl")
        if not os.path.exists(jsonl):
            continue
        try:
            size = os.path.getsize(jsonl)
            if size == 0:
                continue
            first_ts = last_ts = model = last_type = cwd = fp = lp = None
            ti = to = mc = 0
            with open(jsonl) as f:
                head = f.read(4096)
            for line in head.splitlines():
                try:
                    rec = json.loads(line)
                    if not cwd and rec.get("cwd"):
                        cwd = rec["cwd"]
                    ts = rec.get("timestamp")
                    rt = rec.get("type")
                    if rt in ("user", "assistant") and ts and not first_ts:
                        first_ts = ts
                    if rt == "user" and fp is None and not rec.get("isMeta"):
                        msg = rec.get("message", {})
                        ct = msg.get("content", "")
                        if isinstance(ct, str):
                            tx = ct
                        elif isinstance(ct, list):
                            tx = ""
                            for c in ct:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    tx = c["text"]
                                    break
                        else:
                            tx = ""
                        if tx and not tx.startswith("<"):
                            fp = tx[:120].strip()
                except Exception:
                    continue
            read_tail = min(size, 16384)
            with open(jsonl, "rb") as f:
                f.seek(size - read_tail)
                tail = f.read().decode("utf-8", errors="replace")
            for line in tail.splitlines():
                try:
                    rec = json.loads(line)
                    ts = rec.get("timestamp")
                    rt = rec.get("type")
                    if rt in ("user", "assistant") and ts:
                        last_ts = ts
                        last_type = rt
                        mc += 1
                        msg = rec.get("message", {})
                        if rt == "user" and not rec.get("isMeta"):
                            ct = msg.get("content", "")
                            if isinstance(ct, str):
                                tx = ct
                            elif isinstance(ct, list):
                                tx = ""
                                for c in ct:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        tx = c["text"]
                                        break
                            else:
                                tx = ""
                            if tx and not tx.startswith("<"):
                                lp = tx[:120].strip()
                        if rt == "assistant":
                            m = msg.get("model")
                            if m and m != "<synthetic>":
                                model = m
                            u = msg.get("usage", {})
                            ti += u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                            to += u.get("output_tokens", 0)
                    elif rt == "progress":
                        last_type = "user"
                except Exception:
                    continue
            if size > 16384:
                mc = max(mc, size // 3000)
            if not first_ts:
                continue
            results.append({"sid": item, "proj": proj_name, "cwd": cwd, "t0": first_ts, "t1": last_ts, "model": model, "lt": last_type, "ti": ti, "to": to, "mc": mc, "rp": has_running, "fp": fp, "lp": lp})
        except Exception:
            continue
print(json.dumps(results))
"""


class DockerSessionScanner:
    """Scans Docker containers for Claude Code session data in a background thread."""

    def __init__(self):
        self._sessions = []
        self._lock = threading.Lock()
        self._containers = []  # [(name, claude_home, workspace_label)]
        self._last_discover = 0
        self._last_scan = 0
        self._scanning = False

    def get_sessions(self):
        """Return cached container sessions. Thread-safe."""
        with self._lock:
            return list(self._sessions)

    def trigger_scan(self):
        """Start a background scan if enough time has passed."""
        if self._scanning:
            return
        now = time.time()
        if now - self._last_scan < DOCKER_SCAN_INTERVAL:
            return
        self._scanning = True
        self._last_scan = now
        thread = threading.Thread(target=self._do_scan, daemon=True)
        thread.start()

    def _do_scan(self):
        try:
            now = time.time()
            if now - self._last_discover >= DOCKER_DISCOVER_INTERVAL:
                self._containers = self._discover_containers()
                self._last_discover = now

            all_sessions = []
            for name, claude_home, workspace_label in self._containers:
                sessions = self._scan_container(name, claude_home, workspace_label)
                all_sessions.extend(sessions)

            with self._lock:
                self._sessions = all_sessions
        except Exception:
            pass
        finally:
            self._scanning = False

    def _discover_containers(self):
        """Find Docker containers with Claude Code session data."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return []

            containers = []
            for name in result.stdout.strip().split("\n"):
                name = name.strip()
                if not name:
                    continue
                # Check common home dirs for .claude/projects
                claude_home = None
                for home in ["/home/vscode", "/root", "/home/node", "/home/user"]:
                    try:
                        check = subprocess.run(
                            ["docker", "exec", name, "test", "-d",
                             f"{home}/.claude/projects"],
                            capture_output=True, timeout=3
                        )
                        if check.returncode == 0:
                            claude_home = f"{home}/.claude"
                            break
                    except (subprocess.TimeoutExpired, OSError):
                        continue
                if not claude_home:
                    continue

                # Get workspace label from /workspaces mount source
                workspace_label = None
                try:
                    inspect = subprocess.run(
                        ["docker", "inspect", name, "--format",
                         "{{range .Mounts}}{{if eq .Destination \"/workspaces\"}}"
                         "{{.Source}}{{end}}{{end}}"],
                        capture_output=True, text=True, timeout=3
                    )
                    host_path = inspect.stdout.strip()
                    if host_path:
                        workspace_label = os.path.basename(host_path)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                if not workspace_label:
                    # Fallback: extract from container name
                    workspace_label = name.split("_")[0] if "_" in name else name

                containers.append((name, claude_home, workspace_label))
            return containers
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
            return []

    def _scan_container(self, container_name, claude_home, workspace_label):
        """Run scanner script inside container and return session dicts."""
        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "python3", "-c",
                 _DOCKER_SCANNER_SCRIPT, claude_home],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return []

            raw = json.loads(result.stdout.strip())
            sessions = []
            for r in raw:
                # Determine project display name from cwd
                cwd = r.get("cwd", "")
                if cwd in ("/workspaces", "/workspaces/"):
                    project_display = workspace_label
                elif cwd:
                    project_display = os.path.basename(cwd) or workspace_label
                else:
                    project_display = decode_project_path(r["proj"])

                model = r.get("model")
                last_ts = r.get("t1")
                last_activity_ts = 0
                if last_ts:
                    try:
                        last_activity_ts = datetime.fromisoformat(last_ts).timestamp()
                    except (ValueError, TypeError):
                        pass

                # Classify status
                has_running = r.get("rp", False)
                last_type = r.get("lt")
                age = time.time() - last_activity_ts if last_activity_ts else 999999

                if has_running and age < 300:
                    status = "working" if last_type == "user" else "waiting"
                elif age < 3600:
                    status = "recent"
                else:
                    status = "idle"

                sessions.append({
                    "session_id": r["sid"],
                    "project": project_display,
                    "project_path": cwd or "",
                    "source": "docker",
                    "container": container_name,
                    "first_ts": r.get("t0"),
                    "last_ts": last_ts,
                    "last_activity_ts": last_activity_ts,
                    "model": _short_model(model),
                    "model_full": model,
                    "total_input": r.get("ti", 0),
                    "total_output": r.get("to", 0),
                    "total_tokens": r.get("ti", 0) + r.get("to", 0),
                    "msg_count": r.get("mc", 0),
                    "status": status,
                    "is_dir": True,
                    "first_prompt": r.get("fp"),
                    "last_prompt": r.get("lp"),
                })
            return sessions
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError,
                FileNotFoundError, KeyError):
            return []


class SessionCard(tk.Canvas):
    """A card widget displaying one session's info."""

    CARD_HEIGHT = 72
    RADIUS = 6

    def __init__(self, parent, **kwargs):
        super().__init__(parent, height=self.CARD_HEIGHT,
                         bg=COLOR_BG, highlightthickness=0, **kwargs)
        self._session = None

    def update_session(self, session):
        self._session = session
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        if w < 10:
            w = 300
        s = self._session
        if not s:
            return

        h = self.CARD_HEIGHT
        r = self.RADIUS
        px = 6
        py = 4

        # Determine if session is active in a terminal
        status = s.get("status", "idle")
        is_active = status in ("working", "waiting")

        # Card background — dimmed for inactive sessions
        card_bg = COLOR_CARD_BG if is_active else COLOR_CARD_BG_INACTIVE
        self._rounded_rect(1, 1, w - 1, h - 2, r, fill=card_bg, outline="")

        # Choose text colors based on active state
        fg_primary = COLOR_FG if is_active else "#6c7086"
        fg_dim = COLOR_FG_DIM if is_active else "#585b70"
        fg_dimmer = "#7f849c" if is_active else "#505469"

        # Status dot
        status_colors = {
            "working": STATUS_WORKING,
            "waiting": STATUS_WAITING,
            "recent": STATUS_RECENT,
            "idle": STATUS_IDLE,
        }
        dot_color = status_colors.get(status, STATUS_IDLE)
        dot_r = 4
        dot_x = px + dot_r + 2
        dot_y = py + 8
        self.create_oval(
            dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r,
            fill=dot_color, outline=""
        )

        # Project name + session name on line 1
        project_name = s.get("project", "?")
        if s.get("is_fork"):
            project_name = "\u21b3 " + project_name  # ↳
        if s.get("source") == "docker":
            project_name = "\U0001f433 " + project_name  # 🐳

        # Model badge (right side) — measure first to know space for session name
        model = s.get("model", "?")
        if is_active:
            model_color = COLOR_MAUVE if model == "opus" else (
                COLOR_BLUE if model == "sonnet" else COLOR_FG_DIM
            )
        else:
            model_color = fg_dim
        self.create_text(
            w - px - 4, py + 2, anchor="ne",
            text=model,
            font=("SF Mono", 10), fill=model_color
        )

        display_name = s.get("display_name", "")
        text_x = dot_x + dot_r + 6
        if display_name:
            # Project name bold, then separator, then truncated session name
            prefix = project_name + "  "
            self.create_text(
                text_x, py + 2, anchor="nw",
                text=prefix,
                font=("SF Mono", 11, "bold"), fill=fg_primary
            )
            # Measure prefix width to position session name after it
            prefix_px = len(prefix) * 7.5  # approximate char width at size 11
            name_x = text_x + prefix_px
            # Available space: total width minus name_x, model badge (~60px), padding
            avail = w - name_x - 60
            max_chars = max(5, int(avail // 6.5))
            name_text = display_name if len(display_name) <= max_chars else display_name[:max_chars - 1] + "\u2026"
            self.create_text(
                name_x, py + 3, anchor="nw",
                text=name_text,
                font=("SF Mono", 10), fill=fg_dim
            )
        else:
            self.create_text(
                text_x, py + 2, anchor="nw",
                text=project_name,
                font=("SF Mono", 11, "bold"), fill=fg_primary
            )

        # Second line: first prompt
        first_prompt = s.get("first_prompt", "")
        if first_prompt:
            max_chars = max(20, (w - 40) // 7)
            fp_text = first_prompt if len(first_prompt) <= max_chars else first_prompt[:max_chars - 1] + "\u2026"
            self.create_text(
                dot_x + dot_r + 6, py + 17, anchor="nw",
                text=fp_text,
                font=("SF Mono", 9), fill=fg_dim
            )

        # Third line: last prompt
        last_prompt = s.get("last_prompt", "")
        if last_prompt:
            max_chars = max(20, (w - 40) // 7)
            lp_text = last_prompt if len(last_prompt) <= max_chars else last_prompt[:max_chars - 1] + "\u2026"
            self.create_text(
                dot_x + dot_r + 6, py + 31, anchor="nw",
                text="\u25b8 " + lp_text,  # ▸ prefix
                font=("SF Mono", 9), fill=fg_dimmer
            )

        # Fourth line: last activity, tokens, messages
        line4_y = py + 45
        last_activity = format_relative_time(s.get("last_ts"))
        tokens = format_tokens(s.get("total_tokens", 0))
        msgs = s.get("msg_count", 0)
        duration = format_duration(s.get("first_ts"), s.get("last_ts"))

        detail = f"{last_activity}"
        if duration:
            detail += f" \u00b7 {duration}"
        detail += f" \u00b7 {tokens} tok \u00b7 {msgs} msgs"

        self.create_text(
            dot_x + dot_r + 6, line4_y, anchor="nw",
            text=detail,
            font=("SF Mono", 9), fill=fg_dim
        )

    def _rounded_rect(self, x1, y1, x2, y2, r, **kwargs):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2,
            x1 + r, y2, x1, y2, x1, y2 - r,
            x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(pts, smooth=True, **kwargs)


class SessionTracker:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Session Tracker")
        self.root.configure(bg="systemTransparent")
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparent", True)
        self.root.overrideredirect(True)
        self.root.minsize(320, 100)
        self._corner_radius = 16

        self.scanner = SessionScanner()
        self.docker_scanner = DockerSessionScanner()
        self._sessions = []
        self._session_names = self._load_names()

        # Right-click context menu
        self._ctx_menu = tk.Menu(self.root, tearoff=0)
        self._ctx_menu.add_command(label="Resume session", command=self._resume_session)
        self._ctx_menu.add_command(label="Close session", command=self._close_session)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Rename\u2026", command=self._rename_session)
        self._ctx_menu.add_command(label="Fork session", command=self._fork_session)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Clear name", command=self._clear_session_name)
        self._ctx_menu_session = None

        # Background canvas for rounded shape
        self._bg_canvas = tk.Canvas(
            self.root, bg="systemTransparent", highlightthickness=0
        )
        self._bg_canvas.pack(fill=tk.BOTH, expand=True)
        self._bg_canvas.bind("<Configure>", self._on_bg_configure)

        # Drag setup
        self._drag_x = 0
        self._drag_y = 0
        self._drag_happened = False
        self._bg_canvas.bind("<Button-1>", self._start_drag)
        self._bg_canvas.bind("<B1-Motion>", self._on_drag)

        # Content frame on canvas
        content = tk.Frame(self._bg_canvas, bg=COLOR_BG)
        self._content_id = self._bg_canvas.create_window(
            0, 0, window=content, anchor="nw"
        )
        content.bind("<Button-1>", self._start_drag)
        content.bind("<B1-Motion>", self._on_drag)

        # Header row
        header = tk.Frame(content, bg=COLOR_BG)
        header.pack(fill=tk.X, padx=8, pady=(6, 2))

        title = tk.Label(
            header, text="Sessions", font=("SF Mono", 12, "bold"),
            bg=COLOR_BG, fg=COLOR_FG
        )
        title.pack(side=tk.LEFT)

        self._count_label = tk.Label(
            header, text="", font=("SF Mono", 10),
            bg=COLOR_BG, fg=COLOR_FG_DIM
        )
        self._count_label.pack(side=tk.LEFT, padx=(6, 0))

        close_btn = tk.Label(
            header, text="x", font=("SF Mono", 10),
            bg=COLOR_BG, fg=COLOR_FG_DIM, cursor="hand2"
        )
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda e: self.root.destroy())

        new_btn = tk.Label(
            header, text="+", font=("SF Mono", 12, "bold"),
            bg=COLOR_BG, fg=COLOR_FG_DIM, cursor="hand2"
        )
        new_btn.pack(side=tk.RIGHT, padx=(0, 6))
        new_btn.bind("<Button-1>", self._show_new_session_menu)
        new_btn.bind("<Enter>", lambda e: new_btn.configure(fg=COLOR_FG))
        new_btn.bind("<Leave>", lambda e: new_btn.configure(fg=COLOR_FG_DIM))
        self._new_session_menu = tk.Menu(self.root, tearoff=0)

        # Scrollable session list
        list_frame = tk.Frame(content, bg=COLOR_BG)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))

        self._list_canvas = tk.Canvas(
            list_frame, bg=COLOR_BG, highlightthickness=0, borderwidth=0
        )
        self._list_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._card_frame = tk.Frame(self._list_canvas, bg=COLOR_BG)
        self._card_frame_id = self._list_canvas.create_window(
            0, 0, window=self._card_frame, anchor="nw"
        )

        # Update scroll region when cards change size
        self._card_frame.bind("<Configure>", self._on_card_frame_configure)
        self._list_canvas.bind("<Configure>", self._on_list_canvas_configure)

        # Mousewheel scrolling (macOS)
        self._list_canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._card_frame.bind("<MouseWheel>", self._on_mousewheel)

        # Resize grip
        grip = tk.Label(
            content, text="\u22ee\u22ee", font=("SF Mono", 12),
            bg=COLOR_BG, fg="#585b70", cursor="bottom_right_corner",
            padx=4, pady=2,
        )
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<Button-1>", self._start_resize)
        grip.bind("<B1-Motion>", self._on_resize_drag)
        grip.bind("<Enter>", lambda e: grip.configure(fg="#a6adc8"))
        grip.bind("<Leave>", lambda e: grip.configure(fg="#585b70"))

        self._content_frame = content
        self._no_drag_widgets = {id(grip), id(close_btn)}
        self._bind_drag_recursive(content)

        # Session card pool
        self._cards = []

        # Position window
        init_w = 380
        init_h = 200
        screen_w = self.root.winfo_screenwidth()
        self.root.geometry(f"{init_w}x{init_h}+{screen_w - init_w - 20}+130")

        # Clean up stale command files
        for f in (CMD_FILE, CMD_RESULT_FILE):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

        # Schedule tasks
        self.root.after(500, self._initial_scan)
        self.root.after(CMD_POLL_INTERVAL_MS, self._poll_commands)
        self.root.after(1000, self._keep_on_top)

        # Menu bar
        self._setup_menu_bar()

    # --- Rounded background ---

    def _rounded_rect_points(self, x1, y1, x2, y2, r, segments=16):
        pts = []
        for corner_cx, corner_cy, start_angle in [
            (x2 - r, y1 + r, -math.pi / 2),
            (x2 - r, y2 - r, 0),
            (x1 + r, y2 - r, math.pi / 2),
            (x1 + r, y1 + r, math.pi),
        ]:
            for i in range(segments + 1):
                angle = start_angle + (math.pi / 2) * i / segments
                pts.extend([
                    corner_cx + r * math.cos(angle),
                    corner_cy + r * math.sin(angle)
                ])
        return pts

    def _draw_rounded_bg(self):
        c = self._bg_canvas
        c.delete("bg")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 2 or h < 2:
            return
        pts = self._rounded_rect_points(0, 0, w, h, self._corner_radius)
        c.create_polygon(pts, fill=COLOR_BG, outline="", tags="bg")
        c.tag_lower("bg")
        inset = 2
        c.coords(self._content_id, inset, inset)
        c.itemconfigure(self._content_id, width=w - inset * 2, height=h - inset * 2)

    def _on_bg_configure(self, event=None):
        self._draw_rounded_bg()

    # --- Card list scrolling ---

    def _on_card_frame_configure(self, event=None):
        """Update scroll region when the card frame changes size."""
        self._list_canvas.configure(scrollregion=self._list_canvas.bbox("all"))

    def _on_list_canvas_configure(self, event=None):
        """Keep card frame width in sync with canvas width."""
        self._list_canvas.itemconfigure(self._card_frame_id, width=event.width)

    def _on_mousewheel(self, event):
        """Scroll the card list on mousewheel."""
        # Only scroll if content is taller than the visible area
        bbox = self._list_canvas.bbox("all")
        if bbox and bbox[3] > self._list_canvas.winfo_height():
            self._list_canvas.yview_scroll(-event.delta, "units")

    def _bind_card_scroll(self, card):
        """Bind mousewheel scrolling on a session card."""
        card.bind("<MouseWheel>", self._on_mousewheel)

    # --- Drag & resize ---

    def _bind_drag_recursive(self, widget):
        if id(widget) not in self._no_drag_widgets:
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)
        for child in widget.winfo_children():
            self._bind_drag_recursive(child)

    def _start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y
        self._drag_happened = False

    def _on_drag(self, event):
        self._drag_happened = True
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _start_resize(self, event):
        self._resize_x = event.x_root
        self._resize_y = event.y_root
        self._resize_w = self.root.winfo_width()
        self._resize_h = self.root.winfo_height()

    def _on_resize_drag(self, event):
        dw = event.x_root - self._resize_x
        dh = event.y_root - self._resize_y
        new_w = max(320, self._resize_w + dw)
        new_h = max(100, self._resize_h + dh)
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self.root.geometry(f"{new_w}x{new_h}+{x}+{y}")

    def _keep_on_top(self):
        self.root.attributes("-topmost", False)
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.after(3000, self._keep_on_top)

    # --- Session scanning ---

    def _initial_scan(self):
        self._refresh_sessions()
        self.root.after(SCAN_INTERVAL_MS, self._scheduled_scan)

    def _scheduled_scan(self):
        self._refresh_sessions()
        self.root.after(SCAN_INTERVAL_MS, self._scheduled_scan)

    def _refresh_sessions(self):
        """Scan for sessions and update the card display."""
        self._sessions = self.scanner.scan()
        # Merge Docker container sessions
        self.docker_scanner.trigger_scan()
        docker_sessions = self.docker_scanner.get_sessions()
        self._sessions.extend(docker_sessions)
        self._sessions.sort(key=lambda s: s.get("last_activity_ts", 0), reverse=True)
        # Inject display names and parent info
        for s in self._sessions:
            sid = s.get("session_id", "")
            custom = self._get_name(sid)
            parent = self._get_parent(sid)
            s["display_name"] = custom if custom else (s.get("first_prompt") or "")
            s["parent_session"] = parent
            s["is_fork"] = parent is not None
        # Sort forks immediately after their parent
        self._sessions = self._sort_with_forks(self._sessions)
        self._update_cards()

    def _sort_with_forks(self, sessions):
        """Re-sort so child sessions appear right after their parent."""
        by_id = {s["session_id"]: s for s in sessions}
        children = {}  # parent_id -> [child sessions]
        roots = []
        for s in sessions:
            parent = s.get("parent_session")
            if parent and parent in by_id:
                children.setdefault(parent, []).append(s)
            else:
                roots.append(s)
        # Build final list: parent followed by its children
        result = []
        seen = set()
        for s in roots:
            sid = s["session_id"]
            if sid in seen:
                continue
            seen.add(sid)
            result.append(s)
            for child in children.get(sid, []):
                if child["session_id"] not in seen:
                    seen.add(child["session_id"])
                    result.append(child)
        # Add any orphaned forks (parent not in current view)
        for s in sessions:
            if s["session_id"] not in seen:
                result.append(s)
        return result

    def _update_cards(self):
        """Update the card widgets to reflect current sessions."""
        sessions = self._sessions

        # Update count label
        active = sum(1 for s in sessions if s.get("status") in ("working", "waiting"))
        docker = sum(1 for s in sessions if s.get("source") == "docker")
        label = f"({active} active, {len(sessions)} total"
        if docker:
            label += f", {docker} docker"
        label += ")"
        self._count_label.configure(text=label)

        # Grow card pool if needed
        while len(self._cards) < len(sessions):
            card = SessionCard(self._card_frame)
            card.pack(fill=tk.X, pady=(0, 2))
            # Bind drag to new cards
            card.bind("<Button-1>", self._start_drag)
            card.bind("<B1-Motion>", self._on_drag)
            self._bind_card_scroll(card)
            self._cards.append(card)

        # Hide excess cards
        for i in range(len(sessions), len(self._cards)):
            self._cards[i].pack_forget()

        # Update visible cards
        for i, session in enumerate(sessions):
            card = self._cards[i]
            if not card.winfo_ismapped():
                card.pack(fill=tk.X, pady=(0, 2))
            card.update_session(session)
            # Click to activate terminal window
            card.bind("<ButtonRelease-1>", lambda e, s=session: self._on_card_release(e, s))
            card.configure(cursor="hand2" if session.get("tty") else "")
            # Bind right-click for rename (macOS: Button-2, also Control-click)
            card.bind("<Button-2>", lambda e, s=session: self._show_ctx_menu(e, s))
            card.bind("<Control-Button-1>", lambda e, s=session: self._show_ctx_menu(e, s))

    # --- Session names ---

    def _load_names(self):
        try:
            with open(SESSION_NAMES_FILE, "r") as f:
                raw = json.load(f)
            # Migrate old format: {"id": "string"} → {"id": {"name": "string", "parent": null}}
            migrated = False
            for k, v in list(raw.items()):
                if isinstance(v, str):
                    raw[k] = {"name": v, "parent": None}
                    migrated = True
            if migrated:
                self._session_names = raw
                self._save_names()
            return raw
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_names(self):
        try:
            with open(SESSION_NAMES_FILE, "w") as f:
                json.dump(self._session_names, f, indent=2)
        except OSError:
            pass

    def _get_name(self, sid):
        """Get custom name for a session, or None."""
        entry = self._session_names.get(sid)
        if isinstance(entry, dict):
            return entry.get("name")
        if isinstance(entry, str):
            return entry  # old format fallback
        return None

    def _get_parent(self, sid):
        """Get parent session ID, or None."""
        entry = self._session_names.get(sid)
        if isinstance(entry, dict):
            return entry.get("parent")
        return None

    def _show_ctx_menu(self, event, session):
        self._ctx_menu_session = session
        # Enable/disable items based on whether session is active
        is_active = session.get("status") in ("working", "waiting")
        self._ctx_menu.entryconfigure("Resume session",
                                       state="disabled" if is_active else "normal")
        self._ctx_menu.entryconfigure("Close session",
                                       state="normal" if is_active else "disabled")
        # Position menu at click location (use root coords)
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    def _rename_session(self):
        s = self._ctx_menu_session
        if not s:
            return
        sid = s.get("session_id", "")
        current = self._get_name(sid) or ""
        new_name = simpledialog.askstring(
            "Rename Session",
            f"Name for session in {s.get('project', '?')}:",
            initialvalue=current,
            parent=self.root,
        )
        if new_name is not None:
            new_name = new_name.strip()
            existing = self._session_names.get(sid, {})
            if not isinstance(existing, dict):
                existing = {"name": None, "parent": None}
            if new_name:
                existing["name"] = new_name
                self._session_names[sid] = existing
            else:
                existing["name"] = None
                if not existing.get("parent"):
                    self._session_names.pop(sid, None)
                else:
                    self._session_names[sid] = existing
            self._save_names()
            self._refresh_sessions()

    def _clear_session_name(self):
        s = self._ctx_menu_session
        if not s:
            return
        sid = s.get("session_id", "")
        existing = self._session_names.get(sid, {})
        if isinstance(existing, dict) and existing.get("parent"):
            # Keep parent link, just clear the name
            existing["name"] = None
            self._session_names[sid] = existing
        else:
            self._session_names.pop(sid, None)
        self._save_names()
        self._refresh_sessions()

    # --- Terminal activation ---

    def _on_card_release(self, event, session):
        """Handle click (non-drag) on a session card — activate its terminal."""
        if not self._drag_happened:
            self._activate_terminal(session, event)

    def _activate_terminal(self, session, event=None):
        """Bring the Terminal window for this session to the front."""
        tty = session.get("tty")
        if not tty:
            return
        # Brief flash on the card to show the click registered
        if event and event.widget:
            card = event.widget
            card.create_rectangle(
                0, 0, card.winfo_width(), SessionCard.CARD_HEIGHT - 2,
                outline=COLOR_BLUE, width=2, tags="flash"
            )
            card.after(300, lambda: card.delete("flash"))
        # Activate the Terminal window with the matching TTY
        script = (
            'tell application "Terminal"\n'
            '    repeat with w in windows\n'
            '        repeat with t in tabs of w\n'
            f'            if tty of t is "{tty}" then\n'
            '                set index of w to 1\n'
            '                set selected tab of w to t\n'
            '                activate\n'
            '                return\n'
            '            end if\n'
            '        end repeat\n'
            '    end repeat\n'
            'end tell'
        )
        try:
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    # --- New session ---

    def _show_new_session_menu(self, event):
        """Show a dropdown of known project directories to start a new session."""
        menu = self._new_session_menu
        menu.delete(0, tk.END)
        # Gather known projects, sorted by most recent session activity
        projects = []  # (display_name, real_path, last_activity)
        if os.path.isdir(PROJECTS_DIR):
            for proj_name in os.listdir(PROJECTS_DIR):
                proj_path = os.path.join(PROJECTS_DIR, proj_name)
                if not os.path.isdir(proj_path) or proj_name.startswith("."):
                    continue
                display = decode_project_path(proj_name)
                real_path = resolve_project_path(proj_name)
                # Find most recent JSONL mtime for sorting
                latest = 0
                for item in os.listdir(proj_path):
                    if item.endswith(".jsonl") and not item.startswith("agent-"):
                        try:
                            mt = os.stat(os.path.join(proj_path, item)).st_mtime
                            if mt > latest:
                                latest = mt
                        except OSError:
                            pass
                projects.append((display, real_path, latest))
        # Sort by most recently active first
        projects.sort(key=lambda x: x[2], reverse=True)
        for display, real_path, _ in projects:
            menu.add_command(
                label=display,
                command=lambda p=real_path: self._launch_new_session(p)
            )
        if not projects:
            menu.add_command(label="(no projects found)", state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _launch_new_session(self, project_path):
        """Open a new Terminal window with a fresh Claude session."""
        cmd = f"cd {shlex.quote(project_path)} && claude"
        try:
            subprocess.Popen([
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd}"'
            ])
        except OSError as e:
            print(f"New session launch failed: {e}")
        self.root.after(2000, self._refresh_sessions)

    # --- Close ---

    def _close_session(self):
        """Terminate an active Claude session by sending SIGTERM to its process."""
        s = self._ctx_menu_session
        if not s:
            return
        tty = s.get("tty")
        if not tty:
            return
        # Find the Claude PID on this TTY
        pid_to_kill = None
        for pid, pid_tty in self.scanner._pid_ttys.items():
            if pid_tty == tty:
                pid_to_kill = pid
                break
        if not pid_to_kill:
            return
        try:
            os.kill(int(pid_to_kill), signal.SIGTERM)
        except (OSError, ValueError) as e:
            print(f"Close session failed: {e}")
        # Refresh after a moment to reflect the change
        self.root.after(2000, self._refresh_sessions)

    # --- Resume ---

    def _resume_session(self):
        """Resume an inactive session in a new Terminal window."""
        s = self._ctx_menu_session
        if not s:
            return
        sid = s.get("session_id", "")
        project_path = s.get("project_path", "")
        if not sid:
            return
        # Build the resume command
        if project_path:
            cmd = f"cd {shlex.quote(project_path)} && claude --resume {sid}"
        else:
            cmd = f"claude --resume {sid}"
        try:
            subprocess.Popen([
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd}"'
            ])
        except OSError as e:
            print(f"Resume launch failed: {e}")
        # Refresh after a moment to pick up the new process
        self.root.after(2000, self._refresh_sessions)

    # --- Fork ---

    def _fork_session(self):
        """Fork a session: copy its JSONL with a new ID and open in a new terminal."""
        s = self._ctx_menu_session
        if not s:
            return
        old_sid = s.get("session_id", "")
        jsonl_path = s.get("_jsonl_path", "")
        project_path = s.get("project_path", "")

        if not jsonl_path or not os.path.exists(jsonl_path):
            return

        # Generate new session ID
        new_sid = str(_uuid.uuid4())
        proj_dir = os.path.dirname(jsonl_path)
        new_jsonl = os.path.join(proj_dir, new_sid + ".jsonl")
        new_session_dir = os.path.join(proj_dir, new_sid)

        # Copy JSONL with rewritten sessionId
        try:
            with open(jsonl_path, "r") as src:
                content = src.read()
            content = content.replace(old_sid, new_sid)
            with open(new_jsonl, "w") as dst:
                dst.write(content)
            # Create session directory (marks as active)
            os.makedirs(new_session_dir, exist_ok=True)
        except (OSError, IOError) as e:
            print(f"Fork failed: {e}")
            return

        # Record parent relationship and auto-name
        parent_name = self._get_name(old_sid) or s.get("first_prompt") or s.get("project", "session")
        fork_name = f"{parent_name} (fork)"
        self._session_names[new_sid] = {"name": fork_name, "parent": old_sid}
        self._save_names()

        # Launch in new terminal
        if project_path:
            cmd = f"cd {shlex.quote(project_path)} && claude --resume {new_sid}"
        else:
            cmd = f"claude --resume {new_sid}"
        try:
            subprocess.Popen([
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd}"'
            ])
        except OSError as e:
            print(f"Terminal launch failed: {e}")

        # Prompt user to rename the fork
        self.root.after(500, lambda: self._rename_fork(new_sid, fork_name))
        # Refresh immediately to show the new session
        self.root.after(1000, self._refresh_sessions)

    def _rename_fork(self, sid, default_name):
        """Prompt the user to name a newly forked session."""
        new_name = simpledialog.askstring(
            "Name Fork",
            "Name for the forked session:",
            initialvalue=default_name,
            parent=self.root,
        )
        if new_name is not None and new_name.strip():
            entry = self._session_names.get(sid, {"name": None, "parent": None})
            entry["name"] = new_name.strip()
            self._session_names[sid] = entry
            self._save_names()
            self._refresh_sessions()

    # --- Command server ---

    def _poll_commands(self):
        try:
            if os.path.exists(CMD_FILE):
                with open(CMD_FILE, "r") as f:
                    cmd = f.read().strip()
                os.remove(CMD_FILE)
                if cmd:
                    result = self._execute_command(cmd)
                    with open(CMD_RESULT_FILE, "w") as f:
                        f.write(result)
        except (OSError, IOError):
            pass
        self.root.after(CMD_POLL_INTERVAL_MS, self._poll_commands)

    def _execute_command(self, cmd):
        parts = cmd.split(None, 1)
        action = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        if action == "screenshot":
            return self._cmd_screenshot()
        elif action == "refresh":
            self._refresh_sessions()
            return json.dumps({"ok": True, "action": "refresh"})
        elif action == "state":
            return self._cmd_state()
        elif action == "resize":
            return self._cmd_resize(args)
        elif action == "move":
            return self._cmd_move(args)
        else:
            return json.dumps({"ok": False, "error": f"Unknown command: {action}"})

    def _cmd_screenshot(self):
        self.root.update_idletasks()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        margin = 4
        try:
            subprocess.run(
                ["screencapture", "-R",
                 f"{x - margin},{y - margin},{w + margin * 2},{h + margin * 2}",
                 SCREENSHOT_FILE],
                timeout=5, capture_output=True
            )
            return json.dumps({"ok": True, "action": "screenshot", "path": SCREENSHOT_FILE})
        except subprocess.TimeoutExpired:
            return json.dumps({"ok": False, "error": "screencapture timed out"})

    def _cmd_state(self):
        return json.dumps({
            "ok": True,
            "action": "state",
            "window": {
                "x": self.root.winfo_rootx(),
                "y": self.root.winfo_rooty(),
                "width": self.root.winfo_width(),
                "height": self.root.winfo_height(),
            },
            "session_count": len(self._sessions),
            "sessions": [
                {
                    "project": s.get("project"),
                    "session_id": s.get("session_id", "")[:8],
                    "model": s.get("model"),
                    "status": s.get("status"),
                    "source": s.get("source", "local"),
                    "is_fork": s.get("is_fork", False),
                    "parent_session": s.get("parent_session"),
                    "tty": s.get("tty"),
                    "total_tokens": s.get("total_tokens", 0),
                    "msg_count": s.get("msg_count", 0),
                    "last_activity": format_relative_time(s.get("last_ts")),
                }
                for s in self._sessions
            ],
        })

    def _cmd_resize(self, args):
        try:
            w, h = args.lower().split("x")
            w, h = int(w), int(h)
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            return json.dumps({"ok": True, "action": "resize"})
        except (ValueError, AttributeError):
            return json.dumps({"ok": False, "error": "Usage: resize WxH"})

    def _cmd_move(self, args):
        try:
            x, y = args.split(",")
            x, y = int(x), int(y)
            self.root.geometry(f"+{x}+{y}")
            return json.dumps({"ok": True, "action": "move"})
        except (ValueError, AttributeError):
            return json.dumps({"ok": False, "error": "Usage: move X,Y"})

    # --- Menu bar ---

    def _setup_menu_bar(self):
        if not HAS_APPKIT:
            return

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )

        status_bar = NSStatusBar.systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self._status_item.setTitle_("\u25ce")  # ◎

        menu = NSMenu.alloc().init()

        show_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show/Hide", "toggleWindow:", ""
        )
        refresh_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Refresh", "refreshData:", ""
        )
        menu.addItem_(show_item)
        menu.addItem_(refresh_item)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "quitApp:", "q"
        )
        menu.addItem_(quit_item)

        self._menu_delegate = _SessionMenuDelegate.alloc().initWithTracker_(self)
        for item in [show_item, refresh_item, quit_item]:
            item.setTarget_(self._menu_delegate)

        self._status_item.setMenu_(menu)

    def _toggle_window(self):
        if not hasattr(self, "_hidden"):
            self._hidden = False
            self._saved_pos = None

        if self._hidden:
            if self._saved_pos:
                self.root.geometry(self._saved_pos)
            self.root.attributes("-topmost", True)
            self.root.lift()
            self._hidden = False
        else:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            self._saved_pos = f"{w}x{h}+{x}+{y}"
            self.root.geometry(f"+{-9999}+{-9999}")
            self._hidden = True

    def run(self):
        self.root.mainloop()


# PyObjC delegate
if HAS_APPKIT:
    class _SessionMenuDelegate(NSObject):
        def initWithTracker_(self, tracker):
            self = objc_super(_SessionMenuDelegate, self).init()
            if self is not None:
                self._tracker = tracker
            return self

        def toggleWindow_(self, sender):
            self._tracker._toggle_window()

        def refreshData_(self, sender):
            self._tracker._refresh_sessions()

        def quitApp_(self, sender):
            self._tracker.root.destroy()


if __name__ == "__main__":
    tracker = SessionTracker()
    tracker.run()
