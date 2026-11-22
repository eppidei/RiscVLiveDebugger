# widgets_telemetry.py
import re
import struct
import threading
from collections import deque
import customtkinter as ctk
from tkinter import filedialog

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from ocd_backend import OpenOCDSession


import tkinter as tk


class BaseWidget(ctk.CTkFrame):
    GRID_SNAP = 20  # Snap position and size to 20px grid

    # Default (width, height) tailored to each widget class
    DEFAULT_SIZES = {
        "AliveLightWidget": (520, 120),
        "VarSlider": (560, 145),
        "EnumSelectorWidget": (560, 175),
        "MemFileLoader": (540, 155),
        "ReadMonitor": (580, 130),
        "MemBlockPlotter": (600, 390),
        "BreakOnFuncWidget": (880, 310),
        "BreakOnLineWidget": (880, 310),
        "BreakOnVarWidget": (880, 310),
    }

    def __init__(self, parent, **kwargs):
        cls_name = self.__class__.__name__
        def_w, def_h = self.DEFAULT_SIZES.get(cls_name, (560, 150))
        kwargs.setdefault("width", def_w)
        kwargs.setdefault("height", def_h)
        super().__init__(parent, **kwargs)

        # Use _tile_* prefix so we never overwrite Tkinter's reserved self._w path string
        self._tile_w = kwargs["width"]
        self._tile_h = kwargs["height"]
        self._tile_x = 20
        self._tile_y = 20

        self._drag_start_x = 0
        self._drag_start_y = 0
        self._resize_start_x = 0
        self._resize_start_y = 0
        self._resize_start_w = self._tile_w
        self._resize_start_h = self._tile_h

    def pack(self, *args, **kwargs):
        """
        Intercepts the subclass self.pack(...) call and places the widget
        as a draggable, resizable 2D tile on the parent surface.
        """
        self.pack_propagate(False)
        self.grid_propagate(False)

        siblings = [
            w for w in self.master.winfo_children()
            if isinstance(w, BaseWidget) and w is not self
        ]
        if not siblings:
            self._tile_x, self._tile_y = 20, 20
        else:
            last = siblings[-1]
            if last._tile_x + last._tile_w + self._tile_w + 40 <= 1140:
                self._tile_x = last._tile_x + last._tile_w + 20
                self._tile_y = last._tile_y
            else:
                max_bottom = max(w._tile_y + w._tile_h for w in siblings)
                self._tile_x = 20
                self._tile_y = max_bottom + 20

        self.place(x=self._tile_x, y=self._tile_y)
        self._install_drag_and_resize_handles()
        self._update_canvas_height()

    def _install_drag_and_resize_handles(self):
        # 1. Top-left Drag Handle ("⠿")
        self.drag_handle = ctk.CTkLabel(
            self,
            text="⠿",
            font=("Consolas", 16, "bold"),
            text_color="#888888",
            width=18,
            height=18,
            cursor="fleur",
        )
        self.drag_handle.place(x=3, y=3)

        for target in (self.drag_handle, self):
            target.bind("<ButtonPress-1>", self._on_drag_start)
            target.bind("<B1-Motion>", self._on_drag_motion)

        # 2. Bottom-right Resize Grip ("◢")
        self.resize_grip = ctk.CTkLabel(
            self,
            text="◢",
            font=("Consolas", 13),
            text_color="#888888",
            width=16,
            height=16,
            cursor="size_nw_se",
        )
        self.resize_grip.place(relx=1.0, rely=1.0, x=-3, y=-3, anchor="se")
        self.resize_grip.bind("<ButtonPress-1>", self._on_resize_start)
        self.resize_grip.bind("<B1-Motion>", self._on_resize_motion)

        # Raise handles above inner frames after subclass __init__ finishes
        self.after(20, self._lift_handles)

    def _lift_handles(self):
        if self.winfo_exists():
            self.drag_handle.lift()
            self.resize_grip.lift()

    def _on_drag_start(self, event):
        self.lift()
        self._lift_handles()
        self._drag_start_x = event.x_root - self._tile_x
        self._drag_start_y = event.y_root - self._tile_y

    def _on_drag_motion(self, event):
        raw_x = event.x_root - self._drag_start_x
        raw_y = event.y_root - self._drag_start_y
        self._tile_x = max(10, round(raw_x / self.GRID_SNAP) * self.GRID_SNAP)
        self._tile_y = max(10, round(raw_y / self.GRID_SNAP) * self.GRID_SNAP)
        self.place(x=self._tile_x, y=self._tile_y)
        self._update_canvas_height()

    def _on_resize_start(self, event):
        self.lift()
        self._lift_handles()
        self._resize_start_x = event.x_root
        self._resize_start_y = event.y_root
        self._resize_start_w = self._tile_w
        self._resize_start_h = self._tile_h

    def _on_resize_motion(self, event):
        dx = event.x_root - self._resize_start_x
        dy = event.y_root - self._resize_start_y
        new_w = max(320, round((self._resize_start_w + dx) / self.GRID_SNAP) * self.GRID_SNAP)
        new_h = max(100, round((self._resize_start_h + dy) / self.GRID_SNAP) * self.GRID_SNAP)
        if new_w != self._tile_w or new_h != self._tile_h:
            self._tile_w = new_w
            self._tile_h = new_h
            self.configure(width=self._tile_w, height=self._tile_h)
            self._lift_handles()
            self._update_canvas_height()

    def set_tile_size(self, w=None, h=None):
        if w is not None:
            self._tile_w = w
        if h is not None:
            self._tile_h = h
        self.configure(width=self._tile_w, height=self._tile_h)
        self._lift_handles()
        self._update_canvas_height()

    def _update_canvas_height(self):
        """Keeps an invisible spacer inside CTkScrollableFrame so the scrollbar tracks .place() tiles."""
        siblings = [w for w in self.master.winfo_children() if isinstance(w, BaseWidget)]
        max_bottom = max((w._tile_y + w._tile_h for w in siblings), default=600)
        try:
            spacer = getattr(self.master, "_tile_spacer", None)
            if spacer is None or not spacer.winfo_exists():
                spacer = tk.Frame(self.master, width=1, height=700, highlightthickness=0, bd=0)
                spacer.pack(side="top", anchor="nw")
                self.master._tile_spacer = spacer
            spacer.configure(height=max(700, max_bottom + 80))
        except Exception:
            pass

    def get_geometry_state(self):
        return {"x": self._tile_x, "y": self._tile_y, "w": self._tile_w, "h": self._tile_h}

    def set_geometry_state(self, state):
        if "x" in state and "y" in state:
            self._tile_x = state["x"]
            self._tile_y = state["y"]
            self.place(x=self._tile_x, y=self._tile_y)
        if "w" in state and "h" in state:
            self._tile_w = state["w"]
            self._tile_h = state["h"]
            self.configure(width=self._tile_w, height=self._tile_h)
        self._lift_handles()
        self._update_canvas_height()

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
        ctk.CTkComboBox(
            config_frame,
            variable=self.var_type,
            values=["uint8", "uint16", "uint32", "int8", "int16", "int32"],
            width=85,
        ).grid(row=0, column=3, padx=5)

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
        if not self.winfo_exists():
            return
        int_val = int(val)
        self.current_val_label.configure(text=f"Target Value: {int_val}")

        if self.slider_timer:
            self.after_cancel(self.slider_timer)
        self.slider_timer = self.after(200, lambda: self.execute_write(int_val))

    def execute_write(self, value):
        if not self.winfo_exists():
            return
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
                    with OpenOCDSession(timeout=2.0) as ocd:
                        state = ocd.send("[[target current] curstate]")
                        was_running = "running" in state
                        if was_running:
                            ocd.send("halt; wait_halt")
                        ocd.send(f"{mem_cmd} {addr} {hex_val}")
                        if was_running and not self.app.target_halted_at_bp:
                            ocd.send("resume")
                        self.after(0, lambda: self.app.log_msg(f"Wrote {value} ({hex_val}) to {name}"))
                except Exception as e:
                    self.after(0, lambda: self.app.log_msg(f"Write ERROR: {str(e)}"))

        threading.Thread(target=_write_task, daemon=True).start()

    def get_state(self):
        return {
            "widget_type": "write",
            "var_name": self.var_name.get(),
            "var_type": self.var_type.get(),
            "min_val": self.min_val.get(),
            "max_val": self.max_val.get(),
        }

    def set_state(self, state):
        self.var_name.set(state.get("var_name", "motor_speed"))
        self.var_type.set(state.get("var_type", "uint32"))
        self.min_val.set(state.get("min_val", "0"))
        self.max_val.set(state.get("max_val", "255"))
        self.update_config()


class EnumSelectorWidget(BaseWidget):
    # --- DISCRETE VALUE / CHOICE SWITCHER WIDGET ---
    def __init__(self, parent, app):
        super().__init__(parent, corner_radius=10, border_width=2, border_color="#3F51B5")
        self.app = app
        self.pack(fill="x", pady=10, padx=10)

        self.var_name = ctk.StringVar(value="operating_mode")
        self.var_type = ctk.StringVar(value="uint32")

        self.choices_str = ctk.StringVar(value="OFF:0, ON:1")
        self.current_selection = ctk.StringVar(value="OFF")

        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(10, 5))

        ctk.CTkLabel(config_frame, text="Choice | Addr/Name:").grid(row=0, column=0, padx=5)
        ctk.CTkEntry(config_frame, textvariable=self.var_name, width=140).grid(row=0, column=1, padx=5)

        ctk.CTkLabel(config_frame, text="Type:").grid(row=0, column=2, padx=5)
        ctk.CTkComboBox(
            config_frame,
            variable=self.var_type,
            values=["uint8", "uint16", "uint32", "int8", "int16", "int32", "float"],
            width=85,
        ).grid(row=0, column=3, padx=5)

        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=4, padx=10)

        choices_frame = ctk.CTkFrame(self, fg_color="transparent")
        choices_frame.pack(fill="x", padx=10, pady=(0, 5))

        ctk.CTkLabel(choices_frame, text="Values [Label:Val, ...]:").pack(side="left", padx=5)
        self.choices_entry = ctk.CTkEntry(choices_frame, textvariable=self.choices_str, width=320)
        self.choices_entry.pack(side="left", padx=5, fill="x", expand=True)
        ctk.CTkButton(choices_frame, text="Set Choices", width=95, command=self.rebuild_buttons).pack(side="left", padx=5)

        self.btn_container = ctk.CTkFrame(self, fg_color="#1E1E1E", corner_radius=6)
        self.btn_container.pack(fill="x", padx=15, pady=(5, 12))

        self.parsed_choices = {}
        self.segmented_btn = None
        self.rebuild_buttons()

    def rebuild_buttons(self):
        raw = self.choices_str.get()
        self.parsed_choices.clear()

        for pair in raw.split(","):
            if ":" in pair:
                label, val_str = pair.split(":", 1)
                label = label.strip()
                val_str = val_str.strip()
                try:
                    if "." in val_str or (val_str.lower().endswith("f") and not val_str.lower().startswith("0x")):
                        val = float(val_str.lower().rstrip("f"))
                    elif val_str.lower().startswith("0x"):
                        val = int(val_str, 16)
                    else:
                        val = int(val_str)
                    self.parsed_choices[label] = val
                except ValueError:
                    pass

        if not self.parsed_choices:
            self.parsed_choices = {"OFF": 0, "ON": 1}

        if self.segmented_btn:
            self.segmented_btn.destroy()

        labels = list(self.parsed_choices.keys())
        if self.current_selection.get() not in labels:
            self.current_selection.set(labels[0])

        self.segmented_btn = ctk.CTkSegmentedButton(
            self.btn_container,
            values=labels,
            variable=self.current_selection,
            command=self.on_choice_selected,
        )
        self.segmented_btn.pack(fill="x", padx=10, pady=8)

    def on_choice_selected(self, label):
        if not self.winfo_exists():
            return
        val = self.parsed_choices.get(label)
        if val is not None:
            self.execute_write(val)

    def execute_write(self, value):
        if not self.winfo_exists():
            return
        name = self.var_name.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.app.log_msg(f"Wait: '{name}' is not a valid map symbol or hex address!")
            return

        v_type = self.var_type.get()
        if v_type == "float":
            try:
                packed = struct.pack("<f", float(value))
                int_val = struct.unpack("<I", packed)[0]
                hex_val = f"0x{int_val:08X}"
                mem_cmd = "mww"
            except Exception as e:
                self.app.log_msg(f"Float Write ERROR: {e}")
                return
        else:
            int_val = int(value)
            if int_val < 0:
                bits = 8 if "8" in v_type else (16 if "16" in v_type else 32)
                int_val = (1 << bits) + int_val

            cmd_map = {"8": "mwb", "16": "mwh", "32": "mww"}
            mem_cmd = cmd_map.get(v_type.replace("uint", "").replace("int", ""), "mww")
            hex_val = hex(int_val)

        def _write_task():
            with self.app.jtag_lock:
                try:
                    with OpenOCDSession(timeout=2.0) as ocd:
                        state = ocd.send("[target current] curstate")
                        was_running = "running" in state
                        if was_running:
                            ocd.send("halt; wait_halt")
                        ocd.send(f"{mem_cmd} {addr} {hex_val}")
                        if was_running and not self.app.target_halted_at_bp:
                            ocd.send("resume")
                        self.after(
                            0,
                            lambda: self.app.log_msg(
                                f"Choice Write: set '{name}' to {self.current_selection.get()} ({hex_val})"
                            ),
                        )
                except Exception as e:
                    self.after(0, lambda: self.app.log_msg(f"Choice Write ERROR: {str(e)}"))

        threading.Thread(target=_write_task, daemon=True).start()

    def get_state(self):
        return {
            "widget_type": "enum_selector",
            "var_name": self.var_name.get(),
            "var_type": self.var_type.get(),
            "choices_str": self.choices_str.get(),
            "current_selection": self.current_selection.get(),
        }

    def set_state(self, state):
        self.var_name.set(state.get("var_name", "operating_mode"))
        self.var_type.set(state.get("var_type", "uint32"))
        self.choices_str.set(state.get("choices_str", "OFF:0, ON:1"))
        self.rebuild_buttons()
        sel = state.get("current_selection")
        if sel in self.parsed_choices:
            self.current_selection.set(sel)


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
        ctk.CTkComboBox(
            config_frame,
            variable=self.var_type,
            values=["uint8", "uint16", "uint32", "int8", "int16", "int32"],
            width=85,
            command=lambda x: self.execute_read(),
        ).grid(row=0, column=3, padx=2)

        ctk.CTkLabel(config_frame, text="Frac Bits:").grid(row=0, column=4, padx=(10, 2))
        ctk.CTkEntry(config_frame, textvariable=self.frac_bits, width=35).grid(row=0, column=5, padx=2)

        ctk.CTkCheckBox(config_frame, text="Bitfields", variable=self.show_bitfields, command=self.toggle_bitfields).grid(row=0, column=6, padx=(15, 5))
        ctk.CTkCheckBox(config_frame, text="Plot", variable=self.show_plot, command=self.toggle_plot).grid(row=0, column=7, padx=5)
        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=8, padx=(10, 5))

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
        for _ in range(4):
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

        ctk.CTkCheckBox(plot_ctrl_frame, text="Auto-Y", variable=self.auto_scale, width=60, command=self.update_plot_visuals).pack(side="left", padx=(15, 5))

        ctk.CTkLabel(plot_ctrl_frame, text="Y-Min:").pack(side="left", padx=(5, 2))
        ctk.CTkEntry(plot_ctrl_frame, textvariable=self.y_min, width=50).pack(side="left", padx=2)

        ctk.CTkLabel(plot_ctrl_frame, text="Y-Max:").pack(side="left", padx=(10, 2))
        ctk.CTkEntry(plot_ctrl_frame, textvariable=self.y_max, width=50).pack(side="left", padx=2)

        ctk.CTkButton(plot_ctrl_frame, text="Clear", width=60, fg_color="#555555", command=self.clear_plot).pack(side="right")

        self.fig = Figure(figsize=(8, 2.2), dpi=100)
        self.fig.patch.set_facecolor("#242424")
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.9, bottom=0.2)

        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#1E1E1E")
        self.ax.tick_params(colors="white", labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color("#555555")

        (self.line,) = self.ax.plot([], [], color="#00a8ff", linewidth=1.5)

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
        if not self.winfo_exists():
            return
        # If user manually clicks Read Now after stopping OpenOCD, clear the abort latch
        if not self.auto_poll.get() and not halted_only:
            self.app.abort_transfers = False

        # Drop overlapping poll ticks so threads never pile up in a queue
        if getattr(self, "_busy", False) and not halted_only:
            return

        name = self.var_name.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.value_label.configure(text="ERR")
            self.auto_poll.set(False)
            self.app.log_msg(f"Wait: '{name}' is not a valid map symbol or hex address!")
            return

        try:
            interval = int(self.poll_interval.get())
            if interval < 50:
                interval = 50
                self.poll_interval.set("50")
        except ValueError:
            interval = 500
            self.poll_interval.set("500")

        v_type = self.var_type.get()
        cmd_map = {"8": "mdb", "16": "mdh", "32": "mdw"}
        mem_cmd = cmd_map.get(v_type.replace("uint", "").replace("int", ""), "mdw")
        self._busy = True

        def _read_task():
            try:
                with self.app.jtag_lock:
                    # Immediately exit if OpenOCD was stopped while this thread waited for the lock
                    if self.app.abort_transfers:
                        return
                    try:
                        with OpenOCDSession(timeout=2.0) as ocd:
                            if halted_only or self.app.target_halted_at_bp:
                                read_output = ocd.send(f"{mem_cmd} {addr}")
                            else:
                                state = ocd.send("[target current] curstate")
                                if "halted" in state:
                                    self.after(0, self.app.disable_all_auto_poll)
                                    read_output = ocd.send(f"{mem_cmd} {addr}")
                                else:
                                    ocd.send("halt; wait_halt")
                                    read_output = ocd.send(f"{mem_cmd} {addr}")
                                    ocd.send("resume")

                            if not self.app.abort_transfers:
                                self.after(0, lambda: self.process_read_result(read_output))
                    except Exception:
                        pass
            finally:
                self._busy = False
                # Schedule next poll ONLY after current read completes (prevents queue build-up)
                if self.winfo_exists() and self.auto_poll.get() and not halted_only and not self.app.abort_transfers:
                    self.after(interval, self.execute_read)

        threading.Thread(target=_read_task, daemon=True).start()

    def process_read_result(self, response):
        if not self.winfo_exists():
            return

        match = re.search(r":\s*([0-9a-fA-F]+)", response)
        if match:
            try:
                raw_val = int(match.group(1), 16)

                v_type = self.var_type.get()
                is_signed = v_type.startswith("int")
                bits = 8 if "8" in v_type else (16 if "16" in v_type else 32)

                val = raw_val
                if is_signed and (raw_val & (1 << (bits - 1))):
                    val = raw_val - (1 << bits)

                try:
                    f_bits = int(self.frac_bits.get())
                except ValueError:
                    f_bits = 0

                numeric_val = val / (1 << f_bits) if f_bits > 0 else val

                if f_bits > 0:
                    real_str = f"{numeric_val:.6f}".rstrip("0").rstrip(".") or "0"
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
                                m = re.match(r"^(\d+)(?:[:-](\d+))?$", rng_str)
                                if m:
                                    high = int(m.group(1))
                                    low = int(m.group(2)) if m.group(2) else high
                                    if low > high:
                                        high, low = low, high

                                    width = high - low + 1
                                    mask = ((1 << width) - 1) << low
                                    bf_val = (raw_val & mask) >> low

                                    if width == 1:
                                        bf["label"].configure(text=f"{bf_val}")
                                    else:
                                        bf["label"].configure(text=f"{bf_val} (0x{bf_val:X})")
                                else:
                                    bf["label"].configure(text="SYNTAX ERR")
                            except Exception:
                                bf["label"].configure(text="ERR")
                        else:
                            bf["label"].configure(text="-")

                try:
                    buf_sz = max(10, int(self.plot_buffer_size.get()))
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
            "bitfields": bfs,
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

        if self.show_bitfields.get():
            self.toggle_bitfields()
        if self.show_plot.get():
            self.toggle_plot()
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

        ctk.CTkLabel(config_frame, text="Block Dump | Addr:").grid(row=0, column=0, padx=(5, 2))
        ctk.CTkEntry(config_frame, textvariable=self.base_addr, width=120).grid(row=0, column=1, padx=2)

        ctk.CTkLabel(config_frame, text="Size:").grid(row=0, column=2, padx=(10, 2))
        ctk.CTkComboBox(config_frame, variable=self.word_size, values=["8", "16", "32"], width=65).grid(row=0, column=3, padx=2)

        ctk.CTkLabel(config_frame, text="Count:").grid(row=0, column=4, padx=(10, 2))
        ctk.CTkEntry(config_frame, textvariable=self.word_count, width=50).grid(row=0, column=5, padx=2)

        ctk.CTkLabel(config_frame, text="Style:").grid(row=0, column=6, padx=(10, 2))
        ctk.CTkComboBox(config_frame, variable=self.plot_type, values=["Bar", "Line"], width=75, command=lambda x: self.force_redraw()).grid(row=0, column=7, padx=2)

        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=8, padx=(15, 5))

        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.pack(fill="x", padx=15, pady=(0, 5))

        ctk.CTkButton(action_frame, text="Dump & Plot", width=100, command=self.execute_dump).pack(side="left")

        ctk.CTkEntry(action_frame, textvariable=self.poll_interval, width=50).pack(side="right")
        ctk.CTkLabel(action_frame, text="ms").pack(side="right", padx=(0, 5))
        ctk.CTkSwitch(action_frame, text="Auto", variable=self.auto_poll, command=self.toggle_poll).pack(side="right", padx=(10, 5))

        self.fig = Figure(figsize=(8, 3.5), dpi=100)
        self.fig.patch.set_facecolor("#242424")
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.9, bottom=0.15)
        self.ax = self.fig.add_subplot(111)
        self.setup_axes()

        self.last_values = []
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(5, 10))

    def setup_axes(self):
        self.ax.set_facecolor("#1E1E1E")
        self.ax.tick_params(colors="white", labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color("#555555")

    def toggle_poll(self):
        if self.auto_poll.get():
            self.execute_dump()

    def force_redraw(self):
        if self.last_values:
            self.draw_graph(self.last_values)

    def execute_dump(self, halted_only=False):
        if not self.winfo_exists():
            return
        if not self.auto_poll.get() and not halted_only:
            self.app.abort_transfers = False

        if getattr(self, "_busy", False) and not halted_only:
            return

        name = self.base_addr.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.auto_poll.set(False)
            self.app.log_msg("Plotter Error: Invalid address or map symbol.")
            return

        try:
            count = int(self.word_count.get())
        except ValueError:
            count = 64

        try:
            interval = max(100, int(self.poll_interval.get()))
        except ValueError:
            interval = 1000
            self.poll_interval.set("1000")

        ws = self.word_size.get()
        cmd = "mdb" if ws == "8" else ("mdh" if ws == "16" else "mdw")
        self._busy = True

        def _dump_task():
            try:
                with self.app.jtag_lock:
                    if self.app.abort_transfers:
                        return
                    try:
                        with OpenOCDSession(timeout=3.0) as ocd:
                            if halted_only or self.app.target_halted_at_bp:
                                raw_data = ocd.send(f"{cmd} {addr} {count}")
                            else:
                                state = ocd.send("[target current] curstate")
                                if "halted" in state:
                                    self.after(0, self.app.disable_all_auto_poll)
                                    raw_data = ocd.send(f"{cmd} {addr} {count}")
                                else:
                                    ocd.send("halt; wait_halt")
                                    raw_data = ocd.send(f"{cmd} {addr} {count}")
                                    ocd.send("resume")

                            if not self.app.abort_transfers:
                                self.after(0, lambda: self.process_dump(raw_data))
                    except Exception:
                        pass
            finally:
                self._busy = False
                if self.winfo_exists() and self.auto_poll.get() and not halted_only and not self.app.abort_transfers:
                    self.after(interval, self.execute_dump)

        threading.Thread(target=_dump_task, daemon=True).start()

    def process_dump(self, raw_text):
        if not self.winfo_exists():
            return
        values = []
        for line in raw_text.split("\n"):
            if ":" in line:
                parts = line.split(":")[1].strip().split()
                for p in parts:
                    try:
                        values.append(int(p, 16))
                    except ValueError:
                        pass

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
            self.ax.bar(range(len(values)), values, color="#E040FB", width=0.8)
        else:
            self.ax.plot(range(len(values)), values, color="#E040FB", linewidth=1.5, marker="o", markersize=3)

        self.ax.set_xlim(-0.5, max(1, len(values) - 0.5))

        if values:
            min_v, max_v = min(values), max(values)
            margin = (max_v - min_v) * 0.1 if max_v != min_v else max(1, max_v * 0.1)
            self.ax.set_ylim(min_v - margin, max_v + margin)

        self.canvas.draw_idle()

    def get_state(self):
        return {
            "widget_type": "plotter",
            "base_addr": self.base_addr.get(),
            "word_count": self.word_count.get(),
            "word_size": self.word_size.get(),
            "plot_type": self.plot_type.get(),
            "poll_interval": self.poll_interval.get(),
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
        if not fp:
            return
        self.filepath = fp
        self.file_data.clear()

        try:
            with open(fp, "r") as f:
                content = f.read()
            tokens = re.findall(r"[0-9a-fA-F]+", content)
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
        base_addr_hex = self.get_resolved_addr(name)
        if not base_addr_hex:
            self.app.log_msg("Loader Error: Invalid address or map symbol.")
            return

        base_addr_int = int(base_addr_hex, 16)
        ws = self.word_size.get()
        cmd = "mwb" if ws == "8" else ("mwh" if ws == "16" else "mww")
        byte_step = 1 if ws == "8" else (2 if ws == "16" else 4)

        self.status_label.configure(text="Uploading...", text_color="#FFA000")
        self.upload_btn.configure(state="disabled")

        def _upload_task():
            with self.app.jtag_lock:
                try:
                    with OpenOCDSession(timeout=5.0) as ocd:
                        state = ocd.send("[[target current] curstate]")
                        was_running = "running" in state
                        if was_running:
                            ocd.send("halt; wait_halt")

                        batch_size = 30
                        for i in range(0, len(self.file_data), batch_size):
                            batch = self.file_data[i : i + batch_size]
                            cmd_string = ""
                            for j, val in enumerate(batch):
                                addr = hex(base_addr_int + (i + j) * byte_step)
                                cmd_string += f"{cmd} {addr} {hex(val)}; "
                            ocd.send(cmd_string)

                        if was_running and not self.app.target_halted_at_bp:
                            ocd.send("resume")

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
        self.base_addr.set(state.get("base_addr", "0x60000000"))
        self.word_size.set(state.get("word_size", "32"))


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
        if not self.winfo_exists():
            return
        if not self.auto_poll.get() and not halted_only:
            self.app.abort_transfers = False

        if getattr(self, "_busy", False) and not halted_only:
            return

        name = self.var_name.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.set_error("Invalid Address")
            self.auto_poll.set(False)
            return

        try:
            interval = max(50, int(self.poll_interval.get()))
        except ValueError:
            interval = 500
            self.poll_interval.set("500")

        v_type = self.var_type.get()
        cmd = "mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw")
        self._busy = True

        def _read_task():
            try:
                with self.app.jtag_lock:
                    if self.app.abort_transfers:
                        return
                    try:
                        with OpenOCDSession(timeout=2.0) as ocd:
                            if halted_only or self.app.target_halted_at_bp:
                                output = ocd.send(f"{cmd} {addr}")
                            else:
                                state = ocd.send("[target current] curstate")
                                if "halted" in state:
                                    self.after(0, self.app.disable_all_auto_poll)
                                    output = ocd.send(f"{cmd} {addr}")
                                else:
                                    ocd.send("halt; wait_halt")
                                    output = ocd.send(f"{cmd} {addr}")
                                    ocd.send("resume")

                            if not self.app.abort_transfers:
                                self.after(0, lambda: self.process_read(output))
                    except Exception:
                        if not self.app.abort_transfers:
                            self.after(0, lambda: self.set_error("Comm Error"))
            finally:
                self._busy = False
                if self.winfo_exists() and self.auto_poll.get() and not halted_only and not self.app.abort_transfers:
                    self.after(interval, self.execute_read)

        threading.Thread(target=_read_task, daemon=True).start()

    def process_read(self, output):
        if not self.winfo_exists():
            return
        match = re.search(r":\s*([0-9a-fA-F]+)", output)
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
        return {
            "widget_type": "alive_light",
            "var_name": self.var_name.get(),
            "var_type": self.var_type.get(),
            "poll_interval": self.poll_interval.get(),
        }

    def set_state(self, state):
        self.var_name.set(state.get("var_name", "system_alive"))
        self.var_type.set(state.get("var_type", "uint8"))
        self.poll_interval.set(state.get("poll_interval", "500"))
        if state.get("auto_poll", False):
            self.auto_poll.set(True)
            self.toggle_poll()

class EnumSelectorWidget(BaseWidget):
    # --- DISCRETE ENUM / CHOICE SWITCHER WIDGET ---
    def __init__(self, parent, app):
        super().__init__(parent, corner_radius=10, border_width=2, border_color="#3F51B5")
        self.app = app
        self.pack(fill="x", pady=10, padx=10)
        
        self.var_name = ctk.StringVar(value="operating_mode")
        self.var_type = ctk.StringVar(value="uint32")
        
        # Default options: Label -> Value string
        self.choices_str = ctk.StringVar(value="IDLE:0, RUN:1, BOOST:2, FAULT:3")
        self.current_selection = ctk.StringVar(value="IDLE")
        
        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(10, 5))
        
        ctk.CTkLabel(config_frame, text="Choice | Addr/Name:").grid(row=0, column=0, padx=5)
        ctk.CTkEntry(config_frame, textvariable=self.var_name, width=130).grid(row=0, column=1, padx=5)
        
        ctk.CTkLabel(config_frame, text="Type:").grid(row=0, column=2, padx=5)
        ctk.CTkComboBox(
            config_frame, 
            variable=self.var_type, 
            values=["uint8", "uint16", "uint32", "int8", "int16", "int32", "float"], 
            width=85
        ).grid(row=0, column=3, padx=5)
        
        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=4, padx=5)

        # Choices configuration row
        choices_frame = ctk.CTkFrame(self, fg_color="transparent")
        choices_frame.pack(fill="x", padx=10, pady=(0, 5))
        
        ctk.CTkLabel(choices_frame, text="Values [Label:Val, ...]:").pack(side="left", padx=5)
        self.choices_entry = ctk.CTkEntry(choices_frame, textvariable=self.choices_str, width=320)
        self.choices_entry.pack(side="left", padx=5, fill="x", expand=True)
        ctk.CTkButton(choices_frame, text="Set Choices", width=90, command=self.rebuild_buttons).pack(side="left", padx=5)

        # Segmented button container
        self.btn_container = ctk.CTkFrame(self, fg_color="#1E1E1E", corner_radius=6)
        self.btn_container.pack(fill="x", padx=15, pady=(5, 12))
        
        self.parsed_choices = {}  # {"IDLE": 0, "RUN": 1, ...}
        self.segmented_btn = None
        self.rebuild_buttons()

    def rebuild_buttons(self):
        # Parse "Label:Value, Label2:Value2"
        raw = self.choices_str.get()
        self.parsed_choices.clear()
        
        pairs = raw.split(",")
        for pair in pairs:
            if ":" in pair:
                parts = pair.split(":")
                label = parts[0].strip()
                val_str = parts[1].strip()
                try:
                    # Support floats, hex (0x...), and ints
                    if "." in val_str or "f" in val_str.lower():
                        val = float(val_str.lower().replace("f", ""))
                    elif val_str.startswith("0x") or val_str.startswith("0X"):
                        val = int(val_str, 16)
                    else:
                        val = int(val_str)
                    self.parsed_choices[label] = val
                except ValueError:
                    pass

        if not self.parsed_choices:
            self.parsed_choices = {"DEFAULT": 0}

        # Clear old segmented button if it exists
        if self.segmented_btn:
            self.segmented_btn.destroy()

        labels = list(self.parsed_choices.keys())
        if self.current_selection.get() not in labels:
            self.current_selection.set(labels[0])

        self.segmented_btn = ctk.CTkSegmentedButton(
            self.btn_container,
            values=labels,
            variable=self.current_selection,
            command=self.on_choice_selected
        )
        self.segmented_btn.pack(fill="x", padx=10, pady=8)

    def on_choice_selected(self, label):
        if not self.winfo_exists():
            return
        val = self.parsed_choices.get(label)
        if val is not None:
            self.execute_write(val)

    def execute_write(self, value):
        if not self.winfo_exists():
            return
        name = self.var_name.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.app.log_msg(f"Wait: '{name}' is not a valid map symbol or hex address!")
            return

        v_type = self.var_type.get()
        
        # Handle Float formatting (IEEE-754 hex conversion)
        if v_type == "float":
            try:
                packed = struct.pack('<f', float(value))
                int_val = struct.unpack('<I', packed)[0]
                hex_val = f"0x{int_val:08X}"
                mem_cmd = "mww"
            except Exception as e:
                self.app.log_msg(f"Float Write ERROR: {e}")
                return
        else:
            # Handle signed/unsigned integers
            if isinstance(value, float):
                value = int(value)
            if value < 0:
                bits = 8 if "8" in v_type else (16 if "16" in v_type else 32)
                value = (1 << bits) + value

            cmd_map = {"8": "mwb", "16": "mwh", "32": "mww"}
            mem_cmd = cmd_map.get(v_type.replace("uint", "").replace("int", ""), "mww")
            hex_val = hex(value)

        def _write_task():
            with self.app.jtag_lock:
                try:
                    with OpenOCDSession(timeout=2.0) as ocd:
                        state = ocd.send("[[target current] curstate]")
                        was_running = "running" in state
                        if was_running:
                            ocd.send("halt; wait_halt")
                        ocd.send(f"{mem_cmd} {addr} {hex_val}")
                        if was_running and not self.app.target_halted_at_bp:
                            ocd.send("resume")
                        self.after(0, lambda: self.app.log_msg(f"Enum Write: set '{name}' to {self.current_selection.get()} ({hex_val})"))
                except Exception as e:
                    self.after(0, lambda: self.app.log_msg(f"Enum Write ERROR: {str(e)}"))

        threading.Thread(target=_write_task, daemon=True).start()

    def get_state(self):
        return {
            "widget_type": "enum_selector",
            "var_name": self.var_name.get(),
            "var_type": self.var_type.get(),
            "choices_str": self.choices_str.get(),
            "current_selection": self.current_selection.get()
        }

    def set_state(self, state):
        self.var_name.set(state.get("var_name", "operating_mode"))
        self.var_type.set(state.get("var_type", "uint32"))
        self.choices_str.set(state.get("choices_str", "IDLE:0, RUN:1, BOOST:2, FAULT:3"))
        self.rebuild_buttons()
        sel = state.get("current_selection")
        if sel in self.parsed_choices:
            self.current_selection.set(sel)

    def toggle_poll(self):
        if self.auto_poll.get():
            self.execute_read()
        else:
            self.light.configure(fg_color="#424242")
            self.status_lbl.configure(text="Stopped", text_color="gray")

    def execute_read(self, halted_only=False):
        if not self.winfo_exists():
            return

        name = self.var_name.get()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.set_error("Invalid Address")
            self.auto_poll.set(False)
            return

        try:
            interval = max(50, int(self.poll_interval.get()))
        except ValueError:
            interval = 500
            self.poll_interval.set("500")

        v_type = self.var_type.get()
        cmd = "mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw")

        def _read_task():
            with self.app.jtag_lock:
                try:
                    with OpenOCDSession(timeout=2.0) as ocd:
                        if halted_only or self.app.target_halted_at_bp:
                            output = ocd.send(f"{cmd} {addr}")
                        else:
                            state = ocd.send("[[target current] curstate]")
                            if "halted" in state:
                                self.after(0, self.app.disable_all_auto_poll)
                                output = ocd.send(f"{cmd} {addr}")
                            else:
                                ocd.send("halt; wait_halt")
                                output = ocd.send(f"{cmd} {addr}")
                                ocd.send("resume")

                        self.after(0, lambda: self.process_read(output))
                except Exception:
                    self.after(0, lambda: self.set_error("Comm Error"))

        threading.Thread(target=_read_task, daemon=True).start()

        if self.auto_poll.get() and not halted_only:
            self.after(interval, self.execute_read)

    def process_read(self, output):
        if not self.winfo_exists():
            return
        match = re.search(r":\s*([0-9a-fA-F]+)", output)
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
        return {
            "widget_type": "alive_light",
            "var_name": self.var_name.get(),
            "var_type": self.var_type.get(),
            "poll_interval": self.poll_interval.get(),
        }

    def set_state(self, state):
        self.var_name.set(state.get("var_name", "system_alive"))
        self.var_type.set(state.get("var_type", "uint8"))
        self.poll_interval.set(state.get("poll_interval", "500"))
        if state.get("auto_poll", False):
            self.auto_poll.set(True)
            self.toggle_poll()
