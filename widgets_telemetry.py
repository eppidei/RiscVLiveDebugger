# widgets_telemetry.py
import re
import struct
import threading
import tkinter as tk
from collections import deque
import customtkinter as ctk
from tkinter import filedialog

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from ocd_backend import OpenOCDSession

def decode_ocd_word(raw_u32: int, v_type: str):
    """Converts a raw unsigned hex integer from OpenOCD into a typed Python int or float."""
    if v_type == "float":
        packed = struct.pack("<I", raw_u32 & 0xFFFFFFFF)
        return struct.unpack("<f", packed)[0]

    bits = 8 if "8" in v_type else (16 if "16" in v_type else 32)
    mask = (1 << bits) - 1
    val = raw_u32 & mask
    if v_type.startswith("int") and (val & (1 << (bits - 1))):
        val -= 1 << bits
    return val

class BaseWidget(ctk.CTkFrame):
    GRID_SNAP = 20  # Snap position and size to 20px grid

    DEFAULT_SIZES = {
        "AliveLightWidget": (560, 135),
        "VarSlider": (620, 155),
        "EnumSelectorWidget": (600, 185),
        "MemFileLoader": (580, 165),
        "ReadMonitor": (640, 145),
        "MemBlockPlotter": (640, 400),
        "BreakOnFuncWidget": (900, 320),
        "BreakOnLineWidget": (900, 320),
        "BreakOnVarWidget": (900, 320),
    }

    # Natural minimum content (width, height) below which scrollbars appear
    MIN_CONTENT_SIZES = {
        "AliveLightWidget": (530, 105),
        "VarSlider": (590, 125),
        "EnumSelectorWidget": (560, 155),
        "MemFileLoader": (550, 130),
        "ReadMonitor": (610, 115),
        "MemBlockPlotter": (600, 340),
        "BreakOnFuncWidget": (820, 260),
        "BreakOnLineWidget": (820, 260),
        "BreakOnVarWidget": (820, 260),
    }

    def __init__(self, parent, **kwargs):
        cls_name = self.__class__.__name__
        def_w, def_h = self.DEFAULT_SIZES.get(cls_name, (580, 150))
        kwargs.setdefault("width", def_w)
        kwargs.setdefault("height", def_h)
        super().__init__(parent, **kwargs)

        self._tile_w = int(kwargs["width"])
        self._tile_h = int(kwargs["height"])
        self._tile_x = 20
        self._tile_y = 20
        self._tile_placed = False
        self._updating_scrollbars = False
        self._adopted_children = set()
        self._last_win_size = (0, 0)
        self._last_scroll_region = (0, 0, 0, 0)

        self._drag_start_x = 0
        self._drag_start_y = 0
        self._resize_start_x = 0
        self._resize_start_y = 0
        self._resize_start_w = self._tile_w
        self._resize_start_h = self._tile_h

        self._build_scroll_viewport()

    def _get_min_content_size(self):
        """Returns the natural content (min_w, min_h) so scrollbars trigger reliably when shrunk."""
        cls_name = self.__class__.__name__
        min_w, min_h = self.MIN_CONTENT_SIZES.get(cls_name, (550, 130))
        if cls_name == "ReadMonitor":
            if hasattr(self, "show_bitfields") and self.show_bitfields.get():
                min_h += 140
            if hasattr(self, "show_plot") and self.show_plot.get():
                min_h += 230
        return min_w, min_h

    def _build_scroll_viewport(self):
        """Creates a viewport with auto-appearing horizontal & vertical sliding bars."""
        bg_col = self._apply_appearance_mode(self.cget("fg_color"))
        if not bg_col or bg_col == "transparent":
            bg_col = "#2b2b2b"

        self._vp_container = tk.Frame(self, bg=bg_col, highlightthickness=0, bd=0)
        self._vp_container.place(x=4, y=22, relwidth=1.0, relheight=1.0, width=-8, height=-26)

        self._vp_canvas = tk.Canvas(
            self._vp_container,
            bg=bg_col,
            highlightthickness=0,
            bd=0,
        )
        self._h_scroll = ctk.CTkScrollbar(
            self._vp_container,
            orientation="horizontal",
            height=12,
            command=self._vp_canvas.xview,
        )
        self._v_scroll = ctk.CTkScrollbar(
            self._vp_container,
            orientation="vertical",
            width=12,
            command=self._vp_canvas.yview,
        )
        self._vp_canvas.configure(
            xscrollcommand=self._h_scroll.set,
            yscrollcommand=self._v_scroll.set,
        )

        self._vp_canvas.pack(side="left", fill="both", expand=True)

        self._inner_frame = tk.Frame(self._vp_canvas, bg=bg_col, highlightthickness=0, bd=0)
        self._vp_win_id = self._vp_canvas.create_window((0, 0), window=self._inner_frame, anchor="nw")

        self._vp_container.bind("<Configure>", self._on_viewport_configure)

        for target in (self._vp_canvas, self._inner_frame):
            target.bind("<MouseWheel>", self._on_tile_mousewheel)

        # Right-click context menu for per-widget sync and deletion
        self._ctx_menu = tk.Menu(self, tearoff=0)
        if hasattr(self, "get_sync_requests"):
            self._ctx_menu.add_command(
                label="Sync from Target",
                command=lambda: self.app.sync_write_widgets([self]),
            )
            self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Delete Widget", command=self.destroy)
        for target in (self, self._vp_canvas, self._inner_frame):
            target.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event):
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    def _on_tile_mousewheel(self, event):
        if self._v_scroll.winfo_manager() and self.winfo_exists():
            self._vp_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

    def _adopt_children_into_viewport(self):
        """Moves subclass UI elements into _inner_frame so overflow gets sliding bars."""
        if not self.winfo_exists():
            return

        reserved = {self._vp_container}
        if hasattr(self, "drag_handle"):
            reserved.add(self.drag_handle)
        if hasattr(self, "resize_grip"):
            reserved.add(self.resize_grip)

        moved_any = False
        for child in list(self.winfo_children()):
            if child in reserved or child in self._adopted_children:
                continue
            self._adopted_children.add(child)
            mgr = child.winfo_manager()
            if mgr == "pack":
                child.pack_configure(in_=self._inner_frame)
                moved_any = True
            elif mgr == "grid":
                child.grid_configure(in_=self._inner_frame)
                moved_any = True

        if moved_any:
            self._on_viewport_configure()
            self._lift_handles()

    def _on_viewport_configure(self, event=None):
        """Updates inner frame size and scrollbar visibility without feedback loops."""
        if self._updating_scrollbars or not self.winfo_exists():
            return
        self._updating_scrollbars = True
        try:
            cont_w = self._vp_container.winfo_width()
            cont_h = self._vp_container.winfo_height()
            if cont_w <= 10 or cont_h <= 10:
                return

            min_w, min_h = self._get_min_content_size()

            # Compute scrollbar requirements from outer container dimensions (immune to thrashing)
            need_h = min_w > cont_w
            need_v = min_h > cont_h
            avail_w = cont_w - (12 if need_v else 0)
            avail_h = cont_h - (12 if need_h else 0)
            need_h = min_w > avail_w
            need_v = min_h > avail_h
            avail_w = max(1, cont_w - (12 if need_v else 0))
            avail_h = max(1, cont_h - (12 if need_h else 0))

            if need_h and not self._h_scroll.winfo_manager():
                self._h_scroll.pack(side="bottom", fill="x", before=self._vp_canvas)
            elif not need_h and self._h_scroll.winfo_manager():
                self._h_scroll.pack_forget()
                self._vp_canvas.xview_moveto(0.0)

            if need_v and not self._v_scroll.winfo_manager():
                self._v_scroll.pack(side="right", fill="y", before=self._vp_canvas)
            elif not need_v and self._v_scroll.winfo_manager():
                self._v_scroll.pack_forget()
                self._vp_canvas.yview_moveto(0.0)

            target_w = max(min_w, avail_w)
            target_h = max(min_h, avail_h)

            if (target_w, target_h) != self._last_win_size:
                self._last_win_size = (target_w, target_h)
                self._vp_canvas.itemconfigure(self._vp_win_id, width=target_w, height=target_h)

            sr = (0, 0, target_w, target_h)
            if sr != self._last_scroll_region:
                self._last_scroll_region = sr
                self._vp_canvas.configure(scrollregion=sr)
        finally:
            self._updating_scrollbars = False

    def pack(self, *args, **kwargs):
        """Places this BaseWidget tile on the parent workspace and schedules child adoption."""
        if not self._tile_placed:
            self._tile_placed = True
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
            self.after(15, self._adopt_children_into_viewport)
            return

        super().pack(*args, **kwargs)
        self.after(15, self._adopt_children_into_viewport)

    def _install_drag_and_resize_handles(self):
        self.drag_handle = ctk.CTkLabel(
            self,
            text="⠿",
            font=("Consolas", 16, "bold"),
            text_color="#888888",
            width=18,
            height=18,
            cursor="fleur",
        )
        self.drag_handle.place(x=3, y=2)

        for target in (self.drag_handle, self):
            target.bind("<ButtonPress-1>", self._on_drag_start)
            target.bind("<B1-Motion>", self._on_drag_motion)

        self.resize_grip = ctk.CTkLabel(
            self,
            text="◢",
            font=("Consolas", 13),
            text_color="#888888",
            width=16,
            height=16,
            cursor="size_nw_se",
        )
        self.resize_grip.place(relx=1.0, rely=1.0, x=-2, y=-2, anchor="se")
        self.resize_grip.bind("<ButtonPress-1>", self._on_resize_start)
        self.resize_grip.bind("<B1-Motion>", self._on_resize_motion)

        self.after(25, self._lift_handles)

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
        new_w = max(200, round((self._resize_start_w + dx) / self.GRID_SNAP) * self.GRID_SNAP)
        new_h = max(80, round((self._resize_start_h + dy) / self.GRID_SNAP) * self.GRID_SNAP)
        if new_w != self._tile_w or new_h != self._tile_h:
            self._tile_w = int(new_w)
            self._tile_h = int(new_h)
            self.configure(width=self._tile_w, height=self._tile_h)
            self._lift_handles()
            self._on_viewport_configure()
            self._update_canvas_height()

    def set_tile_size(self, w=None, h=None):
        if w is not None:
            self._tile_w = int(w)
        if h is not None:
            self._tile_h = int(h)
        self.configure(width=self._tile_w, height=self._tile_h)
        self._lift_handles()
        self._on_viewport_configure()
        self._update_canvas_height()

    def _update_canvas_height(self):
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
        return {
            "x": int(self._tile_x),
            "y": int(self._tile_y),
            "w": int(self._tile_w),
            "h": int(self._tile_h),
        }

    def set_geometry_state(self, state):
        if "x" in state and "y" in state:
            self._tile_x = int(state["x"])
            self._tile_y = int(state["y"])
            self.place(x=self._tile_x, y=self._tile_y)
        if "w" in state and "h" in state:
            self._tile_w = int(state["w"])
            self._tile_h = int(state["h"])
            self.configure(width=self._tile_w, height=self._tile_h)
        self._lift_handles()
        self._update_canvas_height()
        self.after(20, self._on_viewport_configure)

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
        config_frame.pack(fill="x", padx=10, pady=(4, 4))

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
        self.current_val_label.pack(pady=(2, 0))

        self.slider = ctk.CTkSlider(self, from_=0, to=255)
        self.slider.pack(fill="x", padx=15, pady=(4, 10))
        self.slider.configure(command=self.on_slider_change)
        self.slider.set(0)

    def update_config(self):
        try:
            min_v = int(self.min_val.get())
            max_v = int(self.max_val.get())
            if min_v >= max_v:
                max_v = min_v + 1
                self.max_val.set(str(max_v))
            self.slider.configure(from_=min_v, to=max_v)
            self.current_val_label.configure(text=f"Target Value: {int(self.slider.get())}")
        except ValueError:
            self.app.log_msg("ERROR: Min and Max must be valid integers.")

    def on_slider_change(self, val):
        if getattr(self, "_syncing", False) or not self.winfo_exists():
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
                if getattr(self.app, "abort_transfers", False):
                    return
                try:
                    with OpenOCDSession(timeout=2.0) as ocd:
                        state = ocd.send("[target current] curstate")
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

    def get_sync_requests(self):
        name = self.var_name.get().strip()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.app.log_msg(f"Sync Skip: '{name}' is not a valid map symbol or hex address.")
            return []
        v_type = self.var_type.get()
        mem_cmd = "mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw")
        return [(addr, mem_cmd)]

    def apply_sync_results(self, raw_map: dict):
        name = self.var_name.get().strip()
        addr = self.get_resolved_addr(name)
        v_type = self.var_type.get()
        mem_cmd = "mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw")
        raw_u32 = raw_map.get((addr, mem_cmd))
        if raw_u32 is None or not self.winfo_exists():
            return

        val = int(decode_ocd_word(raw_u32, v_type))
        if self.slider_timer:
            self.after_cancel(self.slider_timer)
            self.slider_timer = None

        try:
            min_v = int(self.min_val.get())
        except ValueError:
            min_v = 0
        try:
            max_v = int(self.max_val.get())
        except ValueError:
            max_v = 255

        bounds_changed = False
        if val < min_v:
            min_v = val
            self.min_val.set(str(min_v))
            bounds_changed = True
        if val > max_v:
            max_v = val
            self.max_val.set(str(max_v))
            bounds_changed = True
        if min_v >= max_v:
            max_v = min_v + 1
            self.max_val.set(str(max_v))
            bounds_changed = True

        self._syncing = True
        try:
            self.slider.configure(from_=min_v, to=max_v)
            self.slider.set(val)
            self.current_val_label.configure(text=f"Target Value: {val}")
        finally:
            self._syncing = False

        if bounds_changed:
            self.app.log_msg(
                f"Sync Note: Expanded '{name}' slider range to [{min_v}, {max_v}] to fit target value {val}."
            )

    def get_state(self):
        return {
            "widget_type": "write",
            "var_name": self.var_name.get(),
            "var_type": self.var_type.get(),
            "min_val": self.min_val.get(),
            "max_val": self.max_val.get(),
            "slider_val": int(self.slider.get()),
        }

    def set_state(self, state):
        self.var_name.set(state.get("var_name", "motor_speed"))
        self.var_type.set(state.get("var_type", "uint32"))
        self.min_val.set(state.get("min_val", "0"))
        self.max_val.set(state.get("max_val", "255"))
        self.update_config()
        if "slider_val" in state:
            try:
                s_val = int(state["slider_val"])
                self.slider.set(s_val)
                self.current_val_label.configure(text=f"Target Value: {s_val}")
            except (ValueError, TypeError):
                pass


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
        config_frame.pack(fill="x", padx=10, pady=(4, 4))

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
        choices_frame.pack(fill="x", padx=10, pady=(0, 4))

        ctk.CTkLabel(choices_frame, text="Values [Label:Val, ...]:").pack(side="left", padx=5)
        self.choices_entry = ctk.CTkEntry(choices_frame, textvariable=self.choices_str, width=260)
        self.choices_entry.pack(side="left", padx=5, fill="x", expand=True)
        ctk.CTkButton(choices_frame, text="Set Choices", width=95, command=self.rebuild_buttons).pack(side="left", padx=5)

        self.btn_container = ctk.CTkFrame(self, fg_color="#1E1E1E", corner_radius=6)
        self.btn_container.pack(fill="x", padx=15, pady=(4, 10))

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
        self.after(20, self._on_viewport_configure)

    def on_choice_selected(self, label):
        if getattr(self, "_syncing", False) or not self.winfo_exists():
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
                if getattr(self.app, "abort_transfers", False):
                    return
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

    def get_sync_requests(self):
        name = self.var_name.get().strip()
        addr = self.get_resolved_addr(name)
        if not addr:
            self.app.log_msg(f"Sync Skip: '{name}' is not a valid map symbol or hex address.")
            return []
        v_type = self.var_type.get()
        mem_cmd = "mdw" if v_type == "float" else ("mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw"))
        return [(addr, mem_cmd)]

    def apply_sync_results(self, raw_map: dict):
        name = self.var_name.get().strip()
        addr = self.get_resolved_addr(name)
        v_type = self.var_type.get()
        mem_cmd = "mdw" if v_type == "float" else ("mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw"))
        raw_u32 = raw_map.get((addr, mem_cmd))
        if raw_u32 is None or not self.winfo_exists():
            return

        val = decode_ocd_word(raw_u32, v_type)
        matched_label = None
        for lbl, c_val in self.parsed_choices.items():
            if v_type == "float":
                if abs(float(val) - float(c_val)) <= max(1e-6, 1e-5 * abs(float(c_val))):
                    matched_label = lbl
                    break
            else:
                if int(val) == int(c_val):
                    matched_label = lbl
                    break

        self._syncing = True
        try:
            if matched_label is not None:
                self.current_selection.set(matched_label)
            else:
                self.current_selection.set("")
                disp = f"{val:.4g}" if v_type == "float" else str(val)
                self.app.log_msg(
                    f"Sync Warning: '{name}' = {disp} on target does not match any choice in [{self.choices_str.get()}]."
                )
        finally:
            self._syncing = False
            
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
        self._busy = False

        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(4, 4))

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
        display_frame.pack(fill="x", padx=15, pady=(4, 8))

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

        self.fig = Figure(figsize=(5.5, 2.0), dpi=100)
        self.fig.patch.set_facecolor("#242424")
        self.fig.subplots_adjust(left=0.09, right=0.98, top=0.9, bottom=0.2)

        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#1E1E1E")
        self.ax.tick_params(colors="white", labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color("#555555")

        (self.line,) = self.ax.plot([], [], color="#00a8ff", linewidth=1.5)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _apply_optional_panes(self):
        """Packs or unpacks the bitfield and plot frames inside _inner_frame without resetting custom tile sizes."""
        self._adopt_children_into_viewport()
        if self.show_bitfields.get():
            self.bf_frame.pack(fill="x", padx=15, pady=(0, 10), in_=self._inner_frame)
            self._adopted_children.add(self.bf_frame)
        else:
            self.bf_frame.pack_forget()

        if self.show_plot.get():
            self.plot_frame.pack(fill="both", expand=True, padx=15, pady=(0, 12), in_=self._inner_frame)
            self._adopted_children.add(self.plot_frame)
            self.update_plot_visuals()
        else:
            self.plot_frame.pack_forget()

        self._on_viewport_configure()

    def _recalc_read_tile_height(self):
        h = 145
        if self.show_bitfields.get():
            h += 145
        if self.show_plot.get():
            h += 245
        self.set_tile_size(h=h)

    def toggle_bitfields(self):
        self._apply_optional_panes()
        self._recalc_read_tile_height()
        if self.show_bitfields.get():
            self.execute_read()

    def toggle_plot(self):
        self._apply_optional_panes()
        self._recalc_read_tile_height()

    def clear_plot(self):
        self.y_data.clear()
        self.update_plot_visuals()

    def toggle_poll(self):
        if self.auto_poll.get():
            self.execute_read()

    def execute_read(self, halted_only=False):
        if not self.winfo_exists():
            return
        if not self.auto_poll.get() and not halted_only:
            self.app.abort_transfers = False

        if self._busy and not halted_only:
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
                    if getattr(self.app, "abort_transfers", False):
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

                            if not getattr(self.app, "abort_transfers", False):
                                self.after(0, lambda: self.process_read_result(read_output))
                    except Exception:
                        pass
            finally:
                self._busy = False
                if (
                    self.winfo_exists()
                    and self.auto_poll.get()
                    and not halted_only
                    and not getattr(self.app, "abort_transfers", False)
                ):
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

        self.show_bitfields.set(bool(state.get("show_bitfields", False)))
        self.show_plot.set(bool(state.get("show_plot", False)))
        self.plot_buffer_size.set(state.get("plot_buffer_size", "100"))

        self.auto_scale.set(bool(state.get("auto_scale", True)))
        self.y_min.set(state.get("y_min", "-10"))
        self.y_max.set(state.get("y_max", "100"))

        saved_bfs = state.get("bitfields", [])
        for i, bf_state in enumerate(saved_bfs):
            if i < len(self.bitfield_rows):
                self.bitfield_rows[i]["name"].set(bf_state.get("name", ""))
                self.bitfield_rows[i]["range"].set(bf_state.get("range", ""))

        # Apply pane visibility without overwriting custom saved tile height (w, h)
        self._apply_optional_panes()
        if "h" not in state and (self.show_bitfields.get() or self.show_plot.get()):
            self._recalc_read_tile_height()

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
        self._busy = False

        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(4, 4))

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

        self.fig = Figure(figsize=(5.5, 2.6), dpi=100)
        self.fig.patch.set_facecolor("#242424")
        self.fig.subplots_adjust(left=0.09, right=0.98, top=0.9, bottom=0.15)
        self.ax = self.fig.add_subplot(111)
        self.setup_axes()

        self.last_values = []
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(4, 8))

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

        if self._busy and not halted_only:
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
                    if getattr(self.app, "abort_transfers", False):
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

                            if not getattr(self.app, "abort_transfers", False):
                                self.after(0, lambda: self.process_dump(raw_data))
                    except Exception:
                        pass
            finally:
                self._busy = False
                if (
                    self.winfo_exists()
                    and self.auto_poll.get()
                    and not halted_only
                    and not getattr(self.app, "abort_transfers", False)
                ):
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
        config_frame.pack(fill="x", padx=10, pady=(4, 4))

        ctk.CTkLabel(config_frame, text="File Loader | Addr/Name:").grid(row=0, column=0, padx=5)
        ctk.CTkEntry(config_frame, textvariable=self.base_addr, width=130).grid(row=0, column=1, padx=5)

        ctk.CTkLabel(config_frame, text="Size:").grid(row=0, column=2, padx=5)
        ctk.CTkComboBox(config_frame, variable=self.word_size, values=["8", "16", "32"], width=70).grid(row=0, column=3, padx=5)

        ctk.CTkButton(config_frame, text="Select HEX File", width=120, command=self.load_file).grid(row=0, column=4, padx=15)
        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=5, padx=5)

        self.status_label = ctk.CTkLabel(self, text="No file selected.", text_color="gray", font=("Consolas", 12))
        self.status_label.pack(pady=4)

        self.upload_btn = ctk.CTkButton(self, text="Upload to Target", fg_color="#F57C00", hover_color="#E65100", command=self.execute_upload, state="disabled")
        self.upload_btn.pack(pady=(0, 10))

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
                if getattr(self.app, "abort_transfers", False):
                    self.after(0, lambda: self.upload_btn.configure(state="normal"))
                    return
                try:
                    with OpenOCDSession(timeout=5.0) as ocd:
                        state = ocd.send("[target current] curstate")
                        was_running = "running" in state
                        if was_running:
                            ocd.send("halt; wait_halt")

                        batch_size = 30
                        for i in range(0, len(self.file_data), batch_size):
                            if getattr(self.app, "abort_transfers", False):
                                break
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
        self._busy = False

        config_frame = ctk.CTkFrame(self, fg_color="transparent")
        config_frame.pack(fill="x", padx=10, pady=(4, 4))

        ctk.CTkLabel(config_frame, text="Alive Light | Addr:").grid(row=0, column=0, padx=(5, 2))
        ctk.CTkEntry(config_frame, textvariable=self.var_name, width=130).grid(row=0, column=1, padx=2)

        ctk.CTkLabel(config_frame, text="Type:").grid(row=0, column=2, padx=(10, 2))
        ctk.CTkComboBox(config_frame, variable=self.var_type, values=["uint8", "uint16", "uint32"], width=85).grid(row=0, column=3, padx=2)

        ctk.CTkButton(config_frame, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy).grid(row=0, column=4, padx=(15, 5))

        display_frame = ctk.CTkFrame(self, fg_color="transparent")
        display_frame.pack(fill="x", padx=15, pady=(4, 8))

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

        if self._busy and not halted_only:
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
                    if getattr(self.app, "abort_transfers", False):
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

                            if not getattr(self.app, "abort_transfers", False):
                                self.after(0, lambda: self.process_read(output))
                    except Exception:
                        if not getattr(self.app, "abort_transfers", False):
                            self.after(0, lambda: self.set_error("Comm Error"))
            finally:
                self._busy = False
                if (
                    self.winfo_exists()
                    and self.auto_poll.get()
                    and not halted_only
                    and not getattr(self.app, "abort_transfers", False)
                ):
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