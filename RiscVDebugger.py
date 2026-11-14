import customtkinter as ctk
from tkinter import filedialog
import socket
import threading
import re
import json
import os
import struct
import subprocess
from collections import deque

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class BaseWidget(ctk.CTkFrame):
    def get_resolved_addr(self, addr_str):
        addr_str = addr_str.strip()
        if addr_str in self.app.symbol_table:
            return self.app.symbol_table[addr_str]
        if addr_str in self.app.func_symbols:
            return self.app.func_symbols[addr_str]
        if addr_str in self.app.var_symbols:
            return self.app.var_symbols[addr_str]
        try:
            int(addr_str, 16)
            return addr_str if addr_str.startswith("0x") else "0x" + addr_str
        except ValueError:
            return None


class VarSlider(BaseWidget):
    # --- WRITE SLIDER WIDGET ---
    def __init__(self, parent, app):
        super().__init__(parent, corner_radius=10, border_width=2, border_color="#2E7D32")
        self.app = app
        self.pack(fill="x", pady=10, padx=10)
        
        self.var_name = ctk.StringVar(value="motor_speed")
        self.var_type = ctk.StringVar(value="uint32")
        self.min_val = ctk.StringVar(value="0")
        self.max_val = ctk.StringVar(value="255")
        self.slider_timer = None
        
        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(config_frame, text="Write | Addr/Name:").grid(row=0, column=0, padx=5)
        ctk.CTkEntry(config_frame, textvariable=self.var_name, width=140).grid(row=0, column=1, padx=5)
        
        ctk.CTkLabel(config_frame, text="Type:").grid(row=0, column=2, padx=5)
        ctk.CTkComboBox(config_frame, variable=self.var_type, 
                        values=["uint8", "uint16", "uint32", "int8", "int16", "int32"], width=85).grid(row=0, column=3, padx=5)
        
        ctk.CTkLabel(config_frame, text="Min:").grid(row=0, column=4, padx=5)
        ctk.CTkEntry(config_frame, textvariable=self.min_val, width=60).grid(row=0, column=5, padx=5)
        
        ctk.CTkLabel(config_frame, text="Max:").grid(row=0, column=6, padx=5)
        ctk.CTkEntry(config_frame, textvariable=self.max_val, width=60).grid(row=0, column=7, padx=5)
        
        ctk.CTkButton(config_frame, text="Apply", width=60, command=self.update_config).grid(row=0, column=8, padx=10)
        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=9, padx=5)

        self.current_val_label = ctk.CTkLabel(self, text="Target Value: 0", font=("Consolas", 14, "bold"), text_color="#4CAF50")
        self.current_val_label.pack(pady=(5, 0))

        self.slider = ctk.CTkSlider(self, from_=0, to=255)
        self.slider.pack(fill="x", padx=15, pady=(5, 15))
        self.slider.configure(command=self.on_slider_change)
        self.slider.set(0)

    def update_config(self):
        try:
            min_v = int(self.min_val.get())
            max_v = int(self.max_val.get())
            self.slider.configure(from_=min_v, to=max_v)
            self.current_val_label.configure(text=f"Target Value: {int(self.slider.get())}")
        except ValueError:
            self.app.log_msg("ERROR: Min and Max must be valid integers.")

    def on_slider_change(self, val):
        if not self.winfo_exists(): return
        int_val = int(val)
        self.current_val_label.configure(text=f"Target Value: {int_val}")
        
        if self.slider_timer:
            self.after_cancel(self.slider_timer)
        self.slider_timer = self.after(200, lambda: self.execute_write(int_val))

    def execute_write(self, value):
        if not self.winfo_exists(): return
        name = self.var_name.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.app.log_msg(f"Wait: '{name}' is not a valid map symbol or hex address!")
            return
            
        v_type = self.var_type.get()
        if value < 0:
            bits = 8 if "8" in v_type else (16 if "16" in v_type else 32)
            value = (1 << bits) + value
            
        cmd_map = {"8": "mwb", "16": "mwh", "32": "mww"}
        mem_cmd = cmd_map.get(v_type.replace("uint", "").replace("int", ""), "mww")
        hex_val = hex(value)
        
        def _write_task():
            with self.app.jtag_lock:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(2.0)
                        s.connect(("localhost", 6666))
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        state = send_and_wait("[[target current] curstate]")
                        was_running = ("running" in state)
                        if was_running:
                            send_and_wait("halt; wait_halt")
                        send_and_wait(f"{mem_cmd} {addr} {hex_val}")
                        if was_running and not self.app.target_halted_at_bp:
                            send_and_wait("resume")
                        self.after(0, lambda: self.app.log_msg(f"Wrote {value} ({hex_val}) to {name}"))
                except Exception as e:
                    self.after(0, lambda: self.app.log_msg(f"Write ERROR: {str(e)}"))

        threading.Thread(target=_write_task, daemon=True).start()

    def get_state(self):
        return {"widget_type": "write", "var_name": self.var_name.get(), "var_type": self.var_type.get(), "min_val": self.min_val.get(), "max_val": self.max_val.get()}
    def set_state(self, state):
        self.var_name.set(state.get("var_name", "motor_speed")); self.var_type.set(state.get("var_type", "uint32"))
        self.min_val.set(state.get("min_val", "0")); self.max_val.set(state.get("max_val", "255")); self.update_config()


class ReadMonitor(BaseWidget):
    # --- SINGLE VARIABLE READ, PLOT, & BITFIELD WIDGET ---
    def __init__(self, parent, app):
        super().__init__(parent, corner_radius=10, border_width=2, border_color="#0277BD")
        self.app = app
        self.pack(fill="x", pady=10, padx=10)
        
        self.var_name = ctk.StringVar(value="status_flags")
        self.var_type = ctk.StringVar(value="uint32")
        self.frac_bits = ctk.StringVar(value="0")  
        self.auto_poll = ctk.BooleanVar(value=False)
        self.poll_interval = ctk.StringVar(value="500") 
        self.show_bitfields = ctk.BooleanVar(value=False)
        
        self.show_plot = ctk.BooleanVar(value=False)
        self.plot_buffer_size = ctk.StringVar(value="100")
        self.auto_scale = ctk.BooleanVar(value=True)
        self.y_min = ctk.StringVar(value="-10")
        self.y_max = ctk.StringVar(value="100")
        
        self.y_data = deque(maxlen=100)
        
        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(config_frame, text="Read | Addr/Name:").grid(row=0, column=0, padx=(5, 2))
        ctk.CTkEntry(config_frame, textvariable=self.var_name, width=130).grid(row=0, column=1, padx=2)
        
        ctk.CTkLabel(config_frame, text="Type:").grid(row=0, column=2, padx=(10, 2))
        ctk.CTkComboBox(config_frame, variable=self.var_type, 
                        values=["uint8", "uint16", "uint32", "int8", "int16", "int32"], 
                        width=85, command=lambda x: self.execute_read()).grid(row=0, column=3, padx=2)
        
        ctk.CTkLabel(config_frame, text="Frac Bits:").grid(row=0, column=4, padx=(10, 2))
        ctk.CTkEntry(config_frame, textvariable=self.frac_bits, width=35).grid(row=0, column=5, padx=2)
        
        ctk.CTkCheckBox(config_frame, text="Bitfields", variable=self.show_bitfields, 
                        command=self.toggle_bitfields).grid(row=0, column=6, padx=(15, 5))
        
        ctk.CTkCheckBox(config_frame, text="Plot", variable=self.show_plot, 
                        command=self.toggle_plot).grid(row=0, column=7, padx=5)
        
        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", 
                      command=self.destroy).grid(row=0, column=8, padx=(10, 5))

        display_frame = ctk.CTkFrame(self, fg_color="transparent")
        display_frame.pack(fill="x", padx=15, pady=(5, 10))

        self.value_label = ctk.CTkLabel(display_frame, text="0", font=("Consolas", 20, "bold"), width=120, anchor="w")
        self.value_label.pack(side="left", padx=(0, 15))

        self.progress = ctk.CTkProgressBar(display_frame)
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.progress.set(0)

        ctk.CTkButton(display_frame, text="Read Now", width=80, command=self.execute_read).pack(side="right")
        ctk.CTkSwitch(display_frame, text="Auto", variable=self.auto_poll, command=self.toggle_poll).pack(side="right", padx=(10, 5))
        ctk.CTkLabel(display_frame, text="ms").pack(side="right", padx=(0, 5))
        ctk.CTkEntry(display_frame, textvariable=self.poll_interval, width=50).pack(side="right")

        # --- BITFIELD UI ---
        self.bf_frame = ctk.CTkFrame(self, fg_color="#1E1E1E", corner_radius=5)
        self.bitfield_rows = []
        for i in range(4):
            row_frame = ctk.CTkFrame(self.bf_frame, fg_color="transparent")
            row_frame.pack(fill="x", pady=2, padx=10)
            
            name_var = ctk.StringVar()
            range_var = ctk.StringVar()
            
            ctk.CTkLabel(row_frame, text="Field:").pack(side="left")
            ctk.CTkEntry(row_frame, textvariable=name_var, width=120).pack(side="left", padx=(5, 15))
            
            ctk.CTkLabel(row_frame, text="Bits:").pack(side="left")
            ctk.CTkEntry(row_frame, textvariable=range_var, width=80).pack(side="left", padx=5)
            
            val_lbl = ctk.CTkLabel(row_frame, text="-", font=("Consolas", 14, "bold"), text_color="#00FF00", width=120, anchor="w")
            val_lbl.pack(side="left", padx=15)
            
            self.bitfield_rows.append({"name": name_var, "range": range_var, "label": val_lbl})

        # --- PLOT UI ---
        self.plot_frame = ctk.CTkFrame(self, fg_color="#242424", corner_radius=5)
        
        plot_ctrl_frame = ctk.CTkFrame(self.plot_frame, fg_color="transparent")
        plot_ctrl_frame.pack(fill="x", padx=10, pady=(5, 0))
        
        ctk.CTkLabel(plot_ctrl_frame, text="Buffer:").pack(side="left")
        ctk.CTkEntry(plot_ctrl_frame, textvariable=self.plot_buffer_size, width=50).pack(side="left", padx=5)
        
        ctk.CTkCheckBox(plot_ctrl_frame, text="Auto-Y", variable=self.auto_scale, width=60, 
                        command=self.update_plot_visuals).pack(side="left", padx=(15, 5))
        
        ctk.CTkLabel(plot_ctrl_frame, text="Y-Min:").pack(side="left", padx=(5, 2))
        ctk.CTkEntry(plot_ctrl_frame, textvariable=self.y_min, width=50).pack(side="left", padx=2)
        
        ctk.CTkLabel(plot_ctrl_frame, text="Y-Max:").pack(side="left", padx=(10, 2))
        ctk.CTkEntry(plot_ctrl_frame, textvariable=self.y_max, width=50).pack(side="left", padx=2)
        
        ctk.CTkButton(plot_ctrl_frame, text="Clear", width=60, fg_color="#555555", 
                      command=self.clear_plot).pack(side="right")

        self.fig = Figure(figsize=(8, 2.2), dpi=100)
        self.fig.patch.set_facecolor('#242424')
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.9, bottom=0.2)
        
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor('#1E1E1E')
        self.ax.tick_params(colors='white', labelsize=8)
        for spine in self.ax.spines.values(): spine.set_color('#555555')
        
        self.line, = self.ax.plot([], [], color='#00a8ff', linewidth=1.5)
        
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def toggle_bitfields(self):
        if self.show_bitfields.get():
            self.bf_frame.pack(fill="x", padx=15, pady=(0, 10))
            self.execute_read()
        else:
            self.bf_frame.pack_forget()
            
    def toggle_plot(self):
        if self.show_plot.get():
            self.plot_frame.pack(fill="x", padx=15, pady=(0, 15))
            self.update_plot_visuals()
        else:
            self.plot_frame.pack_forget()

    def clear_plot(self):
        self.y_data.clear()
        self.update_plot_visuals()

    def toggle_poll(self):
        if self.auto_poll.get():
            self.execute_read()

    def execute_read(self, halted_only=False):
        if not self.winfo_exists(): return
        
        name = self.var_name.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.value_label.configure(text="ERR")
            self.auto_poll.set(False)
            self.app.log_msg(f"Wait: '{name}' is not a valid map symbol or hex address!")
            return
            
        try:
            interval = int(self.poll_interval.get())
            if interval < 50: interval, self.poll_interval.set("50")
        except ValueError:
            interval = 500; self.poll_interval.set("500")

        v_type = self.var_type.get()
        cmd_map = {"8": "mdb", "16": "mdh", "32": "mdw"}
        mem_cmd = cmd_map.get(v_type.replace("uint", "").replace("int", ""), "mdw")
        
        def _read_task():
            with self.app.jtag_lock:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(2.0)
                        s.connect(("localhost", 6666))
                        
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        if halted_only or self.app.target_halted_at_bp:
                            read_output = send_and_wait(f"{mem_cmd} {addr}")
                        else:
                            state = send_and_wait("[[target current] curstate]")
                            if "halted" in state:
                                self.after(0, self.app.disable_all_auto_poll)
                                read_output = send_and_wait(f"{mem_cmd} {addr}")
                            else:
                                send_and_wait("halt; wait_halt")
                                read_output = send_and_wait(f"{mem_cmd} {addr}")
                                send_and_wait("resume")
                        
                        self.after(0, lambda: self.process_read_result(read_output))
                except Exception:
                    pass

        threading.Thread(target=_read_task, daemon=True).start()

        if self.auto_poll.get() and not halted_only:
            self.after(interval, self.execute_read)

    def process_read_result(self, response):
        if not self.winfo_exists(): return
        
        match = re.search(r':\s*([0-9a-fA-F]+)', response)
        if match:
            try:
                raw_val = int(match.group(1), 16)
                
                v_type = self.var_type.get()
                is_signed = v_type.startswith("int")
                bits = 8 if "8" in v_type else (16 if "16" in v_type else 32)
                
                val = raw_val
                if is_signed and (raw_val & (1 << (bits - 1))):
                    val = raw_val - (1 << bits)
                    
                try: f_bits = int(self.frac_bits.get())
                except ValueError: f_bits = 0
                    
                numeric_val = val / (1 << f_bits) if f_bits > 0 else val
                
                if f_bits > 0:
                    real_str = f"{numeric_val:.6f}"
                    if '.' in real_str: real_str = real_str.rstrip('0').rstrip('.')
                    if not real_str: real_str = "0"
                    self.value_label.configure(text=real_str)
                else:
                    self.value_label.configure(text=str(val))
                
                min_v = -(1 << (bits - 1)) if is_signed else 0
                max_v = (1 << (bits - 1)) - 1 if is_signed else (1 << bits) - 1
                ratio = (val - min_v) / (max_v - min_v) if max_v > min_v else 0
                self.progress.set(max(0.0, min(ratio, 1.0)))
                
                if self.show_bitfields.get():
                    for bf in self.bitfield_rows:
                        rng_str = bf["range"].get().strip()
                        if rng_str:
                            try:
                                m = re.match(r'^(\d+)(?:[:-](\d+))?$', rng_str)
                                if m:
                                    high = int(m.group(1))
                                    low = int(m.group(2)) if m.group(2) else high
                                    if low > high: high, low = low, high
                                        
                                    width = high - low + 1
                                    mask = ((1 << width) - 1) << low
                                    bf_val = (raw_val & mask) >> low
                                    
                                    if width == 1: bf["label"].configure(text=f"{bf_val}")
                                    else: bf["label"].configure(text=f"{bf_val} (0x{bf_val:X})")
                                else:
                                    bf["label"].configure(text="SYNTAX ERR")
                            except Exception:
                                bf["label"].configure(text="ERR")
                        else:
                            bf["label"].configure(text="-")
                
                try: 
                    buf_sz = int(self.plot_buffer_size.get())
                    if buf_sz < 10: buf_sz = 10
                except ValueError: 
                    buf_sz = 100
                    
                if self.y_data.maxlen != buf_sz:
                    self.y_data = deque(self.y_data, maxlen=buf_sz)
                    
                self.y_data.append(numeric_val)
                
                if self.show_plot.get():
                    self.update_plot_visuals()

            except ValueError:
                pass

    def update_plot_visuals(self):
        self.line.set_ydata(self.y_data)
        self.line.set_xdata(range(len(self.y_data)))
        
        buf_sz = self.y_data.maxlen if self.y_data.maxlen else 100
        self.ax.set_xlim(0, max(1, buf_sz - 1))
        
        if self.auto_scale.get():
            self.ax.relim()
            self.ax.autoscale_view(scalex=False, scaley=True)
        else:
            try:
                ymin = float(self.y_min.get())
                ymax = float(self.y_max.get())
                if ymin < ymax:
                    self.ax.set_ylim(ymin, ymax)
                else:
                    self.ax.set_ylim(ymax - 1, ymax + 1) 
            except ValueError:
                pass 
                
        self.canvas.draw_idle()

    def get_state(self):
        bfs = [{"name": bf["name"].get(), "range": bf["range"].get()} for bf in self.bitfield_rows]
        return {
            "widget_type": "read",
            "var_name": self.var_name.get(),
            "var_type": self.var_type.get(),
            "frac_bits": self.frac_bits.get(),
            "poll_interval": self.poll_interval.get(),
            "show_bitfields": self.show_bitfields.get(),
            "show_plot": self.show_plot.get(),
            "plot_buffer_size": self.plot_buffer_size.get(),
            "auto_scale": self.auto_scale.get(),
            "y_min": self.y_min.get(),
            "y_max": self.y_max.get(),
            "bitfields": bfs
        }

    def set_state(self, state):
        self.var_name.set(state.get("var_name", "status_flags"))
        self.var_type.set(state.get("var_type", "uint32"))
        self.frac_bits.set(state.get("frac_bits", "0"))
        self.poll_interval.set(state.get("poll_interval", "500"))
        
        self.show_bitfields.set(state.get("show_bitfields", False))
        self.show_plot.set(state.get("show_plot", False))
        self.plot_buffer_size.set(state.get("plot_buffer_size", "100"))
        
        self.auto_scale.set(state.get("auto_scale", True))
        self.y_min.set(state.get("y_min", "-10"))
        self.y_max.set(state.get("y_max", "100"))
        
        saved_bfs = state.get("bitfields", [])
        for i, bf_state in enumerate(saved_bfs):
            if i < len(self.bitfield_rows):
                self.bitfield_rows[i]["name"].set(bf_state.get("name", ""))
                self.bitfield_rows[i]["range"].set(bf_state.get("range", ""))
                
        if self.show_bitfields.get(): self.toggle_bitfields()
        if self.show_plot.get(): self.toggle_plot()
        if state.get("auto_poll", False):
            self.auto_poll.set(True)
            self.toggle_poll()


class MemBlockPlotter(BaseWidget):
    # --- MEMORY BLOCK DUMP & HISTOGRAM WIDGET ---
    def __init__(self, parent, app):
        super().__init__(parent, corner_radius=10, border_width=2, border_color="#8E24AA")
        self.app = app
        self.pack(fill="x", pady=10, padx=10)
        
        self.base_addr = ctk.StringVar(value="0x60000000")
        self.word_count = ctk.StringVar(value="64")
        self.word_size = ctk.StringVar(value="32")
        self.plot_type = ctk.StringVar(value="Bar") 
        
        self.auto_poll = ctk.BooleanVar(value=False)
        self.poll_interval = ctk.StringVar(value="1000") 
        
        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(config_frame, text="Block Dump | Addr:").grid(row=0, column=0, padx=(5,2))
        ctk.CTkEntry(config_frame, textvariable=self.base_addr, width=120).grid(row=0, column=1, padx=2)
        
        ctk.CTkLabel(config_frame, text="Size:").grid(row=0, column=2, padx=(10,2))
        ctk.CTkComboBox(config_frame, variable=self.word_size, values=["8", "16", "32"], width=65).grid(row=0, column=3, padx=2)
        
        ctk.CTkLabel(config_frame, text="Count:").grid(row=0, column=4, padx=(10,2))
        ctk.CTkEntry(config_frame, textvariable=self.word_count, width=50).grid(row=0, column=5, padx=2)
        
        ctk.CTkLabel(config_frame, text="Style:").grid(row=0, column=6, padx=(10,2))
        ctk.CTkComboBox(config_frame, variable=self.plot_type, values=["Bar", "Line"], width=75, command=lambda x: self.force_redraw()).grid(row=0, column=7, padx=2)
        
        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=8, padx=(15, 5))

        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.pack(fill="x", padx=15, pady=(0, 5))
        
        ctk.CTkButton(action_frame, text="Dump & Plot", width=100, command=self.execute_dump).pack(side="left")
        
        ctk.CTkEntry(action_frame, textvariable=self.poll_interval, width=50).pack(side="right")
        ctk.CTkLabel(action_frame, text="ms").pack(side="right", padx=(0, 5))
        ctk.CTkSwitch(action_frame, text="Auto", variable=self.auto_poll, command=self.toggle_poll).pack(side="right", padx=(10, 5))

        self.fig = Figure(figsize=(8, 3.5), dpi=100)
        self.fig.patch.set_facecolor('#242424')
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.9, bottom=0.15)
        self.ax = self.fig.add_subplot(111)
        self.setup_axes()
        
        self.last_values = []
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(5, 10))

    def setup_axes(self):
        self.ax.set_facecolor('#1E1E1E')
        self.ax.tick_params(colors='white', labelsize=8)
        for spine in self.ax.spines.values(): spine.set_color('#555555')

    def toggle_poll(self):
        if self.auto_poll.get(): self.execute_dump()

    def force_redraw(self):
        if self.last_values:
            self.draw_graph(self.last_values)

    def execute_dump(self, halted_only=False):
        if not self.winfo_exists(): return
        
        name = self.base_addr.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.auto_poll.set(False)
            self.app.log_msg("Plotter Error: Invalid address or map symbol.")
            return
            
        try: count = int(self.word_count.get())
        except ValueError: count = 64
        
        try: interval = max(100, int(self.poll_interval.get()))
        except ValueError: interval = 1000; self.poll_interval.set("1000")
        
        ws = self.word_size.get()
        cmd = "mdb" if ws == "8" else ("mdh" if ws == "16" else "mdw")
        
        def _dump_task():
            with self.app.jtag_lock:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(3.0) 
                        s.connect(("localhost", 6666))
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        if halted_only or self.app.target_halted_at_bp:
                            raw_data = send_and_wait(f"{cmd} {addr} {count}")
                        else:
                            state = send_and_wait("[[target current] curstate]")
                            if "halted" in state:
                                self.after(0, self.app.disable_all_auto_poll)
                                raw_data = send_and_wait(f"{cmd} {addr} {count}")
                            else:
                                send_and_wait("halt; wait_halt")
                                raw_data = send_and_wait(f"{cmd} {addr} {count}")
                                send_and_wait("resume")
                        
                        self.after(0, lambda: self.process_dump(raw_data))
                except Exception:
                    pass 

        threading.Thread(target=_dump_task, daemon=True).start()
        
        if self.auto_poll.get() and not halted_only:
            self.after(interval, self.execute_dump)

    def process_dump(self, raw_text):
        if not self.winfo_exists(): return
        values = []
        for line in raw_text.split('\n'):
            if ':' in line:
                parts = line.split(':')[1].strip().split()
                for p in parts:
                    try: values.append(int(p, 16))
                    except ValueError: pass
                    
        if not values:
            if not self.auto_poll.get():
                self.app.log_msg("Plotter Error: No valid hex data found in OpenOCD response.")
            return
            
        self.last_values = values
        self.draw_graph(values)
        
        if not self.auto_poll.get():
            self.app.log_msg(f"Plotted {len(values)} words from {self.base_addr.get()}.")

    def draw_graph(self, values):
        self.ax.clear()
        self.setup_axes()
        
        if self.plot_type.get() == "Bar":
            self.ax.bar(range(len(values)), values, color='#E040FB', width=0.8)
        else:
            self.ax.plot(range(len(values)), values, color='#E040FB', linewidth=1.5, marker='o', markersize=3)
            
        self.ax.set_xlim(-0.5, max(1, len(values) - 0.5))
        
        if values:
            min_v, max_v = min(values), max(values)
            margin = (max_v - min_v) * 0.1 if max_v != min_v else max(1, max_v * 0.1)
            self.ax.set_ylim(min_v - margin, max_v + margin)
            
        self.canvas.draw_idle()

    def get_state(self):
        return {
            "widget_type": "plotter", "base_addr": self.base_addr.get(), 
            "word_count": self.word_count.get(), "word_size": self.word_size.get(),
            "plot_type": self.plot_type.get(), "poll_interval": self.poll_interval.get()
        }
        
    def set_state(self, state):
        self.base_addr.set(state.get("base_addr", "0x60000000"))
        self.word_count.set(state.get("word_count", "64"))
        self.word_size.set(state.get("word_size", "32"))
        self.plot_type.set(state.get("plot_type", "Bar"))
        self.poll_interval.set(state.get("poll_interval", "1000"))
        if state.get("auto_poll", False):
            self.auto_poll.set(True)
            self.toggle_poll()


class MemFileLoader(BaseWidget):
    # --- MEMORY FILE LOADER WIDGET ---
    def __init__(self, parent, app):
        super().__init__(parent, corner_radius=10, border_width=2, border_color="#F57C00")
        self.app = app
        self.pack(fill="x", pady=10, padx=10)
        
        self.base_addr = ctk.StringVar(value="0x60000000")
        self.word_size = ctk.StringVar(value="32")
        self.filepath = None
        self.file_data = []
        
        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(config_frame, text="File Loader | Addr/Name:").grid(row=0, column=0, padx=5)
        ctk.CTkEntry(config_frame, textvariable=self.base_addr, width=130).grid(row=0, column=1, padx=5)
        
        ctk.CTkLabel(config_frame, text="Size:").grid(row=0, column=2, padx=5)
        ctk.CTkComboBox(config_frame, variable=self.word_size, values=["8", "16", "32"], width=70).grid(row=0, column=3, padx=5)
        
        ctk.CTkButton(config_frame, text="Select HEX File", width=120, command=self.load_file).grid(row=0, column=4, padx=15)
        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=5, padx=5)

        self.status_label = ctk.CTkLabel(self, text="No file selected.", text_color="gray", font=("Consolas", 12))
        self.status_label.pack(pady=5)
        
        self.upload_btn = ctk.CTkButton(self, text="Upload to Target", fg_color="#F57C00", hover_color="#E65100", command=self.execute_upload, state="disabled")
        self.upload_btn.pack(pady=(0, 15))

    def load_file(self):
        fp = filedialog.askopenfilename(filetypes=[("Text/Hex Files", "*.txt *.hex *.dat"), ("All Files", "*.*")])
        if not fp: return
        self.filepath = fp
        self.file_data.clear()
        
        try:
            with open(fp, 'r') as f: content = f.read()
            tokens = re.findall(r'[0-9a-fA-F]+', content)
            self.file_data = [int(t, 16) for t in tokens]
            
            if self.file_data:
                self.status_label.configure(text=f"Loaded {len(self.file_data)} words ready for upload.", text_color="#4CAF50")
                self.upload_btn.configure(state="normal")
            else:
                self.status_label.configure(text="No hex values found in file.", text_color="#C62828")
                self.upload_btn.configure(state="disabled")
        except Exception as e:
            self.status_label.configure(text=f"Error reading file: {e}", text_color="#C62828")

    def execute_upload(self):
        name = self.base_addr.get()
        base_addr_int = self.get_resolved_addr(name)
        if not base_addr_int:
            self.app.log_msg("Loader Error: Invalid address or map symbol.")
            return
            
        base_addr_int = int(base_addr_int, 16)
        ws = self.word_size.get()
        cmd = "mwb" if ws == "8" else ("mwh" if ws == "16" else "mww")
        byte_step = 1 if ws == "8" else (2 if ws == "16" else 4)
        
        self.status_label.configure(text="Uploading...", text_color="#FFA000")
        self.upload_btn.configure(state="disabled")

        def _upload_task():
            with self.app.jtag_lock:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(5.0)
                        s.connect(("localhost", 6666))
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        state = send_and_wait("[[target current] curstate]")
                        was_running = ("running" in state)
                        if was_running:
                            send_and_wait("halt; wait_halt")
                        
                        batch_size = 30
                        for i in range(0, len(self.file_data), batch_size):
                            batch = self.file_data[i:i+batch_size]
                            cmd_string = ""
                            for j, val in enumerate(batch):
                                addr = hex(base_addr_int + (i + j) * byte_step)
                                cmd_string += f"{cmd} {addr} {hex(val)}; "
                            send_and_wait(cmd_string)
                            
                        if was_running and not self.app.target_halted_at_bp:
                            send_and_wait("resume")
                        
                        self.after(0, lambda: self.status_label.configure(text=f"Success: Uploaded {len(self.file_data)} words.", text_color="#4CAF50"))
                        self.after(0, lambda: self.upload_btn.configure(state="normal"))
                        self.after(0, lambda: self.app.log_msg(f"Finished writing {len(self.file_data)} words to {name}"))
                        
                except Exception as e:
                    self.after(0, lambda: self.status_label.configure(text="Upload Failed!", text_color="#C62828"))
                    self.after(0, lambda: self.upload_btn.configure(state="normal"))
                    self.after(0, lambda: self.app.log_msg(f"Loader ERROR: {str(e)}"))

        threading.Thread(target=_upload_task, daemon=True).start()

    def get_state(self):
        return {"widget_type": "loader", "base_addr": self.base_addr.get(), "word_size": self.word_size.get()}
    def set_state(self, state):
        self.base_addr.set(state.get("base_addr", "0x60000000")); self.word_size.set(state.get("word_size", "32"))


class AliveLightWidget(BaseWidget):
    # --- ALIVE LIGHT INDICATOR WIDGET ---
    def __init__(self, parent, app):
        super().__init__(parent, corner_radius=10, border_width=2, border_color="#FBC02D")
        self.app = app
        self.pack(fill="x", pady=10, padx=10)
        
        self.var_name = ctk.StringVar(value="system_alive")
        self.var_type = ctk.StringVar(value="uint8")
        self.auto_poll = ctk.BooleanVar(value=False)
        self.poll_interval = ctk.StringVar(value="500") 
        
        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(config_frame, text="Alive Light | Addr:").grid(row=0, column=0, padx=(5, 2))
        ctk.CTkEntry(config_frame, textvariable=self.var_name, width=130).grid(row=0, column=1, padx=2)
        
        ctk.CTkLabel(config_frame, text="Type:").grid(row=0, column=2, padx=(10, 2))
        ctk.CTkComboBox(config_frame, variable=self.var_type, values=["uint8", "uint16", "uint32"], width=85).grid(row=0, column=3, padx=2)
        
        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=4, padx=(15, 5))

        display_frame = ctk.CTkFrame(self, fg_color="transparent")
        display_frame.pack(fill="x", padx=15, pady=(5, 10))

        self.light = ctk.CTkLabel(display_frame, text="", width=30, height=30, corner_radius=15, fg_color="#424242")
        self.light.pack(side="left", padx=(10, 15))

        self.status_lbl = ctk.CTkLabel(display_frame, text="Stopped", text_color="gray", font=("Consolas", 14, "bold"))
        self.status_lbl.pack(side="left")

        ctk.CTkButton(display_frame, text="Read Now", width=80, command=self.execute_read).pack(side="right")
        ctk.CTkSwitch(display_frame, text="Auto Poll", variable=self.auto_poll, command=self.toggle_poll).pack(side="right", padx=(10, 5))
        ctk.CTkLabel(display_frame, text="ms").pack(side="right", padx=(0, 5))
        ctk.CTkEntry(display_frame, textvariable=self.poll_interval, width=50).pack(side="right")

    def toggle_poll(self):
        if self.auto_poll.get():
            self.execute_read()
        else:
            self.light.configure(fg_color="#424242")
            self.status_lbl.configure(text="Stopped", text_color="gray")

    def execute_read(self, halted_only=False):
        if not self.winfo_exists(): return
        
        name = self.var_name.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.set_error("Invalid Address")
            self.auto_poll.set(False)
            return
            
        try: interval = max(50, int(self.poll_interval.get()))
        except ValueError: interval = 500; self.poll_interval.set("500")

        v_type = self.var_type.get()
        cmd = "mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw")
        
        def _read_task():
            with self.app.jtag_lock:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(2.0)
                        s.connect(("localhost", 6666))
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        if halted_only or self.app.target_halted_at_bp:
                            output = send_and_wait(f"{cmd} {addr}")
                        else:
                            state = send_and_wait("[[target current] curstate]")
                            if "halted" in state:
                                self.after(0, self.app.disable_all_auto_poll)
                                output = send_and_wait(f"{cmd} {addr}")
                            else:
                                send_and_wait("halt; wait_halt")
                                output = send_and_wait(f"{cmd} {addr}")
                                send_and_wait("resume")
                        
                        self.after(0, lambda: self.process_read(output))
                except Exception:
                    self.after(0, lambda: self.set_error("Comm Error"))

        threading.Thread(target=_read_task, daemon=True).start()
        
        if self.auto_poll.get() and not halted_only:
            self.after(interval, self.execute_read)

    def process_read(self, output):
        if not self.winfo_exists(): return
        match = re.search(r':\s*([0-9a-fA-F]+)', output)
        if match:
            val = int(match.group(1), 16)
            if val > 0:
                self.light.configure(fg_color="#00E676") 
                self.status_lbl.configure(text=f"TRUE ({val})", text_color="#00E676")
            else:
                self.light.configure(fg_color="#424242") 
                self.status_lbl.configure(text="FALSE (0)", text_color="gray")
        else:
            self.set_error("Parse Error")

    def set_error(self, msg):
        if self.winfo_exists():
            self.light.configure(fg_color="#D50000") 
            self.status_lbl.configure(text=msg, text_color="#D50000")

    def get_state(self):
        return {"widget_type": "alive_light", "var_name": self.var_name.get(), "var_type": self.var_type.get(), "poll_interval": self.poll_interval.get()}

    def set_state(self, state):
        self.var_name.set(state.get("var_name", "system_alive"))
        self.var_type.set(state.get("var_type", "uint8"))
        self.poll_interval.set(state.get("poll_interval", "500"))
        if state.get("auto_poll", False):
            self.auto_poll.set(True)
            self.toggle_poll()

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

        self.selector_buttons = {}  # {key: {"btn": CTkButton, "addr": str}}
        self.var_rows = {}          # {var_name: {"addr": str, "raw": int|None, "fmt_var": StringVar, "val_lbl": CTkLabel, "frame": CTkFrame}}

        # 1. Top Control Row (Target Entry + Extra Options + Arm/Resume)
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

        # 3. Side-by-Side Split Container:
        #    Left  = Scrollable Selector List (Functions / Lines / Watch Vars)
        #    Right = Scrollable Variables Snapshot List (Updated on Breakpoint Hit)
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
        """Override in subclass: returns {symbol_or_line: addr_hex} to populate the left scrollable selector."""
        return {}

    def on_selector_item_clicked(self, key, addr):
        """Override in subclass: sets the active target variable when user clicks an item in the left list."""
        pass

    def get_current_target_key(self):
        return ""

    def resolve_trigger_command(self):
        """Override in subclass: returns (addr_hex, set_cmd, clear_cmd, description) or None."""
        return None

    def on_symbol_files_updated(self):
        """Called by main GUI when .map or .lst files are loaded."""
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
                text_color="gray"
            ).pack(pady=20)
            return

        current_sel = self.get_current_target_key()
        for key in sorted(items_dict.keys()):
            addr = items_dict[key]
            is_sel = (key == current_sel)
            btn = ctk.CTkButton(
                self.selector_scroll,
                text=f"{key:<28} {addr}",
                font=("Consolas", 11),
                anchor="w",
                height=24,
                fg_color="#0277BD" if is_sel else "transparent",
                hover_color="#263238",
                text_color="white" if is_sel else "#E0E0E0",
                command=lambda k=key, a=addr: self._handle_selector_click(k, a)
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
                text_color="gray"
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
                command=lambda choice, vn=var_name: self.update_single_row_display(vn)
            )
            fmt_box.pack(side="right", padx=2)

            self.var_rows[var_name] = {
                "addr": addr,
                "raw": None,
                "fmt_var": fmt_var,
                "val_lbl": val_lbl,
                "frame": row
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
                f_val = struct.unpack('<f', struct.pack('<I', raw_u32))[0]
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
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            s.settimeout(2.0)
                            s.connect(("localhost", 6666))
                            def send_and_wait(command):
                                s.sendall(command.encode('utf-8') + b'\x1a')
                                resp = b""
                                while b'\x1a' not in resp:
                                    chunk = s.recv(1024)
                                    if not chunk: break
                                    resp += chunk
                                return resp.decode('utf-8').strip('\x1a\r\n')

                            state = send_and_wait("[[target current] curstate]")
                            was_running = ("running" in state)
                            if was_running:
                                send_and_wait("halt; wait_halt")
                            send_and_wait(self.clear_cmd)
                            send_and_wait(self.set_cmd)
                            if was_running:
                                send_and_wait("resume")

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
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(2.0)
                        s.connect(("localhost", 6666))
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        state = send_and_wait("[[target current] curstate]")
                        was_running = ("running" in state)
                        if was_running:
                            send_and_wait("halt; wait_halt")
                        if clear_cmd:
                            send_and_wait(clear_cmd)
                        if was_running or resume_after:
                            send_and_wait("resume")
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
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(1.5)
                        s.connect(("localhost", 6666))
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        state = send_and_wait("[[target current] curstate]")
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
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(4.0)
                        s.connect(("localhost", 6666))
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        pc_out = send_and_wait("reg pc")
                        m_pc = re.search(r'0x[0-9a-fA-F]+', pc_out)
                        if m_pc:
                            pc_str = m_pc.group(0)

                        for var_name, info in self.var_rows.items():
                            out = send_and_wait(f"mdw {info['addr']}")
                            m_val = re.search(r':\s*([0-9a-fA-F]+)', out)
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
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(2.0)
                        s.connect(("localhost", 6666))
                        def send_and_wait(command):
                            s.sendall(command.encode('utf-8') + b'\x1a')
                            resp = b""
                            while b'\x1a' not in resp:
                                chunk = s.recv(1024)
                                if not chunk: break
                                resp += chunk
                            return resp.decode('utf-8').strip('\x1a\r\n')

                        send_and_wait(self.clear_cmd)
                        send_and_wait("step")
                        if keep:
                            send_and_wait(self.set_cmd)
                        send_and_wait("resume")

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
    # --- 1. BREAK ON FUNCTION WIDGET (SCROLL & CLICK FUNCTION SELECTOR) ---
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
    # --- 2. BREAK ON SOURCE LINE WIDGET (SCROLL & CLICK .LST LINE SELECTOR) ---
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
    # --- 3. BREAK ON VARIABLE WATCHPOINT WIDGET (SCROLL & CLICK VAR SELECTOR) ---
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
            "global_fmt": self.global_fmt.get()
        }

    def set_state(self, state):
        self.var_target.set(state.get("var_target", "motor_speed"))
        self.wp_mode.set(state.get("wp_mode", "w"))
        self.wp_len.set(state.get("wp_len", "4"))
        fmt = state.get("global_fmt", "Hex")
        self.global_fmt.set(fmt)
        self.apply_global_format(fmt)
        self.populate_selector_rows()


class ModernOpenOCD_GUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PolarFire Live Tuner & Telemetry Dashboard")
        self.geometry("1150x860")

        self.symbol_table = {}   # All symbols: {name: addr_hex}
        self.var_symbols = {}    # RAM / Data symbols: {name: addr_hex}
        self.func_symbols = {}   # Code / .text functions: {name: addr_hex}
        self.lst_table = {}      # Disassembly line map: {"file.c:line": addr_hex}

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

        self.btn_start_ocd = ctk.CTkButton(ocd_frame, text="Start OpenOCD", fg_color="#00695C", hover_color="#004D40", command=self.toggle_openocd)
        self.btn_start_ocd.grid(row=0, column=6, padx=15, pady=5)
        
        ctk.CTkButton(ocd_frame, text="Reset CPU", width=80, fg_color="#D84315", hover_color="#BF360C", command=self.reset_target).grid(row=0, column=7, padx=5, pady=5)

        file_frame = ctk.CTkFrame(self, fg_color="transparent")
        file_frame.pack(fill="x", padx=20, pady=5)
        
        ctk.CTkButton(file_frame, text="Upload .map File", width=125, command=self.load_map_file).pack(side="left")
        self.map_label = ctk.CTkLabel(file_frame, text="No .map loaded.", text_color="gray")
        self.map_label.pack(side="left", padx=(8, 20))

        ctk.CTkButton(file_frame, text="Upload .lst File (Opt)", width=145, fg_color="#37474F", hover_color="#263238", command=self.load_lst_file).pack(side="left")
        self.lst_label = ctk.CTkLabel(file_frame, text="Optional (for Break on Line)", text_color="gray")
        self.lst_label.pack(side="left", padx=8)
        
        ctk.CTkButton(file_frame, text="Save Layout", width=90, fg_color="#555555", command=self.save_layout).pack(side="right")
        ctk.CTkButton(file_frame, text="Load Layout", width=90, fg_color="#555555", command=self.load_layout).pack(side="right", padx=10)
        
        add_frame = ctk.CTkFrame(self, fg_color="transparent")
        add_frame.pack(fill="x", padx=20, pady=(2, 10))

        ctk.CTkButton(add_frame, text="+ Read Var", width=95, fg_color="#0277BD", hover_color="#01579B", 
                      command=lambda: ReadMonitor(self.scrollable_frame, self)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(add_frame, text="+ Write Slider", width=100, fg_color="#2E7D32", hover_color="#1B5E20", 
                      command=lambda: VarSlider(self.scrollable_frame, self)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(add_frame, text="+ Mem Plotter", width=100, fg_color="#8E24AA", hover_color="#6A1B9A", 
                      command=lambda: MemBlockPlotter(self.scrollable_frame, self)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(add_frame, text="+ Mem Loader", width=100, fg_color="#F57C00", hover_color="#E65100", 
                      command=lambda: MemFileLoader(self.scrollable_frame, self)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(add_frame, text="+ Alive Light", width=95, fg_color="#FBC02D", hover_color="#F9A825", text_color="black", 
                      command=lambda: AliveLightWidget(self.scrollable_frame, self)).pack(side="left", padx=(0, 16))

        ctk.CTkButton(add_frame, text="+ Break on Func", width=115, fg_color="#00838F", hover_color="#006064",
                      command=lambda: BreakOnFuncWidget(self.scrollable_frame, self)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(add_frame, text="+ Break on Line", width=115, fg_color="#00695C", hover_color="#004D40",
                      command=lambda: BreakOnLineWidget(self.scrollable_frame, self)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(add_frame, text="+ Break on Var", width=115, fg_color="#D84315", hover_color="#BF360C",
                      command=lambda: BreakOnVarWidget(self.scrollable_frame, self)).pack(side="left")

        self.scrollable_frame = ctk.CTkScrollableFrame(self, corner_radius=10)
        self.scrollable_frame.pack(side="top", fill="both", expand=True, padx=20, pady=5)

        self.log_text = ctk.CTkTextbox(self, height=100, state="disabled", font=("Consolas", 12))
        self.log_text.pack(pady=15, padx=20, fill="x")

    def disable_all_auto_poll(self):
        """Disables Auto Poll on all instantiated widgets when a breakpoint is hit."""
        for widget in self.scrollable_frame.winfo_children():
            if hasattr(widget, "auto_poll") and widget.auto_poll.get():
                widget.auto_poll.set(False)
                if isinstance(widget, AliveLightWidget):
                    widget.toggle_poll()

    def refresh_other_widgets_halted(self):
        """Updates any active ReadMonitor, AliveLight, or MemBlockPlotter widgets once while halted."""
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
        if fp: self.ocd_exe_path.set(fp)

    def browse_cfg(self):
        fp = filedialog.askopenfilename(title="Select main OpenOCD .cfg", filetypes=[("Config Files", "*.cfg"), ("All Files", "*.*")])
        if fp: self.ocd_cfg_path.set(fp)

    def toggle_openocd(self):
        if self.ocd_process is None:
            exe = self.ocd_exe_path.get().strip()
            cfg = self.ocd_cfg_path.get().strip()
            
            if not exe or not cfg:
                self.log_msg("ERROR: Must select both OpenOCD exe and a .cfg file.")
                return
                
            cfg_dir = os.path.dirname(cfg)
            cfg_file = os.path.basename(cfg)
            
            try:
                if os.name == 'nt':
                    ps_script = (
                        f"$host.UI.RawUI.WindowTitle = 'OpenOCD Live Console'; "
                        f"& '{exe}' -s '{cfg_dir}' -f '{cfg_file}' 2>&1 | ForEach-Object {{ "
                        f"if ($_ -match 'Error') {{ Write-Host $_ -ForegroundColor Red }} "
                        f"elseif ($_ -match 'Warn') {{ Write-Host $_ -ForegroundColor Yellow }} "
                        f"elseif ($_ -match 'Info') {{ Write-Host $_ -ForegroundColor Cyan }} "
                        f"else {{ Write-Host $_ -ForegroundColor Gray }} }}"
                    )
                    cmd = ["powershell", "-NoLogo", "-NoExit", "-Command", ps_script]
                    self.ocd_process = subprocess.Popen(cmd, cwd=cfg_dir, creationflags=subprocess.CREATE_NEW_CONSOLE)
                else:
                    cmd = [exe, "-s", cfg_dir, "-f", cfg_file]
                    self.ocd_process = subprocess.Popen(cmd, cwd=cfg_dir)

                self.btn_start_ocd.configure(text="Stop OpenOCD", fg_color="#C62828", hover_color="#b71c1c")
                self.log_msg("OpenOCD started in colorized console window.")
            except Exception as e:
                self.log_msg(f"Failed to start OpenOCD: {e}")
        else:
            self.stop_openocd_process()
            self.btn_start_ocd.configure(text="Start OpenOCD", fg_color="#00695C", hover_color="#004D40")
            self.log_msg("OpenOCD server stopped.")

    def stop_openocd_process(self):
        if self.ocd_process:
            if os.name == 'nt':
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.ocd_process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            else:
                self.ocd_process.terminate()
                self.ocd_process.wait()
            self.ocd_process = None

    def reset_target(self):
        def _task():
            with self.jtag_lock:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(2.0)
                        s.connect(("localhost", 6666))
                        s.sendall("rbp all; rwp all; reset run\x1a".encode('utf-8'))
                        self.target_halted_at_bp = False
                        self.after(0, lambda: self.log_msg("Cleared triggers and sent 'reset run' to target."))
                except Exception as e:
                    self.after(0, lambda: self.log_msg(f"Reset failed (Is OpenOCD running?): {e}"))
        threading.Thread(target=_task, daemon=True).start()

    def on_closing(self):
        self.stop_openocd_process()
        self.destroy()

    def load_map_file(self):
        filepath = filedialog.askopenfilename(filetypes=[("Map Files", "*.map"), ("All Files", "*.*")])
        if not filepath: return
        self.symbol_table.clear()
        self.var_symbols.clear()
        self.func_symbols.clear()

        top_section = ""
        sub_section = ""
        pending_sub_sym = None
        pending_sub_kind = None  # "func" or "var"

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

        try:
            with open(filepath, 'r', errors='ignore') as f:
                for raw_line in f:
                    line = raw_line.rstrip('\r\n')
                    stripped = line.strip()
                    if not stripped:
                        continue

                    # 1. Top-level output section (starts at column 0)
                    if not line[0].isspace():
                        m_top = re.match(r'^(\.[a-zA-Z0-9_.*]+|COMMON)\b', stripped)
                        if m_top:
                            top_section = m_top.group(1)
                            sub_section = top_section
                            pending_sub_sym = None
                            pending_sub_kind = None

                    # 2. Input subsection (e.g. " .text.my_func" or " .bss.my_var")
                    m_sub = re.match(r'^\s+(\.[a-zA-Z0-9_.$*]+|COMMON)(?:\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+))?', line)
                    if m_sub:
                        sub_section = m_sub.group(1)
                        kind = classify_section(sub_section) or classify_section(top_section)

                        # Extract symbol suffix from -ffunction-sections / -fdata-sections (e.g., .text.PID_Step -> PID_Step)
                        sub_sym = None
                        for prefix in (".text.", ".data.", ".bss.", ".sdata.", ".sbss.", ".uncached_lsram."):
                            if sub_section.startswith(prefix):
                                candidate = sub_section[len(prefix):]
                                if candidate not in ignored_subsections and re.match(r'^[a-zA-Z_]\w*$', candidate):
                                    sub_sym = candidate
                                break

                        if m_sub.group(2) and m_sub.group(3):
                            # Single-line subsection with address and size
                            addr_int = int(m_sub.group(2), 16)
                            size_int = int(m_sub.group(3), 16)
                            if addr_int > 0 and size_int > 0 and sub_sym and not sub_sym.startswith(ignored_prefixes):
                                norm_addr = f"0x{addr_int:08X}"
                                self.symbol_table[sub_sym] = norm_addr
                                if kind == "func":
                                    self.func_symbols[sub_sym] = norm_addr
                                elif kind == "var":
                                    self.var_symbols[sub_sym] = norm_addr
                            pending_sub_sym = None
                            pending_sub_kind = None
                        else:
                            # Wrapped subsection: address + size will appear on the next line
                            pending_sub_sym = sub_sym
                            pending_sub_kind = kind
                        continue

                    # 3. Wrapped subsection continuation line: "                0x0000000080000240       0x54 ./src/motor.o"
                    if pending_sub_sym:
                        m_wrap = re.match(r'^\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+\S+', line)
                        if m_wrap:
                            addr_int = int(m_wrap.group(1), 16)
                            size_int = int(m_wrap.group(2), 16)
                            if addr_int > 0 and size_int > 0 and not pending_sub_sym.startswith(ignored_prefixes):
                                norm_addr = f"0x{addr_int:08X}"
                                self.symbol_table[pending_sub_sym] = norm_addr
                                if pending_sub_kind == "func":
                                    self.func_symbols[pending_sub_sym] = norm_addr
                                elif pending_sub_kind == "var":
                                    self.var_symbols[pending_sub_sym] = norm_addr
                            pending_sub_sym = None
                            pending_sub_kind = None
                            continue
                        pending_sub_sym = None
                        pending_sub_kind = None

                    # 4. Standard Global Symbol Line: "                0x0000000080000240                Motor_Control_Init"
                    m_sym = re.match(r'^\s+(0x[0-9a-fA-F]+)\s+([a-zA-Z_]\w*)$', line)
                    if m_sym:
                        addr_str, sym_name = m_sym.group(1), m_sym.group(2)
                        if sym_name.startswith(ignored_prefixes):
                            continue
                        addr_int = int(addr_str, 16)
                        if addr_int == 0:
                            continue
                        norm_addr = f"0x{addr_int:08X}"
                        self.symbol_table[sym_name] = norm_addr

                        kind = classify_section(sub_section) or classify_section(top_section)
                        if kind == "func":
                            self.func_symbols[sym_name] = norm_addr
                        elif kind == "var":
                            self.var_symbols[sym_name] = norm_addr

            # Fallback if map file had non-standard section headers
            if not self.var_symbols:
                self.var_symbols.update(self.symbol_table)
            if not self.func_symbols:
                self.func_symbols.update(self.symbol_table)

            fname = os.path.basename(filepath)
            self.map_label.configure(
                text=f"{fname} ({len(self.var_symbols)} vars, {len(self.func_symbols)} funcs)",
                text_color="#4CAF50"
            )
            self.log_msg(f"Loaded .map: {len(self.var_symbols)} RAM variables and {len(self.func_symbols)} functions ready to scroll & select.")
            self.notify_symbol_files_updated()
        except Exception as e:
            self.log_msg(f"ERROR reading map file: {e}")

    def load_lst_file(self):
        filepath = filedialog.askopenfilename(filetypes=[("Disassembly Listing", "*.lst *.txt"), ("All Files", "*.*")])
        if not filepath: return
        self.lst_table.clear()

        pending_line_key = None
        try:
            with open(filepath, 'r', errors='ignore') as f:
                for line in f:
                    stripped = line.strip()
                    m_src = re.match(r'^(.+?\.[cChHsS](?:pp)?):(\d+)', stripped)
                    if m_src:
                        base_file = os.path.basename(m_src.group(1))
                        line_num = m_src.group(2)
                        pending_line_key = f"{base_file}:{line_num}"
                        continue

                    if pending_line_key:
                        m_inst = re.match(r'^\s*([0-9a-fA-F]{4,16}):\s+[0-9a-fA-F]{4,8}\s+', line)
                        if m_inst:
                            addr_int = int(m_inst.group(1), 16)
                            if pending_line_key not in self.lst_table:
                                self.lst_table[pending_line_key] = f"0x{addr_int:08X}"
                            pending_line_key = None

            fname = os.path.basename(filepath)
            self.lst_label.configure(text=f"{fname} ({len(self.lst_table)} lines)", text_color="#4CAF50")
            self.log_msg(f"Loaded optional .lst file: mapped {len(self.lst_table)} source lines to instruction addresses.")
            self.notify_symbol_files_updated()
        except Exception as e:
            self.log_msg(f"ERROR reading .lst file: {e}")

    def save_layout(self):
        filepath = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON Layouts", "*.json")])
        if not filepath: return
        layout_data = []
        for widget in self.scrollable_frame.winfo_children():
            if hasattr(widget, 'get_state'):
                layout_data.append(widget.get_state())
        try:
            with open(filepath, 'w') as f: json.dump(layout_data, f, indent=4)
            self.log_msg(f"Layout saved successfully: {len(layout_data)} widgets.")
        except Exception as e: self.log_msg(f"ERROR saving layout: {e}")

    def load_layout(self):
        filepath = filedialog.askopenfilename(filetypes=[("JSON Layouts", "*.json"), ("All Files", "*.*")])
        if not filepath: return
        try:
            with open(filepath, 'r') as f: layout_data = json.load(f)
            for widget in self.scrollable_frame.winfo_children(): widget.destroy()
            for state in layout_data:
                w_type = state.get("widget_type")
                if w_type == "write": new_w = VarSlider(self.scrollable_frame, self)
                elif w_type == "read": new_w = ReadMonitor(self.scrollable_frame, self)
                elif w_type == "plotter": new_w = MemBlockPlotter(self.scrollable_frame, self)
                elif w_type == "loader": new_w = MemFileLoader(self.scrollable_frame, self)
                elif w_type == "alive_light": new_w = AliveLightWidget(self.scrollable_frame, self)
                elif w_type == "bp_func": new_w = BreakOnFuncWidget(self.scrollable_frame, self)
                elif w_type == "bp_line": new_w = BreakOnLineWidget(self.scrollable_frame, self)
                elif w_type == "bp_var": new_w = BreakOnVarWidget(self.scrollable_frame, self)
                else: continue
                new_w.set_state(state)
            self.log_msg(f"Layout loaded successfully: {len(layout_data)} widgets.")
        except Exception as e: self.log_msg(f"ERROR loading layout: {e}")

    def log_msg(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

if __name__ == "__main__":
    app = ModernOpenOCD_GUI()
    app.mainloop()