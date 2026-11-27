# RiscVLiveDebugger.py
import json
import os
import re
import threading
import traceback
import customtkinter as ctk
from tkinter import filedialog

import matplotlib
matplotlib.use("TkAgg")

from ocd_backend import (
    OpenOCDSession,
    start_openocd_console,
    stop_openocd_console,
    parse_map_file,
    parse_lst_file,
)
from widgets_telemetry import (
    BaseWidget,
    VarSlider,
    EnumSelectorWidget,
    ReadMonitor,
    MemBlockPlotter,
    MemFileLoader,
    AliveLightWidget,
)
from widgets_breakpoints import (
    BreakOnFuncWidget,
    BreakOnLineWidget,
    BreakOnVarWidget,
)

from widgets_roi import RoiCropWidget

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class ModernOpenOCD_GUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("RISC-V Live Tuner & Telemetry Dashboard")
        self.geometry("1180x880")

        self.symbol_table = {}
        self.var_symbols = {}
        self.func_symbols = {}
        self.lst_table = {}

        self.jtag_lock = threading.Lock()
        self.target_halted_at_bp = False
        self.abort_transfers = False
        self.ocd_process = None

        self.ocd_exe_path = ctk.StringVar()
        self.ocd_cfg_path = ctk.StringVar()

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.build_ui()
        self.log_msg("Ready. Load a .map file (and optional .lst file for Break on Line).")

    def build_ui(self):
        ocd_frame = ctk.CTkFrame(self, fg_color="#1E1E1E", border_width=1, border_color="#555555")
        ocd_frame.pack(fill="x", padx=20, pady=(15, 5))

        ctk.CTkLabel(ocd_frame, text="OpenOCD Exe:").grid(row=0, column=0, padx=10, pady=5)
        ctk.CTkEntry(ocd_frame, textvariable=self.ocd_exe_path, width=240).grid(row=0, column=1, padx=5, pady=5)
        ctk.CTkButton(ocd_frame, text="Browse", width=60, command=self.browse_exe).grid(row=0, column=2, padx=5, pady=5)

        ctk.CTkLabel(ocd_frame, text="Main CFG:").grid(row=0, column=3, padx=10, pady=5)
        ctk.CTkEntry(ocd_frame, textvariable=self.ocd_cfg_path, width=240).grid(row=0, column=4, padx=5, pady=5)
        ctk.CTkButton(ocd_frame, text="Browse", width=60, command=self.browse_cfg).grid(row=0, column=5, padx=5, pady=5)

        self.btn_start_ocd = ctk.CTkButton(
            ocd_frame, text="Start OpenOCD", fg_color="#00695C", hover_color="#004D40", command=self.toggle_openocd
        )
        self.btn_start_ocd.grid(row=0, column=6, padx=15, pady=5)

        ctk.CTkButton(
            ocd_frame, text="Reset CPU", width=80, fg_color="#D84315", hover_color="#BF360C", command=self.reset_target
        ).grid(row=0, column=7, padx=5, pady=5)

        file_frame = ctk.CTkFrame(self, fg_color="transparent")
        file_frame.pack(fill="x", padx=20, pady=5)

        ctk.CTkButton(file_frame, text="Upload .map File", width=125, command=self.load_map_file).pack(side="left")
        self.map_label = ctk.CTkLabel(file_frame, text="No .map loaded.", text_color="gray")
        self.map_label.pack(side="left", padx=(8, 20))

        ctk.CTkButton(
            file_frame, text="Upload .lst File (Opt)", width=145, fg_color="#37474F", hover_color="#263238", command=self.load_lst_file
        ).pack(side="left")
        self.lst_label = ctk.CTkLabel(file_frame, text="Optional (for Break on Line)", text_color="gray")
        self.lst_label.pack(side="left", padx=8)

        ctk.CTkButton(file_frame, text="Save Layout", width=90, fg_color="#555555", command=self.save_layout).pack(side="right")
        ctk.CTkButton(file_frame, text="Load Layout", width=90, fg_color="#555555", command=self.load_layout).pack(side="right", padx=8)
        ctk.CTkButton(
            file_frame,
            text="Sync from Target",
            width=125,
            fg_color="#00838F",
            hover_color="#006064",
            command=self.sync_write_widgets,
        ).pack(side="right", padx=(8, 0))

        add_frame = ctk.CTkFrame(self, fg_color="transparent")
        add_frame.pack(fill="x", padx=20, pady=(2, 10))

        ctk.CTkButton(
            add_frame, text="+ Read Var", width=85, fg_color="#0277BD", hover_color="#01579B",
            command=lambda: ReadMonitor(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            add_frame, text="+ Write Slider", width=92, fg_color="#2E7D32", hover_color="#1B5E20",
            command=lambda: VarSlider(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            add_frame, text="+ Enum Switch", width=98, fg_color="#3F51B5", hover_color="#303F9F",
            command=lambda: EnumSelectorWidget(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            add_frame, text="+ ROI Cropper", width=98, fg_color="#00ACC1", hover_color="#00838F", text_color="black",
            command=lambda: RoiCropWidget(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            add_frame, text="+ Mem Plotter", width=92, fg_color="#8E24AA", hover_color="#6A1B9A",
            command=lambda: MemBlockPlotter(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            add_frame, text="+ Mem Loader", width=92, fg_color="#F57C00", hover_color="#E65100",
            command=lambda: MemFileLoader(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            add_frame, text="+ Alive Light", width=86, fg_color="#FBC02D", hover_color="#F9A825", text_color="black",
            command=lambda: AliveLightWidget(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            add_frame, text="+ Break on Func", width=105, fg_color="#00838F", hover_color="#006064",
            command=lambda: BreakOnFuncWidget(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            add_frame, text="+ Break on Line", width=105, fg_color="#00695C", hover_color="#004D40",
            command=lambda: BreakOnLineWidget(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            add_frame, text="+ Break on Var", width=102, fg_color="#D84315", hover_color="#BF360C",
            command=lambda: BreakOnVarWidget(self.scrollable_frame, self),
        ).pack(side="left")

        self.scrollable_frame = ctk.CTkScrollableFrame(self, corner_radius=10)
        self.scrollable_frame.pack(side="top", fill="both", expand=True, padx=20, pady=5)

        self.log_text = ctk.CTkTextbox(self, height=100, state="disabled", font=("Consolas", 12))
        self.log_text.pack(pady=(15, 5), padx=20, fill="x")

        footer_frame = ctk.CTkFrame(self, fg_color="transparent")
        footer_frame.pack(fill="x", padx=20, pady=(0, 10))

        author_lbl = ctk.CTkLabel(
            footer_frame,
            text="RISC-V Live Tuner & Telemetry Dashboard  |  Developed by Leonardo Di Carlo",
            font=("Consolas", 11),
            text_color="#777777",
            anchor="w",
        )
        author_lbl.pack(side="left")

    def disable_all_auto_poll(self):
        """Disables Auto Poll on all instantiated widgets immediately."""
        for widget in self.scrollable_frame.winfo_children():
            if hasattr(widget, "auto_poll") and widget.auto_poll.get():
                widget.auto_poll.set(False)
                if isinstance(widget, AliveLightWidget):
                    widget.toggle_poll()

    def stop_all_activity(self):
        """Flushes queued JTAG threads, stops all auto-polling, and disarms breakpoint timers."""
        self.abort_transfers = True
        self.target_halted_at_bp = False
        self.disable_all_auto_poll()
        for widget in self.scrollable_frame.winfo_children():
            if hasattr(widget, "is_armed") and widget.is_armed:
                widget.is_armed = False
                widget.is_halted_here = False
                if hasattr(widget, "arm_btn"):
                    widget.arm_btn.configure(text="Arm Breakpoint", fg_color="#00838F", hover_color="#006064")
                if hasattr(widget, "resume_btn"):
                    widget.resume_btn.configure(state="disabled")
                if hasattr(widget, "status_lbl"):
                    widget.status_lbl.configure(text="Status: Disarmed", text_color="gray")

    def refresh_other_widgets_halted(self):
        for widget in self.scrollable_frame.winfo_children():
            if isinstance(widget, (ReadMonitor, AliveLightWidget)):
                widget.execute_read(halted_only=True)
            elif isinstance(widget, MemBlockPlotter):
                widget.execute_dump(halted_only=True)

    def notify_symbol_files_updated(self):
        for widget in self.scrollable_frame.winfo_children():
            if hasattr(widget, "on_symbol_files_updated"):
                widget.on_symbol_files_updated()

    def browse_exe(self):
        fp = filedialog.askopenfilename(title="Select openocd.exe", filetypes=[("Executable", "*.exe"), ("All Files", "*.*")])
        if fp:
            self.ocd_exe_path.set(fp)

    def browse_cfg(self):
        fp = filedialog.askopenfilename(title="Select main OpenOCD .cfg", filetypes=[("Config Files", "*.cfg"), ("All Files", "*.*")])
        if fp:
            self.ocd_cfg_path.set(fp)

    def toggle_openocd(self):
        if self.ocd_process is None:
            exe = self.ocd_exe_path.get().strip()
            cfg = self.ocd_cfg_path.get().strip()
            if not exe or not cfg:
                self.log_msg("ERROR: Must select both OpenOCD exe and a .cfg file.")
                return
            try:
                self.abort_transfers = False
                self.ocd_process = start_openocd_console(exe, cfg)
                self.btn_start_ocd.configure(text="Stop OpenOCD", fg_color="#C62828", hover_color="#b71c1c")
                self.log_msg("OpenOCD started in colorized console window.")
            except Exception as e:
                self.log_msg(f"Failed to start OpenOCD: {e}")
        else:
            self.stop_all_activity()
            stop_openocd_console(self.ocd_process)
            self.ocd_process = None
            self.btn_start_ocd.configure(text="Start OpenOCD", fg_color="#00695C", hover_color="#004D40")
            self.log_msg("OpenOCD server stopped. All polling and queued reads flushed.")

    def reset_target(self):
        def _task():
            with self.jtag_lock:
                try:
                    with OpenOCDSession(timeout=2.0) as ocd:
                        ocd.send("rbp all; rwp all; reset run")
                    self.target_halted_at_bp = False
                    self.after(0, lambda: self.log_msg("Cleared triggers and sent 'reset run' to target."))
                except Exception as e:
                    self.after(0, lambda: self.log_msg(f"Reset failed (Is OpenOCD running?): {e}"))

        threading.Thread(target=_task, daemon=True).start()

    def on_closing(self):
        self.stop_all_activity()
        stop_openocd_console(self.ocd_process)
        self.ocd_process = None
        self.destroy()

    def load_map_file(self):
        filepath = filedialog.askopenfilename(title="Select .map File", filetypes=[("Map Files", "*.map"), ("All Files", "*.*")])
        if not filepath:
            return
        try:
            sym_all, sym_vars, sym_funcs = parse_map_file(filepath)
            self.symbol_table = sym_all
            self.var_symbols = sym_vars
            self.func_symbols = sym_funcs

            fname = os.path.basename(filepath)
            self.map_label.configure(
                text=f"{fname} ({len(self.var_symbols)} vars, {len(self.func_symbols)} funcs)",
                text_color="#4CAF50",
            )
            self.log_msg(
                f"Loaded .map: {len(self.var_symbols)} RAM variables and {len(self.func_symbols)} functions ready to scroll & select."
            )
            self.notify_symbol_files_updated()
        except Exception as e:
            self.log_msg(f"ERROR reading map file: {e}")

    def load_lst_file(self):
        filepath = filedialog.askopenfilename(title="Select .lst File", filetypes=[("Disassembly Listing", "*.lst *.txt"), ("All Files", "*.*")])
        if not filepath:
            return
        try:
            self.lst_table = parse_lst_file(filepath)
            fname = os.path.basename(filepath)
            self.lst_label.configure(text=f"{fname} ({len(self.lst_table)} lines)", text_color="#4CAF50")
            self.log_msg(f"Loaded optional .lst file: mapped {len(self.lst_table)} source lines to instruction addresses.")
            self.notify_symbol_files_updated()
        except Exception as e:
            self.log_msg(f"ERROR reading .lst file: {e}")

    def sync_write_widgets(self, target_widgets=None):
        """Reads live target RAM values once and updates write widgets (excluding MemFileLoader)."""
        self.abort_transfers = False

        if target_widgets is None:
            widgets = [
                w for w in self.scrollable_frame.winfo_children()
                if w.winfo_exists() and hasattr(w, "get_sync_requests")
            ]
        else:
            widgets = [w for w in target_widgets if w.winfo_exists() and hasattr(w, "get_sync_requests")]

        if not widgets:
            self.log_msg("Sync: No write-capable widgets on the workspace to sync.")
            return

        # Collect unique (addr, mem_cmd) pairs across all targeted write widgets
        unique_reqs = []
        seen = set()
        active_widgets = []
        for w in widgets:
            reqs = w.get_sync_requests()
            if reqs:
                active_widgets.append(w)
                for key in reqs:
                    if key not in seen:
                        seen.add(key)
                        unique_reqs.append(key)

        if not unique_reqs:
            return

        def _sync_task():
            raw_results = {}
            with self.jtag_lock:
                if self.abort_transfers:
                    return
                try:
                    with OpenOCDSession(timeout=2.5) as ocd:
                        state = ocd.send("[target current] curstate")
                        was_running = "running" in state
                        if was_running:
                            ocd.send("halt; wait_halt")
                        try:
                            for addr, mem_cmd in unique_reqs:
                                resp = ocd.send(f"{mem_cmd} {addr}")
                                m = re.search(r":\s*([0-9a-fA-F]+)", resp)
                                if m:
                                    raw_results[(addr, mem_cmd)] = int(m.group(1), 16)
                        finally:
                            if was_running and not self.target_halted_at_bp:
                                ocd.send("resume")
                except Exception as e:
                    self.after(0, lambda: self.log_msg(f"Sync ERROR (Is OpenOCD connected?): {e}"))
                    return

            def _apply_all():
                synced = 0
                for w in active_widgets:
                    if w.winfo_exists():
                        w.apply_sync_results(raw_results)
                        synced += 1
                self.log_msg(f"Synced {synced} write widget(s) from target RAM.")

            self.after(0, _apply_all)

        threading.Thread(target=_sync_task, daemon=True).start()

    def save_layout(self):
        filepath = filedialog.asksaveasfilename(
            title="Save Dashboard Layout",
            defaultextension=".json",
            filetypes=[("JSON Layouts", "*.json"), ("All Files", "*.*")],
        )
        if not filepath:
            return
        layout_data = []
        for w in self.scrollable_frame.winfo_children():
            if isinstance(w, BaseWidget) and hasattr(w, "get_state"):
                st = dict(w.get_state())
                st.update(w.get_geometry_state())
                layout_data.append(st)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(layout_data, f, indent=4)
            self.log_msg(f"Layout saved successfully ({len(layout_data)} widgets) -> {os.path.basename(filepath)}")
        except Exception as e:
            self.log_msg(f"ERROR saving layout: {e}")

    def load_layout(self):
        filepath = filedialog.askopenfilename(
            title="Load Dashboard Layout",
            filetypes=[("JSON Layouts", "*.json"), ("All Files", "*.*")],
        )
        if not filepath:
            return
        widget_factory = {
            "write": VarSlider,
            "enum_selector": EnumSelectorWidget,
            "roi_crop": RoiCropWidget,
            "read": ReadMonitor,
            "plotter": MemBlockPlotter,
            "loader": MemFileLoader,
            "alive_light": AliveLightWidget,
            "bp_func": BreakOnFuncWidget,
            "bp_line": BreakOnLineWidget,
            "bp_var": BreakOnVarWidget,
        }
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                layout_data = json.load(f)

            self.stop_all_activity()
            self.abort_transfers = False

            for widget in list(self.scrollable_frame.winfo_children()):
                widget.destroy()
            if hasattr(self.scrollable_frame, "_tile_spacer"):
                self.scrollable_frame._tile_spacer = None

            loaded_count = 0
            for state in layout_data:
                w_type = state.get("widget_type")
                cls = widget_factory.get(w_type)
                if cls:
                    new_w = cls(self.scrollable_frame, self)
                    new_w._adopt_children_into_viewport()
                    new_w.set_state(state)
                    # Restore exact (x, y, w, h) AFTER set_state so saved sizes are never overwritten
                    new_w.set_geometry_state(state)
                    loaded_count += 1
                else:
                    self.log_msg(f"Warning: Unknown widget_type '{w_type}' skipped.")

            self.log_msg(f"Layout loaded successfully ({loaded_count} widgets) <- {os.path.basename(filepath)}")
        except Exception as e:
            self.log_msg(f"ERROR loading layout: {e}\n{traceback.format_exc()}")

    def log_msg(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


if __name__ == "__main__":
    app = ModernOpenOCD_GUI()
    app.mainloop()