# ocd_backend.py
import os
import re
import socket
import subprocess


class OpenOCDSession:
    """Context manager for atomic OpenOCD TCL socket transactions (port 6666)."""
    def __init__(self, host="localhost", port=6666, timeout=2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None

    def __enter__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def send(self, command: str) -> str:
        """Sends a TCL command terminated with 0x1A and returns the response string."""
        # Prevent double-bracket evaluation ('invalid command name "running"') in JimTCL
        if command.strip() == "[[target current] curstate]":
            command = "[target current] curstate"

        self.sock.sendall(command.encode("utf-8") + b"\x1a")
        resp = b""
        while b"\x1a" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                break
            resp += chunk
        return resp.decode("utf-8", errors="ignore").strip("\x1a\r\n")


def start_openocd_console(exe_path: str, cfg_path: str):
    """Spawns OpenOCD in a visible console window with live colorized output on Windows."""
    cfg_dir = os.path.dirname(cfg_path)
    cfg_file = os.path.basename(cfg_path)

    if os.name == "nt":
        # Merge stderr into stdout via cmd.exe first so PowerShell never wraps lines in RemoteException
        ps_script = (
            "$host.UI.RawUI.WindowTitle = 'OpenOCD Live Console'; "
            f"cmd /c '\"\"{exe_path}\" -s \"{cfg_dir}\" -f \"{cfg_file}\" 2>&1\"' | ForEach-Object {{ "
            "$line = $_.ToString(); "
            "if ($line -match 'Error') { Write-Host $line -ForegroundColor Red } "
            "elseif ($line -match 'Warn') { Write-Host $line -ForegroundColor Yellow } "
            "elseif ($line -match 'Info') { Write-Host $line -ForegroundColor Cyan } "
            "else { Write-Host $line -ForegroundColor Gray } }"
        )
        cmd = ["powershell", "-NoLogo", "-NoExit", "-Command", ps_script]
        return subprocess.Popen(cmd, cwd=cfg_dir, creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        cmd = [exe_path, "-s", cfg_dir, "-f", cfg_file]
        return subprocess.Popen(cmd, cwd=cfg_dir)


def stop_openocd_console(proc):
    """Cleanly terminates OpenOCD and any wrapper console process."""
    if not proc:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc.terminate()
        proc.wait()


def parse_map_file(filepath: str):
    """
    Parses a GNU ld .map file and returns:
      (symbol_table, var_symbols, func_symbols)
    """
    symbol_table = {}
    var_symbols = {}
    func_symbols = {}

    top_section = ""
    sub_section = ""
    pending_sub_sym = None
    pending_sub_kind = None

    ignored_prefixes = (
        "__", "_start", "_end", "_edata", "_fbss", "_sdata", "_etext",
        "_heap", "_stack", "_impure", "_global_impure", "_ctype_"
    )
    ignored_subsections = {"unlikely", "exit", "startup", "hot", "stub"}

    def classify_section(sec_str):
        if any(k in sec_str for k in (".text", ".init", ".ram_code", ".itim", ".fast_code", ".vector")):
            return "func"
        if any(k in sec_str for k in (".data", ".bss", ".sdata", ".sbss", ".uncached", "COMMON", ".noinit", ".l2")):
            return "var"
        return None

    with open(filepath, "r", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if not stripped:
                continue

            # 1. Top-level output section (column 0)
            if not line[0].isspace():
                m_top = re.match(r"^(\.[a-zA-Z0-9_.*]+|COMMON)\b", stripped)
                if m_top:
                    top_section = m_top.group(1)
                    sub_section = top_section
                    pending_sub_sym = None
                    pending_sub_kind = None

            # 2. Input subsection (e.g. " .text.my_func" or " .bss.my_var")
            m_sub = re.match(
                r"^\s+(\.[a-zA-Z0-9_.$*]+|COMMON)(?:\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+))?",
                line,
            )
            if m_sub:
                sub_section = m_sub.group(1)
                kind = classify_section(sub_section) or classify_section(top_section)

                sub_sym = None
                for prefix in (".text.", ".data.", ".bss.", ".sdata.", ".sbss.", ".uncached_lsram."):
                    if sub_section.startswith(prefix):
                        candidate = sub_section[len(prefix):]
                        if candidate not in ignored_subsections and re.match(r"^[a-zA-Z_]\w*$", candidate):
                            sub_sym = candidate
                        break

                if m_sub.group(2) and m_sub.group(3):
                    addr_int = int(m_sub.group(2), 16)
                    size_int = int(m_sub.group(3), 16)
                    if addr_int > 0 and size_int > 0 and sub_sym and not sub_sym.startswith(ignored_prefixes):
                        norm_addr = f"0x{addr_int:08X}"
                        symbol_table[sub_sym] = norm_addr
                        if kind == "func":
                            func_symbols[sub_sym] = norm_addr
                        elif kind == "var":
                            var_symbols[sub_sym] = norm_addr
                    pending_sub_sym = None
                    pending_sub_kind = None
                else:
                    pending_sub_sym = sub_sym
                    pending_sub_kind = kind
                continue

            # 3. Wrapped subsection continuation line
            if pending_sub_sym:
                m_wrap = re.match(r"^\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\S+", line)
                if m_wrap:
                    addr_int = int(m_wrap.group(1), 16)
                    size_int = int(m_wrap.group(2), 16)
                    if addr_int > 0 and size_int > 0 and not pending_sub_sym.startswith(ignored_prefixes):
                        norm_addr = f"0x{addr_int:08X}"
                        symbol_table[pending_sub_sym] = norm_addr
                        if pending_sub_kind == "func":
                            func_symbols[pending_sub_sym] = norm_addr
                        elif pending_sub_kind == "var":
                            var_symbols[pending_sub_sym] = norm_addr
                    pending_sub_sym = None
                    pending_sub_kind = None
                    continue
                pending_sub_sym = None
                pending_sub_kind = None

            # 4. Standard Global Symbol Line
            m_sym = re.match(r"^\s+(0x[0-9a-fA-F]+)\s+([a-zA-Z_]\w*)$", line)
            if m_sym:
                addr_str, sym_name = m_sym.group(1), m_sym.group(2)
                if sym_name.startswith(ignored_prefixes):
                    continue
                addr_int = int(addr_str, 16)
                if addr_int == 0:
                    continue
                norm_addr = f"0x{addr_int:08X}"
                symbol_table[sym_name] = norm_addr

                kind = classify_section(sub_section) or classify_section(top_section)
                if kind == "func":
                    func_symbols[sym_name] = norm_addr
                elif kind == "var":
                    var_symbols[sym_name] = norm_addr

    if not var_symbols:
        var_symbols.update(symbol_table)
    if not func_symbols:
        func_symbols.update(symbol_table)

    return symbol_table, var_symbols, func_symbols


def parse_lst_file(filepath: str):
    """
    Parses a GNU objdump -S disassembly .lst file and returns:
      lst_table: {"filename.c:line": "0x80001234"}
    """
    lst_table = {}
    pending_line_key = None

    with open(filepath, "r", errors="ignore") as f:
        for line in f:
            stripped = line.strip()
            m_src = re.match(r"^(.+?\.[cChHsS](?:pp)?):(\d+)", stripped)
            if m_src:
                base_file = os.path.basename(m_src.group(1))
                line_num = m_src.group(2)
                pending_line_key = f"{base_file}:{line_num}"
                continue

            if pending_line_key:
                m_inst = re.match(r"^\s*([0-9a-fA-F]{4,16}):\s+[0-9a-fA-F]{4,8}\s+", line)
                if m_inst:
                    addr_int = int(m_inst.group(1), 16)
                    if pending_line_key not in lst_table:
                        lst_table[pending_line_key] = f"0x{addr_int:08X}"
                    pending_line_key = None

    return lst_table