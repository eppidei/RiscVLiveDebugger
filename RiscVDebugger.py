import customtkinter as ctk
from tkinter import filedialog
import socket
import threading
import re
import json
import os
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

                    send_and_wait("halt; wait_halt")
                    send_and_wait(f"{mem_cmd} {addr} {hex_val}")
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

    def execute_read(self):
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

                    send_and_wait("halt; wait_halt")
                    read_output = send_and_wait(f"{mem_cmd} {addr}")
                    send_and_wait("resume")
                    
                    self.after(0, lambda: self.process_read_result(read_output))
            except Exception:
                pass # Fail silently on timeout during fast polling

        threading.Thread(target=_read_task, daemon=True).start()

        if self.auto_poll.get():
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

    def execute_dump(self):
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

                    send_and_wait("halt; wait_halt")
                    raw_data = send_and_wait(f"{cmd} {addr} {count}")
                    send_and_wait("resume")
                    
                    self.after(0, lambda: self.process_dump(raw_data))
            except Exception as e:
                pass 

        threading.Thread(target=_dump_task, daemon=True).start()
        
        if self.auto_poll.get():
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

                    send_and_wait("halt; wait_halt")
                    
                    batch_size = 30
                    for i in range(0, len(self.file_data), batch_size):
                        batch = self.file_data[i:i+batch_size]
                        cmd_string = ""
                        for j, val in enumerate(batch):
                            addr = hex(base_addr_int + (i + j) * byte_step)
                            cmd_string += f"{cmd} {addr} {hex(val)}; "
                        send_and_wait(cmd_string)
                        
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

        # Circular Indicator Lamp
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

    def execute_read(self):
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

                    send_and_wait("halt; wait_halt")
                    output = send_and_wait(f"{cmd} {addr}")
                    send_and_wait("resume")
                    
                    self.after(0, lambda: self.process_read(output))
            except Exception:
                self.after(0, lambda: self.set_error("Comm Error"))

        threading.Thread(target=_read_task, daemon=True).start()
        
        if self.auto_poll.get():
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


class ModernOpenOCD_GUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PolarFire Live Tuner & Data Logger")
        self.geometry("1100x800")
        self.symbol_table = {}
        self.ocd_process = None
        
        self.ocd_exe_path = ctk.StringVar()
        self.ocd_cfg_path = ctk.StringVar()
        
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.build_ui()
        self.log_msg("Ready. Select OpenOCD settings, or just load a .map file if OpenOCD is already running.")

    def build_ui(self):
        ocd_frame = ctk.CTkFrame(self, fg_color="#1E1E1E", border_width=1, border_color="#555555")
        ocd_frame.pack(fill="x", padx=20, pady=(15, 5))

        footer = ctk.CTkLabel(
        self, 
        text="Live Memory Tuner | Developed by Leonardo Di Carlo | GitHub: https://github.com/eppidei/RiscVLiveDebugger", 
        text_color="#666666", 
        font=("Consolas", 10)
        )
        footer.pack(side="bottom", pady=2)
        
        ctk.CTkLabel(ocd_frame, text="OpenOCD Exe:").grid(row=0, column=0, padx=10, pady=5)
        ctk.CTkEntry(ocd_frame, textvariable=self.ocd_exe_path, width=250).grid(row=0, column=1, padx=5, pady=5)
        ctk.CTkButton(ocd_frame, text="Browse", width=60, command=self.browse_exe).grid(row=0, column=2, padx=5, pady=5)
        
        ctk.CTkLabel(ocd_frame, text="Main CFG:").grid(row=0, column=3, padx=10, pady=5)
        ctk.CTkEntry(ocd_frame, textvariable=self.ocd_cfg_path, width=250).grid(row=0, column=4, padx=5, pady=5)
        ctk.CTkButton(ocd_frame, text="Browse", width=60, command=self.browse_cfg).grid(row=0, column=5, padx=5, pady=5)

        self.btn_start_ocd = ctk.CTkButton(ocd_frame, text="Start OpenOCD", fg_color="#00695C", hover_color="#004D40", command=self.toggle_openocd)
        self.btn_start_ocd.grid(row=0, column=6, padx=15, pady=5)
        
        ctk.CTkButton(ocd_frame, text="Reset CPU", width=80, fg_color="#D84315", hover_color="#BF360C", command=self.reset_target).grid(row=0, column=7, padx=5, pady=5)

        file_frame = ctk.CTkFrame(self, fg_color="transparent")
        file_frame.pack(fill="x", padx=20, pady=5)
        
        ctk.CTkButton(file_frame, text="Upload .map File", width=120, command=self.load_map_file).pack(side="left")
        self.map_label = ctk.CTkLabel(file_frame, text="No map file loaded.", text_color="gray")
        self.map_label.pack(side="left", padx=15)
        
        ctk.CTkButton(file_frame, text="Save Layout", width=90, fg_color="#555555", command=self.save_layout).pack(side="right")
        ctk.CTkButton(file_frame, text="Load Layout", width=90, fg_color="#555555", command=self.load_layout).pack(side="right", padx=10)
        
        add_frame = ctk.CTkFrame(self, fg_color="transparent")
        add_frame.pack(fill="x", padx=20, pady=(0, 10))

        ctk.CTkButton(add_frame, text="+ Read Var", width=90, fg_color="#0277BD", hover_color="#01579B", 
                      command=lambda: ReadMonitor(self.scrollable_frame, self)).pack(side="left", padx=(0, 10))
        ctk.CTkButton(add_frame, text="+ Write Slider", width=90, fg_color="#2E7D32", hover_color="#1B5E20", 
                      command=lambda: VarSlider(self.scrollable_frame, self)).pack(side="left", padx=(0, 10))
        ctk.CTkButton(add_frame, text="+ Mem Plotter", width=90, fg_color="#8E24AA", hover_color="#6A1B9A", 
                      command=lambda: MemBlockPlotter(self.scrollable_frame, self)).pack(side="left", padx=(0, 10))
        ctk.CTkButton(add_frame, text="+ Mem Loader", width=90, fg_color="#F57C00", hover_color="#E65100", 
                      command=lambda: MemFileLoader(self.scrollable_frame, self)).pack(side="left", padx=(0, 10))
        ctk.CTkButton(add_frame, text="+ Alive Light", width=90, fg_color="#FBC02D", hover_color="#F9A825", text_color="black", 
                      command=lambda: AliveLightWidget(self.scrollable_frame, self)).pack(side="left")

        self.scrollable_frame = ctk.CTkScrollableFrame(self, corner_radius=10)
        self.scrollable_frame.pack(side="top", fill="both", expand=True, padx=20, pady=5)

        self.log_text = ctk.CTkTextbox(self, height=100, state="disabled", font=("Consolas", 12))
        self.log_text.pack(pady=15, padx=20, fill="x")

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
            
            cmd = [exe, "-s", cfg_dir, "-f", cfg_file]
            
            try:
                c_flags = subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
                self.ocd_process = subprocess.Popen(cmd, cwd=cfg_dir, creationflags=c_flags)
                self.btn_start_ocd.configure(text="Stop OpenOCD", fg_color="#C62828", hover_color="#b71c1c")
                self.log_msg("OpenOCD started. Check the new terminal window for errors.")
            except Exception as e:
                self.log_msg(f"Failed to start OpenOCD: {e}")
        else:
            self.ocd_process.terminate()
            self.ocd_process.wait()
            self.ocd_process = None
            self.btn_start_ocd.configure(text="Start OpenOCD", fg_color="#00695C", hover_color="#004D40")
            self.log_msg("OpenOCD server stopped.")

    def reset_target(self):
        def _task():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect(("localhost", 6666))
                    s.sendall("reset run\x1a".encode('utf-8'))
                    self.after(0, lambda: self.log_msg("Sent 'reset run' command to target."))
            except Exception as e:
                self.after(0, lambda: self.log_msg(f"Reset failed (Is OpenOCD running?): {e}"))
        threading.Thread(target=_task, daemon=True).start()

    def on_closing(self):
        if self.ocd_process:
            self.log_msg("Terminating OpenOCD before exit...")
            self.ocd_process.terminate()
            self.ocd_process.wait()
        self.destroy()

    def load_map_file(self):
        filepath = filedialog.askopenfilename(filetypes=[("Map Files", "*.map"), ("All Files", "*.*")])
        if not filepath: return
        self.symbol_table.clear()
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[0].startswith("0x"):
                        addr, sym = parts[0], parts[-1] 
                        if re.match(r'^[a-zA-Z_]\w*$', sym):
                            self.symbol_table[sym] = addr
            self.map_label.configure(text=f"Loaded: {filepath.split('/')[-1]} ({len(self.symbol_table)} vars)", text_color="#4CAF50")
            self.log_msg(f"Map file loaded. Found {len(self.symbol_table)} variables.")
        except Exception as e:
            self.log_msg(f"ERROR reading map file: {e}")

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