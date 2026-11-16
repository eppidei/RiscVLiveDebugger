# RiscVLiveDebugger.py
import json
import os
import threading
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
    VarSlider,
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

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class ModernOpenOCD_GUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PolarFire Live Tuner & Telemetry Dashboard")
        self.geometry("1150x860")

        self.symbol_table = {}
        self.var_symbols = {}
        self.func_symbols = {}
        self.lst_table = {}

        self.jtag_lock = threading.Lock()
        self.target_halted_at_bp = False
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
        ctk.CTkButton(file_frame, text="Load Layout", width=90, fg_color="#555555", command=self.load_layout).pack(side="right", padx=10)

        add_frame = ctk.CTkFrame(self, fg_color="transparent")
        add_frame.pack(fill="x", padx=20, pady=(2, 10))

        ctk.CTkButton(
            add_frame, text="+ Read Var", width=95, fg_color="#0277BD", hover_color="#01579B",
            command=lambda: ReadMonitor(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            add_frame, text="+ Write Slider", width=100, fg_color="#2E7D32", hover_color="#1B5E20",
            command=lambda: VarSlider(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            add_frame, text="+ Mem Plotter", width=100, fg_color="#8E24AA", hover_color="#6A1B9A",
            command=lambda: MemBlockPlotter(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            add_frame, text="+ Mem Loader", width=100, fg_color="#F57C00", hover_color="#E65100",
            command=lambda: MemFileLoader(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            add_frame, text="+ Alive Light", width=95, fg_color="#FBC02D", hover_color="#F9A825", text_color="black",
            command=lambda: AliveLightWidget(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 16))

        ctk.CTkButton(
            add_frame, text="+ Break on Func", width=115, fg_color="#00838F", hover_color="#006064",
            command=lambda: BreakOnFuncWidget(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            add_frame, text="+ Break on Line", width=115, fg_color="#00695C", hover_color="#004D40",
            command=lambda: BreakOnLineWidget(self.scrollable_frame, self),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            add_frame, text="+ Break on Var", width=115, fg_color="#D84315", hover_color="#BF360C",
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
            text="RISCV Live Tuner & Telemetry Dashboard  |  Developed by Leonardo Di Carlo", 
            font=("Consolas", 11), 
            text_color="#777777",
            anchor="w"
        )
        author_lbl.pack(side="left")

    def disable_all_auto_poll(self):
        for widget in self.scrollable_frame.winfo_children():
            if hasattr(widget, "auto_poll") and widget.auto_poll.get():
                widget.auto_poll.set(False)
                if isinstance(widget, AliveLightWidget):
                    widget.toggle_poll()

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
                self.ocd_process = start_openocd_console(exe, cfg)
                self.btn_start_ocd.configure(text="Stop OpenOCD", fg_color="#C62828", hover_color="#b71c1c")
                self.log_msg("OpenOCD started in colorized console window.")
            except Exception as e:
                self.log_msg(f"Failed to start OpenOCD: {e}")
        else:
            stop_openocd_console(self.ocd_process)
            self.ocd_process = None
            self.btn_start_ocd.configure(text="Start OpenOCD", fg_color="#00695C", hover_color="#004D40")
            self.log_msg("OpenOCD server stopped.")

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
        stop_openocd_console(self.ocd_process)
        self.ocd_process = None
        self.destroy()

    def load_map_file(self):
        filepath = filedialog.askopenfilename(filetypes=[("Map Files", "*.map"), ("All Files", "*.*")])
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
        filepath = filedialog.askopenfilename(filetypes=[("Disassembly Listing", "*.lst *.txt"), ("All Files", "*.*")])
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

    def save_layout(self):
        filepath = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Layouts", "*.json")])
        if not filepath:
            return
        layout_data = [
            w.get_state() for w in self.scrollable_frame.winfo_children() if hasattr(w, "get_state")
        ]
        try:
            with open(filepath, "w") as f:
                json.dump(layout_data, f, indent=4)
            self.log_msg(f"Layout saved successfully: {len(layout_data)} widgets.")
        except Exception as e:
            self.log_msg(f"ERROR saving layout: {e}")

    def load_layout(self):
        filepath = filedialog.askopenfilename(filetypes=[("JSON Layouts", "*.json"), ("All Files", "*.*")])
        if not filepath:
            return
        widget_Factory = {
            "write": VarSlider,
            "read": ReadMonitor,
            "plotter": MemBlockPlotter,
            "loader": MemFileLoader,
            "alive_light": AliveLightWidget,
            "bp_func": BreakOnFuncWidget,
            "bp_line": BreakOnLineWidget,
            "bp_var": BreakOnVarWidget,
        }
        try:
            with open(filepath, "r") as f:
                layout_data = json.load(f)
            for widget in self.scrollable_frame.winfo_children():
                widget.destroy()
            for state in layout_data:
                cls = widget_Factory.get(state.get("widget_type"))
                if cls:
                    new_w = cls(self.scrollable_frame, self)
                    new_w.set_state(state)
            self.log_msg(f"Layout loaded successfully: {len(layout_data)} widgets.")
        except Exception as e:
            self.log_msg(f"ERROR loading layout: {e}")

    def log_msg(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


if __name__ == "__main__":
    app = ModernOpenOCD_GUI()
    app.mainloop()