# widgets_breakpoints.py
import re
import struct
import threading
import customtkinter as ctk

from ocd_backend import OpenOCDSession
from widgets_telemetry import BaseWidget


class BaseBreakpointWidget(BaseWidget):
    # --- SHARED BASE CLASS FOR ALL 3 BREAKPOINT WIDGETS ---
    FORMATS = ["Hex", "uint32", "int32", "uint16", "int16", "uint8", "int8", "float", "bin"]

    def __init__(self, parent, app, border_color="#00ACC1", selector_title="Select Target"):
        super().__init__(parent, corner_radius=10, border_width=2, border_color=border_color)
        self.app = app
        self.selector_title = selector_title
        self.pack(fill="x", pady=10, padx=10)

        self.is_armed = False
        self.is_halted_here = False
        self.active_bp_addr = None
        self.keep_armed = ctk.BooleanVar(value=True)
        self.global_fmt = ctk.StringVar(value="Hex")
        self.filter_var = ctk.StringVar(value="")
        self.selector_filter_var = ctk.StringVar(value="")

        self.selector_buttons = {}
        self.var_rows = {}

        # 1. Top Control Row
        self.top_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.top_frame.pack(fill="x", padx=10, pady=(10, 4))
        self.build_specific_controls(self.top_frame)

        # 2. Status & Action Bar
        ctrl_frame = ctk.CTkFrame(self, fg_color="transparent")
        ctrl_frame.pack(fill="x", padx=15, pady=(2, 6))

        self.arm_btn = ctk.CTkButton(ctrl_frame, text="Arm Breakpoint", width=120, fg_color="#00838F", hover_color="#006064", command=self.toggle_arm)
        self.arm_btn.pack(side="left", padx=(0, 10))

        self.resume_btn = ctk.CTkButton(ctrl_frame, text="Resume Code", width=110, fg_color="#2E7D32", hover_color="#1B5E20", state="disabled", command=self.resume_target)
        self.resume_btn.pack(side="left", padx=(0, 10))

        ctk.CTkCheckBox(ctrl_frame, text="Keep Armed on Resume", variable=self.keep_armed, width=140).pack(side="left", padx=5)

        self.status_lbl = ctk.CTkLabel(ctrl_frame, text="Status: Disarmed", font=("Consolas", 13, "bold"), text_color="gray")
        self.status_lbl.pack(side="left", padx=15)

        # 3. Side-by-Side Split Container
        split_frame = ctk.CTkFrame(self, fg_color="transparent")
        split_frame.pack(fill="x", padx=12, pady=(2, 12))
        split_frame.grid_columnconfigure(0, weight=2)
        split_frame.grid_columnconfigure(1, weight=3)

        # --- LEFT PANE: SCROLLABLE TARGET SELECTOR ---
        left_pane = ctk.CTkFrame(split_frame, fg_color="#181818", corner_radius=6)
        left_pane.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        left_hdr = ctk.CTkFrame(left_pane, fg_color="transparent")
        left_hdr.pack(fill="x", padx=6, pady=4)
        self.selector_title_lbl = ctk.CTkLabel(left_hdr, text=self.selector_title, font=("Consolas", 12, "bold"), text_color="#4DD0E1")
        self.selector_title_lbl.pack(side="left", padx=4)

        sel_filter_entry = ctk.CTkEntry(left_hdr, textvariable=self.selector_filter_var, width=110, placeholder_text="Search list...")
        sel_filter_entry.pack(side="right", padx=4)
        sel_filter_entry.bind("<KeyRelease>", lambda e: self.apply_selector_filter())

        self.selector_scroll = ctk.CTkScrollableFrame(left_pane, height=175, corner_radius=5, fg_color="#1E1E1E")
        self.selector_scroll.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # --- RIGHT PANE: SCROLLABLE ALL-VARIABLES SNAPSHOT ---
        right_pane = ctk.CTkFrame(split_frame, fg_color="#181818", corner_radius=6)
        right_pane.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        right_hdr = ctk.CTkFrame(right_pane, fg_color="transparent")
        right_hdr.pack(fill="x", padx=6, pady=4)

        ctk.CTkLabel(right_hdr, text="Variables Snapshot on Hit:", font=("Consolas", 12, "bold")).pack(side="left", padx=4)
        filter_entry = ctk.CTkEntry(right_hdr, textvariable=self.filter_var, width=105, placeholder_text="Filter var...")
        filter_entry.pack(side="left", padx=6)
        filter_entry.bind("<KeyRelease>", lambda e: self.apply_filter())

        ctk.CTkComboBox(right_hdr, variable=self.global_fmt, values=self.FORMATS, width=85, command=self.apply_global_format).pack(side="right", padx=4)
        ctk.CTkLabel(right_hdr, text="Format All:").pack(side="right", padx=2)

        self.var_scroll = ctk.CTkScrollableFrame(right_pane, height=175, corner_radius=5, fg_color="#1E1E1E")
        self.var_scroll.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.populate_selector_rows()
        self.populate_var_rows()

    def build_specific_controls(self, parent):
        pass

    def get_selector_dict(self):
        return {}

    def on_selector_item_clicked(self, key, addr):
        pass

    def get_current_target_key(self):
        return ""

    def resolve_trigger_command(self):
        return None

    def on_symbol_files_updated(self):
        self.populate_selector_rows()
        self.populate_var_rows()

    def populate_selector_rows(self):
        for child in self.selector_scroll.winfo_children():
            child.destroy()
        self.selector_buttons.clear()

        items_dict = self.get_selector_dict()
        if not items_dict:
            ctk.CTkLabel(
                self.selector_scroll,
                text="Load .map (or .lst) file above\nto populate scrollable list.",
                text_color="gray",
            ).pack(pady=20)
            return

        current_sel = self.get_current_target_key()
        for key in sorted(items_dict.keys()):
            addr = items_dict[key]
            is_sel = key == current_sel
            btn = ctk.CTkButton(
                self.selector_scroll,
                text=f"{key:<28} {addr}",
                font=("Consolas", 11),
                anchor="w",
                height=24,
                fg_color="#0277BD" if is_sel else "transparent",
                hover_color="#263238",
                text_color="white" if is_sel else "#E0E0E0",
                command=lambda k=key, a=addr: self._handle_selector_click(k, a),
            )
            btn.pack(fill="x", pady=1, padx=2)
            self.selector_buttons[key] = {"btn": btn, "addr": addr}

        self.apply_selector_filter()

    def _handle_selector_click(self, key, addr):
        self.on_selector_item_clicked(key, addr)
        for k, info in self.selector_buttons.items():
            if k == key:
                info["btn"].configure(fg_color="#0277BD", text_color="white")
            else:
                info["btn"].configure(fg_color="transparent", text_color="#E0E0E0")

    def apply_selector_filter(self):
        query = self.selector_filter_var.get().strip().lower()
        for key, info in self.selector_buttons.items():
            if not query or query in key.lower() or query in info["addr"].lower():
                info["btn"].pack(fill="x", pady=1, padx=2)
            else:
                info["btn"].pack_forget()

    def populate_var_rows(self):
        for child in self.var_scroll.winfo_children():
            child.destroy()
        self.var_rows.clear()

        source_vars = self.app.var_symbols if self.app.var_symbols else self.app.symbol_table
        if not source_vars:
            ctk.CTkLabel(
                self.var_scroll,
                text="Load a .map file to populate\nthe variable snapshot list.",
                text_color="gray",
            ).pack(pady=20)
            return

        for var_name in sorted(source_vars.keys()):
            addr = source_vars[var_name]
            row = ctk.CTkFrame(self.var_scroll, fg_color="transparent")
            row.pack(fill="x", pady=1, padx=2)

            ctk.CTkLabel(row, text=var_name, font=("Consolas", 11), width=155, anchor="w").pack(side="left", padx=(2, 4))
            ctk.CTkLabel(row, text=addr, font=("Consolas", 10), text_color="#888888", width=85, anchor="w").pack(side="left", padx=2)

            val_lbl = ctk.CTkLabel(row, text="---", font=("Consolas", 12, "bold"), text_color="#00E676", width=130, anchor="w")
            val_lbl.pack(side="left", padx=6)

            fmt_var = ctk.StringVar(value=self.global_fmt.get())
            fmt_box = ctk.CTkComboBox(
                row,
                variable=fmt_var,
                values=self.FORMATS,
                width=82,
                height=22,
                command=lambda choice, vn=var_name: self.update_single_row_display(vn),
            )
            fmt_box.pack(side="right", padx=2)

            self.var_rows[var_name] = {
                "addr": addr,
                "raw": None,
                "fmt_var": fmt_var,
                "val_lbl": val_lbl,
                "frame": row,
            }

        self.apply_filter()

    def apply_filter(self):
        query = self.filter_var.get().strip().lower()
        for var_name, info in self.var_rows.items():
            if not query or query in var_name.lower():
                info["frame"].pack(fill="x", pady=1, padx=2)
            else:
                info["frame"].pack_forget()

    def apply_global_format(self, fmt_choice):
        for var_name, info in self.var_rows.items():
            info["fmt_var"].set(fmt_choice)
            self.update_single_row_display(var_name)

    @staticmethod
    def format_raw_value(raw_u32, fmt):
        if raw_u32 is None:
            return "---"
        raw_u32 &= 0xFFFFFFFF
        if fmt == "Hex":
            return f"0x{raw_u32:08X}"
        elif fmt == "uint32":
            return str(raw_u32)
        elif fmt == "int32":
            val = raw_u32 - (1 << 32) if (raw_u32 & 0x80000000) else raw_u32
            return str(val)
        elif fmt == "uint16":
            return str(raw_u32 & 0xFFFF)
        elif fmt == "int16":
            v16 = raw_u32 & 0xFFFF
            return str(v16 - (1 << 16) if (v16 & 0x8000) else v16)
        elif fmt == "uint8":
            return str(raw_u32 & 0xFF)
        elif fmt == "int8":
            v8 = raw_u32 & 0xFF
            return str(v8 - (1 << 8) if (v8 & 0x80) else v8)
        elif fmt == "float":
            try:
                f_val = struct.unpack("<f", struct.pack("<I", raw_u32))[0]
                return f"{f_val:.6g}"
            except Exception:
                return "NaN"
        elif fmt == "bin":
            return f"0b{raw_u32:032b}"
        return f"0x{raw_u32:08X}"

    def update_single_row_display(self, var_name):
        info = self.var_rows.get(var_name)
        if not info:
            return
        formatted = self.format_raw_value(info["raw"], info["fmt_var"].get())
        info["val_lbl"].configure(text=formatted)

    def toggle_arm(self):
        if not self.is_armed:
            trig = self.resolve_trigger_command()
            if not trig:
                return
            addr, set_cmd, clear_cmd, desc = trig
            self.active_bp_addr = addr
            self.set_cmd = set_cmd
            self.clear_cmd = clear_cmd
            self.bp_desc = desc

            def _arm_task():
                with self.app.jtag_lock:
                    try:
                        with OpenOCDSession(timeout=2.0) as ocd:
                            state = ocd.send("[[target current] curstate]")
                            was_running = "running" in state
                            if was_running:
                                ocd.send("halt; wait_halt")
                            ocd.send(self.clear_cmd)
                            ocd.send(self.set_cmd)
                            if was_running:
                                ocd.send("resume")

                        self.is_armed = True
                        self.after(0, lambda: self.arm_btn.configure(text="Disarm", fg_color="#C62828", hover_color="#b71c1c"))
                        self.after(0, lambda: self.status_lbl.configure(text=f"Armed ({self.bp_desc})", text_color="#FFD740"))
                        self.after(0, lambda: self.app.log_msg(f"Armed {self.bp_desc} at {self.active_bp_addr}"))
                        self.after(200, self.poll_for_halt)
                    except Exception as e:
                        self.after(0, lambda: self.app.log_msg(f"Breakpoint Arm ERROR: {e}"))

            threading.Thread(target=_arm_task, daemon=True).start()
        else:
            self.disarm_breakpoint()

    def disarm_breakpoint(self, resume_after=False):
        clear_cmd = getattr(self, "clear_cmd", None)
        self.is_armed = False
        self.is_halted_here = False

        def _disarm_task():
            with self.app.jtag_lock:
                try:
                    with OpenOCDSession(timeout=2.0) as ocd:
                        state = ocd.send("[[target current] curstate]")
                        was_running = "running" in state
                        if was_running:
                            ocd.send("halt; wait_halt")
                        if clear_cmd:
                            ocd.send(clear_cmd)
                        if was_running or resume_after:
                            ocd.send("resume")
                            self.app.target_halted_at_bp = False
                except Exception:
                    pass

            if self.winfo_exists():
                self.after(0, lambda: self.arm_btn.configure(text="Arm Breakpoint", fg_color="#00838F", hover_color="#006064"))
                self.after(0, lambda: self.resume_btn.configure(state="disabled"))
                self.after(0, lambda: self.status_lbl.configure(text="Status: Disarmed", text_color="gray"))
                self.after(0, lambda: self.app.log_msg("Breakpoint disarmed."))

        threading.Thread(target=_disarm_task, daemon=True).start()

    def poll_for_halt(self):
        if not self.winfo_exists() or not self.is_armed or self.is_halted_here:
            return

        def _check_task():
            hit_detected = False
            with self.app.jtag_lock:
                try:
                    with OpenOCDSession(timeout=1.5) as ocd:
                        state = ocd.send("[[target current] curstate]")
                        if "halted" in state:
                            hit_detected = True
                            self.is_halted_here = True
                            self.app.target_halted_at_bp = True
                except Exception:
                    pass

            if hit_detected:
                self.after(0, self.on_breakpoint_hit)
            elif self.is_armed and self.winfo_exists():
                self.after(200, self.poll_for_halt)

        threading.Thread(target=_check_task, daemon=True).start()

    def on_breakpoint_hit(self):
        if not self.winfo_exists():
            return
        self.app.disable_all_auto_poll()
        self.resume_btn.configure(state="normal")
        self.status_lbl.configure(text="HALTED! Reading variables...", text_color="#FF5252")

        def _snapshot_task():
            pc_str = "unknown"
            results = {}
            with self.app.jtag_lock:
                try:
                    with OpenOCDSession(timeout=4.0) as ocd:
                        pc_out = ocd.send("reg pc")
                        m_pc = re.search(r"0x[0-9a-fA-F]+", pc_out)
                        if m_pc:
                            pc_str = m_pc.group(0)

                        for var_name, info in self.var_rows.items():
                            out = ocd.send(f"mdw {info['addr']}")
                            m_val = re.search(r":\s*([0-9a-fA-F]+)", out)
                            if m_val:
                                results[var_name] = int(m_val.group(1), 16)
                except Exception as e:
                    self.after(0, lambda: self.app.log_msg(f"Snapshot Read ERROR: {e}"))

            def _apply_ui():
                if not self.winfo_exists():
                    return
                for var_name, raw_val in results.items():
                    if var_name in self.var_rows:
                        self.var_rows[var_name]["raw"] = raw_val
                        self.update_single_row_display(var_name)
                self.status_lbl.configure(text=f"HALTED @ PC={pc_str}", text_color="#FF5252")
                self.app.log_msg(f"Breakpoint hit at PC={pc_str}. Auto-poll disabled; updated {len(results)} variables.")
                self.app.refresh_other_widgets_halted()

            self.after(0, _apply_ui)

        threading.Thread(target=_snapshot_task, daemon=True).start()

    def resume_target(self):
        if not self.is_halted_here:
            return
        keep = self.keep_armed.get()

        def _resume_task():
            with self.app.jtag_lock:
                try:
                    with OpenOCDSession(timeout=2.0) as ocd:
                        ocd.send(self.clear_cmd)
                        ocd.send("step")
                        if keep:
                            ocd.send(self.set_cmd)
                        ocd.send("resume")

                    self.app.target_halted_at_bp = False
                    self.is_halted_here = False

                    def _update_ui():
                        if not self.winfo_exists():
                            return
                        self.resume_btn.configure(state="disabled")
                        if keep:
                            self.status_lbl.configure(text=f"Armed ({self.bp_desc})", text_color="#FFD740")
                            self.app.log_msg("Target resumed (breakpoint re-armed).")
                            self.after(200, self.poll_for_halt)
                        else:
                            self.is_armed = False
                            self.arm_btn.configure(text="Arm Breakpoint", fg_color="#00838F", hover_color="#006064")
                            self.status_lbl.configure(text="Status: Disarmed", text_color="gray")
                            self.app.log_msg("Target resumed (breakpoint disarmed).")

                    self.after(0, _update_ui)
                except Exception as e:
                    self.after(0, lambda: self.app.log_msg(f"Resume ERROR: {e}"))

        threading.Thread(target=_resume_task, daemon=True).start()

    def destroy(self):
        if self.is_armed:
            self.disarm_breakpoint()
        super().destroy()


class BreakOnFuncWidget(BaseBreakpointWidget):
    # --- 1. BREAK ON FUNCTION WIDGET ---
    def __init__(self, parent, app):
        self.func_name = ctk.StringVar(value="main")
        super().__init__(parent, app, border_color="#00ACC1", selector_title="Select Function (.map):")

    def build_specific_controls(self, parent):
        ctk.CTkLabel(parent, text="Break on Func | Selected Function / Hex:").pack(side="left", padx=(5, 5))
        ctk.CTkEntry(parent, textvariable=self.func_name, width=220).pack(side="left", padx=5)
        self.addr_preview = ctk.CTkLabel(parent, text="", font=("Consolas", 11), text_color="#4DD0E1")
        self.addr_preview.pack(side="left", padx=10)
        ctk.CTkButton(parent, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).pack(side="right", padx=5)

    def get_selector_dict(self):
        return self.app.func_symbols

    def get_current_target_key(self):
        return self.func_name.get().strip()

    def on_selector_item_clicked(self, key, addr):
        self.func_name.set(key)
        self.addr_preview.configure(text=f"-> {addr}")

    def resolve_trigger_command(self):
        target = self.func_name.get().strip()
        addr = self.app.func_symbols.get(target) or self.get_resolved_addr(target)
        if not addr:
            self.app.log_msg(f"Break on Func Error: '{target}' not found in .map file or valid hex.")
            return None
        self.addr_preview.configure(text=f"-> {addr}")
        return (addr, f"bp {addr} 2 hw", f"rbp {addr}", f"Func: {target} @ {addr}")

    def get_state(self):
        return {"widget_type": "bp_func", "func_name": self.func_name.get(), "global_fmt": self.global_fmt.get()}

    def set_state(self, state):
        self.func_name.set(state.get("func_name", "main"))
        fmt = state.get("global_fmt", "Hex")
        self.global_fmt.set(fmt)
        self.apply_global_format(fmt)
        self.populate_selector_rows()


class BreakOnLineWidget(BaseBreakpointWidget):
    # --- 2. BREAK ON SOURCE LINE WIDGET ---
    def __init__(self, parent, app):
        self.line_spec = ctk.StringVar(value="main.c:100")
        super().__init__(parent, app, border_color="#26A69A", selector_title="Select Source Line (.lst):")

    def build_specific_controls(self, parent):
        ctk.CTkLabel(parent, text="Break on Line | Selected file.c:line / Hex:").pack(side="left", padx=(5, 5))
        ctk.CTkEntry(parent, textvariable=self.line_spec, width=220).pack(side="left", padx=5)
        self.resolved_lbl = ctk.CTkLabel(parent, text="(Load optional .lst or enter 0x addr)", text_color="gray", font=("Consolas", 11))
        self.resolved_lbl.pack(side="left", padx=10)
        ctk.CTkButton(parent, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).pack(side="right", padx=5)

    def get_selector_dict(self):
        return self.app.lst_table

    def get_current_target_key(self):
        return self.line_spec.get().strip()

    def on_selector_item_clicked(self, key, addr):
        self.line_spec.set(key)
        self.resolved_lbl.configure(text=f"-> {addr}", text_color="#00E676")

    def resolve_trigger_command(self):
        spec = self.line_spec.get().strip()
        addr = self.app.lst_table.get(spec)
        if not addr:
            for k, v in self.app.lst_table.items():
                if k.lower() == spec.lower():
                    addr = v
                    break
            if not addr:
                addr = self.get_resolved_addr(spec)

        if not addr:
            self.app.log_msg(f"Break on Line Error: Cannot resolve '{spec}'. Load optional .lst file or enter hex address.")
            return None
        self.resolved_lbl.configure(text=f"-> {addr}", text_color="#00E676")
        return (addr, f"bp {addr} 2 hw", f"rbp {addr}", f"Line: {spec} @ {addr}")

    def get_state(self):
        return {"widget_type": "bp_line", "line_spec": self.line_spec.get(), "global_fmt": self.global_fmt.get()}

    def set_state(self, state):
        self.line_spec.set(state.get("line_spec", "main.c:100"))
        fmt = state.get("global_fmt", "Hex")
        self.global_fmt.set(fmt)
        self.apply_global_format(fmt)
        self.populate_selector_rows()


class BreakOnVarWidget(BaseBreakpointWidget):
    # --- 3. BREAK ON VARIABLE WATCHPOINT WIDGET ---
    def __init__(self, parent, app):
        self.var_target = ctk.StringVar(value="motor_speed")
        self.wp_mode = ctk.StringVar(value="w")
        self.wp_len = ctk.StringVar(value="4")
        super().__init__(parent, app, border_color="#FF7043", selector_title="Select Watch Variable (.map):")

    def build_specific_controls(self, parent):
        ctk.CTkLabel(parent, text="Break on Var | Selected Var / Hex:").pack(side="left", padx=(5, 5))
        ctk.CTkEntry(parent, textvariable=self.var_target, width=180).pack(side="left", padx=5)

        ctk.CTkLabel(parent, text="Access:").pack(side="left", padx=(10, 2))
        ctk.CTkComboBox(parent, variable=self.wp_mode, values=["w", "r", "a"], width=60).pack(side="left", padx=2)

        ctk.CTkLabel(parent, text="Bytes:").pack(side="left", padx=(10, 2))
        ctk.CTkComboBox(parent, variable=self.wp_len, values=["1", "2", "4"], width=60).pack(side="left", padx=2)

        self.addr_preview = ctk.CTkLabel(parent, text="", font=("Consolas", 11), text_color="#FFAB91")
        self.addr_preview.pack(side="left", padx=8)

        ctk.CTkButton(parent, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).pack(side="right", padx=5)

    def get_selector_dict(self):
        return self.app.var_symbols if self.app.var_symbols else self.app.symbol_table

    def get_current_target_key(self):
        return self.var_target.get().strip()

    def on_selector_item_clicked(self, key, addr):
        self.var_target.set(key)
        self.addr_preview.configure(text=f"-> {addr}")

    def resolve_trigger_command(self):
        target = self.var_target.get().strip()
        addr = self.get_resolved_addr(target)
        if not addr:
            self.app.log_msg(f"Break on Var Error: '{target}' is not a valid .map symbol or hex address.")
            return None
        self.addr_preview.configure(text=f"-> {addr}")
        mode = self.wp_mode.get().strip()
        length = self.wp_len.get().strip()
        return (addr, f"wp {addr} {length} {mode}", f"rwp {addr}", f"Var WP({mode}): {target} @ {addr}")

    def get_state(self):
        return {
            "widget_type": "bp_var",
            "var_target": self.var_target.get(),
            "wp_mode": self.wp_mode.get(),
            "wp_len": self.wp_len.get(),
            "global_fmt": self.global_fmt.get(),
        }

    def set_state(self, state):
        self.var_target.set(state.get("var_target", "motor_speed"))
        self.wp_mode.set(state.get("wp_mode", "w"))
        self.wp_len.set(state.get("wp_len", "4"))
        fmt = state.get("global_fmt", "Hex")
        self.global_fmt.set(fmt)
        self.apply_global_format(fmt)
        self.populate_selector_rows()