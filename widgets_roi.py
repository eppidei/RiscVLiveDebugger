# widgets_roi.py
import ast
import operator
import struct
import threading
import tkinter as tk
import customtkinter as ctk

from ocd_backend import OpenOCDSession
from widgets_telemetry import BaseWidget, decode_ocd_word


_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def eval_roi_formula(expr_str: str, sym_vars: dict, relax_floor_div: bool = False) -> float:
    """Safely evaluates linear combinations and arithmetic formulas of ROI vertex coordinates."""
    cleaned = expr_str.strip()
    if not cleaned:
        raise ValueError("Empty formula")

    tree = ast.parse(cleaned, mode="eval")
    norm_vars = {k.upper(): v for k, v in sym_vars.items()}

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            key = node.id.upper()
            if key in norm_vars:
                return norm_vars[key]
            raise ValueError(f"Unknown token '{node.id}'")
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            op_fn = operator.truediv if (relax_floor_div and isinstance(node.op, ast.FloorDiv)) else _ALLOWED_BINOPS[type(node.op)]
            return op_fn(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
            return _ALLOWED_UNARYOPS[type(node.op)](_eval(node.operand))
        raise ValueError("Unsupported operator")

    return float(_eval(tree))


class RoiCropWidget(BaseWidget):
    """Interactive 2D Rectangular ROI / FPGA Cropping IP Controller."""

    HANDLE_RADIUS = 7

    def __init__(self, parent, app):
        super().__init__(
            parent,
            width=860,
            height=390,
            corner_radius=10,
            border_width=2,
            border_color="#00ACC1",
        )
        self.app = app
        self.pack(fill="x", pady=10, padx=10)

        # Virtual frame dimensions & inclusive ROI bounds (0 .. fw-1, 0 .. fh-1)
        self.fw = 1920
        self.fh = 1080
        self.x0 = 0
        self.y0 = 0
        self.x1 = 1919
        self.y1 = 1079

        self.frame_w_var = ctk.StringVar(value="1920")
        self.frame_h_var = ctk.StringVar(value="1080")
        self.live_write = ctk.BooleanVar(value=True)

        self.entry_ul_x = ctk.StringVar(value="0")
        self.entry_ul_y = ctk.StringVar(value="0")
        self.entry_lr_x = ctk.StringVar(value="1919")
        self.entry_lr_y = ctk.StringVar(value="1079")

        # Canvas viewport geometry cache & mouse drag state
        self._frame_rect = (16, 16, 300, 180)  # (left, top, right, bottom) in canvas pixels
        self._drag_mode = None
        self._drag_start_virt = (0, 0)
        self._drag_orig_roi = (0, 0, 0, 0)

        # Debounce & thread-coalescing state
        self._debounce_timer = None
        self._write_busy = False
        self._write_pending = False

        self._build_roi_ui()
        self._sync_coord_entries()
        self._evaluate_all_rows()

    def _get_min_content_size(self):
        """Triggers BaseWidget auto-scrollbars when tile is resized smaller than 810x340."""
        return (810, 340)

    def _build_roi_ui(self):
        # --- TOP CONFIGURATION BAR ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(4, 4))

        ctk.CTkLabel(header, text="ROI Cropper | Frame W:", font=("Consolas", 12, "bold")).pack(side="left", padx=(4, 4))
        w_ent = ctk.CTkEntry(header, textvariable=self.frame_w_var, width=62)
        w_ent.pack(side="left", padx=2)
        w_ent.bind("<Return>", lambda e: self.apply_frame_size())

        ctk.CTkLabel(header, text="H:").pack(side="left", padx=(6, 2))
        h_ent = ctk.CTkEntry(header, textvariable=self.frame_h_var, width=62)
        h_ent.pack(side="left", padx=2)
        h_ent.bind("<Return>", lambda e: self.apply_frame_size())

        ctk.CTkButton(header, text="Apply Frame", width=88, command=self.apply_frame_size).pack(side="left", padx=(8, 6))
        ctk.CTkButton(
            header, text="Full Frame", width=80, fg_color="#37474F", hover_color="#263238", command=self.reset_full_roi
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            header, text="X", width=30, fg_color="#C62828", hover_color="#b71c1c", command=self.destroy
        ).pack(side="right", padx=(8, 4))
        ctk.CTkButton(
            header, text="Write Now", width=82, fg_color="#00838F", hover_color="#006064",
            command=lambda: self.trigger_write(immediate=True),
        ).pack(side="right", padx=6)
        ctk.CTkSwitch(header, text="Live Write", variable=self.live_write).pack(side="right", padx=8)

        # --- MAIN SPLIT BODY (LEFT: 2D CANVAS, RIGHT: 4 LINEAR COMBINATIONS) ---
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=(2, 8))

        # Left Pane: Interactive Visual Frame
        left_pane = ctk.CTkFrame(body, fg_color="#1E1E1E", corner_radius=8)
        left_pane.pack(side="left", fill="both", expand=True, padx=(0, 8))

        self.roi_canvas = tk.Canvas(
            left_pane,
            width=330,
            height=210,
            bg="#121518",
            highlightthickness=1,
            highlightbackground="#333333",
        )
        self.roi_canvas.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.roi_canvas.bind("<Configure>", lambda e: self._redraw_canvas())
        self.roi_canvas.bind("<Motion>", self._on_canvas_hover)
        self.roi_canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.roi_canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.roi_canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        # Exact coordinate bar underneath canvas
        coord_bar = ctk.CTkFrame(left_pane, fg_color="transparent")
        coord_bar.pack(fill="x", padx=8, pady=(0, 2))

        for lbl_txt, var in (
            ("UL_X:", self.entry_ul_x),
            ("UL_Y:", self.entry_ul_y),
            ("LR_X:", self.entry_lr_x),
            ("LR_Y:", self.entry_lr_y),
        ):
            ctk.CTkLabel(coord_bar, text=lbl_txt, font=("Consolas", 11)).pack(side="left", padx=(2, 1))
            ent = ctk.CTkEntry(coord_bar, textvariable=var, width=48, height=24, font=("Consolas", 11))
            ent.pack(side="left", padx=(0, 4))
            ent.bind("<Return>", lambda e: self.apply_manual_coords())

        ctk.CTkButton(coord_bar, text="Set", width=38, height=24, command=self.apply_manual_coords).pack(side="left", padx=2)

        self.vertex_summary_lbl = ctk.CTkLabel(
            left_pane,
            text="",
            font=("Consolas", 11),
            text_color="#80DEEA",
            anchor="w",
        )
        self.vertex_summary_lbl.pack(fill="x", padx=10, pady=(0, 6))

        # Right Pane: 4 Target Variables & Linear Combination Formulas
        right_pane = ctk.CTkFrame(body, fg_color="#1E1E1E", corner_radius=8)
        right_pane.pack(side="right", fill="y")

        legend_lbl = ctk.CTkLabel(
            right_pane,
            text="Vertices: UL_X, UL_Y | UR_X, UR_Y | LL_X, LL_Y | LR_X, LR_Y | FW, FH",
            font=("Consolas", 11, "bold"),
            text_color="#FFB74D",
        )
        legend_lbl.pack(fill="x", padx=10, pady=(8, 4))

        hdr_row = ctk.CTkFrame(right_pane, fg_color="transparent")
        hdr_row.pack(fill="x", padx=8, pady=(0, 2))
        ctk.CTkLabel(hdr_row, text="On", width=26, font=("Consolas", 11, "bold")).grid(row=0, column=0, padx=2)
        ctk.CTkLabel(hdr_row, text="Map Var / Hex Addr", width=120, anchor="w", font=("Consolas", 11, "bold")).grid(row=0, column=1, padx=4)
        ctk.CTkLabel(hdr_row, text="Linear Combination Formula", width=155, anchor="w", font=("Consolas", 11, "bold")).grid(row=0, column=3, padx=4)
        ctk.CTkLabel(hdr_row, text="Type", width=72, font=("Consolas", 11, "bold")).grid(row=0, column=4, padx=3)
        ctk.CTkLabel(hdr_row, text="Value", width=68, anchor="e", font=("Consolas", 11, "bold")).grid(row=0, column=5, padx=4)

        default_rows = [
            (True, "crop_anchor_x", "UL_X", "uint32"),
            (True, "crop_anchor_y", "UL_Y", "uint32"),
            (True, "crop_win_len_x", "UR_X - UL_X + 1", "uint32"),
            (True, "crop_win_len_y", "LL_Y - UL_Y + 1", "uint32"),
        ]

        self.mapping_rows = []
        for idx, (en_def, sym_def, expr_def, type_def) in enumerate(default_rows):
            rf = ctk.CTkFrame(right_pane, fg_color="#262626", corner_radius=5)
            rf.pack(fill="x", padx=8, pady=4)

            en_var = ctk.BooleanVar(value=en_def)
            sym_var = ctk.StringVar(value=sym_def)
            expr_var = ctk.StringVar(value=expr_def)
            type_var = ctk.StringVar(value=type_def)

            chk = ctk.CTkCheckBox(rf, text="", variable=en_var, width=22, checkbox_width=18, checkbox_height=18)
            chk.grid(row=0, column=0, padx=(6, 2), pady=6)

            sym_ent = ctk.CTkEntry(rf, textvariable=sym_var, width=120, font=("Consolas", 11))
            sym_ent.grid(row=0, column=1, padx=3, pady=6)
            sym_ent.bind("<Return>", lambda e: self.trigger_write(immediate=True))

            ctk.CTkLabel(rf, text="=", font=("Consolas", 12, "bold")).grid(row=0, column=2, padx=1)

            expr_ent = ctk.CTkEntry(rf, textvariable=expr_var, width=155, font=("Consolas", 11))
            expr_ent.grid(row=0, column=3, padx=3, pady=6)
            expr_ent.bind("<KeyRelease>", lambda e: self._evaluate_all_rows())
            expr_ent.bind("<Return>", lambda e: self._on_formula_enter())

            t_combo = ctk.CTkComboBox(
                rf,
                variable=type_var,
                values=["uint8", "uint16", "uint32", "int8", "int16", "int32", "float"],
                width=74,
                font=("Consolas", 11),
                command=lambda _: self._evaluate_all_rows(),
            )
            t_combo.grid(row=0, column=4, padx=3, pady=6)

            val_lbl = ctk.CTkLabel(
                rf,
                text="0",
                width=68,
                anchor="e",
                font=("Consolas", 12, "bold"),
                text_color="#4CAF50",
            )
            val_lbl.grid(row=0, column=5, padx=(4, 8), pady=6)

            self.mapping_rows.append(
                {
                    "enabled": en_var,
                    "var_name": sym_var,
                    "formula": expr_var,
                    "var_type": type_var,
                    "val_lbl": val_lbl,
                }
            )

        hint_lbl = ctk.CTkLabel(
            right_pane,
            text="Tip: Drag inside ROI to pan, drag corners/edges to resize, or drag outside for new ROI.",
            font=("Consolas", 10),
            text_color="#888888",
        )
        hint_lbl.pack(fill="x", padx=10, pady=(4, 6))

    # --- VERTEX DICTIONARY & FORMULA EVALUATION ---
    def get_vertex_dict(self) -> dict:
        """Returns the 4 vertex coordinates (UL, UR, LL, LR) plus virtual frame dimensions."""
        return {
            "UL_X": self.x0,
            "UL_Y": self.y0,
            "UR_X": self.x1,
            "UR_Y": self.y0,
            "LL_X": self.x0,
            "LL_Y": self.y1,
            "LR_X": self.x1,
            "LR_Y": self.y1,
            "FW": self.fw,
            "FH": self.fh,
        }

    def _encode_typed_value(self, raw_num: float, v_type: str):
        """Encodes evaluated formula output into (tcl_mem_cmd, hex_str, display_str)."""
        if v_type == "float":
            fval = float(raw_num)
            packed = struct.pack("<f", fval)
            u32 = struct.unpack("<I", packed)[0]
            return "mww", f"0x{u32:08X}", f"{fval:.4g}"

        ival = int(round(raw_num))
        bits = 8 if "8" in v_type else (16 if "16" in v_type else 32)
        if v_type.startswith("int"):
            lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
            ival = max(lo, min(hi, ival))
            reg_val = (1 << bits) + ival if ival < 0 else ival
        else:
            lo, hi = 0, (1 << bits) - 1
            ival = max(lo, min(hi, ival))
            reg_val = ival

        cmd = "mwb" if bits == 8 else ("mwh" if bits == 16 else "mww")
        return cmd, hex(reg_val), str(ival)

    def _evaluate_all_rows(self):
        """Updates the live value preview on all 4 formula rows and returns valid write jobs."""
        vdict = self.get_vertex_dict()
        w_px = self.x1 - self.x0 + 1
        h_px = self.y1 - self.y0 + 1
        self.vertex_summary_lbl.configure(
            text=f"UL({self.x0},{self.y0}) UR({self.x1},{self.y0}) LL({self.x0},{self.y1}) LR({self.x1},{self.y1}) | {w_px}x{h_px}"
        )

        jobs = []
        for row in self.mapping_rows:
            expr = row["formula"].get().strip()
            v_type = row["var_type"].get()
            try:
                val = eval_roi_formula(expr, vdict)
                cmd, hex_val, disp_str = self._encode_typed_value(val, v_type)
                row["val_lbl"].configure(text=disp_str, text_color="#4CAF50")
                if row["enabled"].get() and row["var_name"].get().strip():
                    jobs.append((row["var_name"].get().strip(), cmd, hex_val, disp_str))
            except Exception:
                row["val_lbl"].configure(text="ERR", text_color="#EF5350")
        return jobs

    def _on_formula_enter(self):
        self._evaluate_all_rows()
        if self.live_write.get():
            self.trigger_write(immediate=True)

    # --- FRAME & COORDINATE MANAGEMENT ---
    def apply_frame_size(self):
        try:
            new_fw = max(1, int(self.frame_w_var.get().strip()))
            new_fh = max(1, int(self.frame_h_var.get().strip()))
        except ValueError:
            self.app.log_msg("ROI Error: Frame W and Frame H must be positive integers.")
            return

        self.fw = new_fw
        self.fh = new_fh
        self.frame_w_var.set(str(self.fw))
        self.frame_h_var.set(str(self.fh))

        self.x0 = max(0, min(self.fw - 1, self.x0))
        self.x1 = max(self.x0, min(self.fw - 1, self.x1))
        self.y0 = max(0, min(self.fh - 1, self.y0))
        self.y1 = max(self.y0, min(self.fh - 1, self.y1))

        self._sync_coord_entries()
        self._redraw_canvas()
        self._evaluate_all_rows()
        if self.live_write.get():
            self.trigger_write(immediate=True)

    def reset_full_roi(self):
        self.x0, self.y0 = 0, 0
        self.x1, self.y1 = self.fw - 1, self.fh - 1
        self._sync_coord_entries()
        self._redraw_canvas()
        self._evaluate_all_rows()
        if self.live_write.get():
            self.trigger_write(immediate=True)

    def apply_manual_coords(self):
        try:
            nx0 = int(self.entry_ul_x.get().strip())
            ny0 = int(self.entry_ul_y.get().strip())
            nx1 = int(self.entry_lr_x.get().strip())
            ny1 = int(self.entry_lr_y.get().strip())
        except ValueError:
            self.app.log_msg("ROI Error: Vertex coordinates must be integers.")
            return

        if nx0 > nx1:
            nx0, nx1 = nx1, nx0
        if ny0 > ny1:
            ny0, ny1 = ny1, ny0

        self.x0 = max(0, min(self.fw - 1, nx0))
        self.x1 = max(self.x0, min(self.fw - 1, nx1))
        self.y0 = max(0, min(self.fh - 1, ny0))
        self.y1 = max(self.y0, min(self.fh - 1, ny1))

        self._sync_coord_entries()
        self._redraw_canvas()
        self._evaluate_all_rows()
        if self.live_write.get():
            self.trigger_write(immediate=True)

    def _sync_coord_entries(self):
        self.entry_ul_x.set(str(self.x0))
        self.entry_ul_y.set(str(self.y0))
        self.entry_lr_x.set(str(self.x1))
        self.entry_lr_y.set(str(self.y1))

    # --- 2D CANVAS RENDERING & COORDINATE MAPPING ---
    def _virt_to_canvas(self, vx: int, vy: int):
        fl, ft, fr, fb = self._frame_rect
        pw = max(1.0, fr - fl)
        ph = max(1.0, fb - ft)
        cx = fl + (vx / (self.fw - 1)) * pw if self.fw > 1 else fl
        cy = ft + (vy / (self.fh - 1)) * ph if self.fh > 1 else ft
        return cx, cy

    def _canvas_to_virt(self, cx: float, cy: float):
        fl, ft, fr, fb = self._frame_rect
        pw = max(1.0, fr - fl)
        ph = max(1.0, fb - ft)
        vx = int(round(((cx - fl) / pw) * (self.fw - 1))) if self.fw > 1 else 0
        vy = int(round(((cy - ft) / ph) * (self.fh - 1))) if self.fh > 1 else 0
        return max(0, min(self.fw - 1, vx)), max(0, min(self.fh - 1, vy))

    def _redraw_canvas(self):
        if not self.winfo_exists():
            return
        c = self.roi_canvas
        c.delete("all")

        cw = max(80, c.winfo_width())
        ch = max(80, c.winfo_height())
        pad_x, pad_y = 22, 18
        avail_w = max(40, cw - 2 * pad_x)
        avail_h = max(40, ch - 2 * pad_y)

        # Fit virtual frame with aspect ratio preserved (minimum 28px thickness for extreme line-scan ratios)
        scale = min(avail_w / max(1, self.fw), avail_h / max(1, self.fh))
        draw_w = max(28.0, min(avail_w, self.fw * scale))
        draw_h = max(28.0, min(avail_h, self.fh * scale))

        fl = (cw - draw_w) / 2.0
        ft = (ch - draw_h) / 2.0
        fr = fl + draw_w
        fb = ft + draw_h
        self._frame_rect = (fl, ft, fr, fb)

        # Virtual sensor background & subtle rule-of-thirds grid
        c.create_rectangle(fl, ft, fr, fb, fill="#182026", outline="#546E7A", width=1.5)
        for frac in (1 / 3, 2 / 3):
            gx = fl + draw_w * frac
            gy = ft + draw_h * frac
            c.create_line(gx, ft, gx, fb, fill="#222D35", dash=(3, 3))
            c.create_line(fl, gy, fr, gy, fill="#222D35", dash=(3, 3))

        c.create_text(fl, ft - 9, text="(0,0)", fill="#78909C", font=("Consolas", 8), anchor="w")
        c.create_text(fr, fb + 9, text=f"({self.fw - 1},{self.fh - 1})", fill="#78909C", font=("Consolas", 8), anchor="e")

        # Selected ROI rectangle
        rx0, ry0 = self._virt_to_canvas(self.x0, self.y0)
        rx1, ry1 = self._virt_to_canvas(self.x1, self.y1)

        c.create_rectangle(
            rx0, ry0, rx1, ry1,
            fill="#0D3B4C",
            stipple="gray50",
            outline="#00E5FF",
            width=2,
        )

        # Corner labels (UL, UR, LL, LR)
        c.create_text(rx0 + 4, ry0 + 3, text="UL", fill="#80DEEA", font=("Consolas", 8, "bold"), anchor="nw")
        if (rx1 - rx0) > 38:
            c.create_text(rx1 - 4, ry0 + 3, text="UR", fill="#80DEEA", font=("Consolas", 8, "bold"), anchor="ne")
        if (ry1 - ry0) > 28:
            c.create_text(rx0 + 4, ry1 - 3, text="LL", fill="#80DEEA", font=("Consolas", 8, "bold"), anchor="sw")
            if (rx1 - rx0) > 38:
                c.create_text(rx1 - 4, ry1 - 3, text="LR", fill="#80DEEA", font=("Consolas", 8, "bold"), anchor="se")

        # 8 Resize Grab Handles (4 corners + 4 mid-edges)
        mx = (rx0 + rx1) / 2.0
        my = (ry0 + ry1) / 2.0
        hs = 4
        for hx, hy in (
            (rx0, ry0), (rx1, ry0), (rx0, ry1), (rx1, ry1),
            (mx, ry0), (mx, ry1), (rx0, my), (rx1, my),
        ):
            c.create_rectangle(hx - hs, hy - hs, hx + hs, hy + hs, fill="#00E5FF", outline="#00252E")

    # --- HIT TESTING & INTERACTIVE DRAGGING ---
    def _hit_test(self, cx: float, cy: float):
        rx0, ry0 = self._virt_to_canvas(self.x0, self.y0)
        rx1, ry1 = self._virt_to_canvas(self.x1, self.y1)
        mx, my = (rx0 + rx1) / 2.0, (ry0 + ry1) / 2.0
        r = self.HANDLE_RADIUS

        # 1. Corners first
        if abs(cx - rx0) <= r and abs(cy - ry0) <= r:
            return "nw", "size_nw_se"
        if abs(cx - rx1) <= r and abs(cy - ry0) <= r:
            return "ne", "size_ne_sw"
        if abs(cx - rx0) <= r and abs(cy - ry1) <= r:
            return "sw", "size_ne_sw"
        if abs(cx - rx1) <= r and abs(cy - ry1) <= r:
            return "se", "size_nw_se"

        # 2. Mid-edge handles & edge lines
        if abs(cy - ry0) <= r and (rx0 <= cx <= rx1 or abs(cx - mx) <= r):
            return "n", "sb_v_double_arrow"
        if abs(cy - ry1) <= r and (rx0 <= cx <= rx1 or abs(cx - mx) <= r):
            return "s", "sb_v_double_arrow"
        if abs(cx - rx0) <= r and (ry0 <= cy <= ry1 or abs(cy - my) <= r):
            return "w", "sb_h_double_arrow"
        if abs(cx - rx1) <= r and (ry0 <= cy <= ry1 or abs(cy - my) <= r):
            return "e", "sb_h_double_arrow"

        # 3. Inside ROI -> Pan whole window
        if rx0 < cx < rx1 and ry0 < cy < ry1:
            return "move", "fleur"

        # 4. Outside ROI -> Draw new box
        return "new", "crosshair"

    def _on_canvas_hover(self, event):
        if self._drag_mode is None:
            _, cursor_name = self._hit_test(event.x, event.y)
            self.roi_canvas.configure(cursor=cursor_name)

    def _on_canvas_press(self, event):
        self.lift()
        self._lift_handles()
        mode, cursor_name = self._hit_test(event.x, event.y)
        self._drag_mode = mode
        self.roi_canvas.configure(cursor=cursor_name)

        vx, vy = self._canvas_to_virt(event.x, event.y)
        self._drag_start_virt = (vx, vy)
        self._drag_orig_roi = (self.x0, self.y0, self.x1, self.y1)

        if mode == "new":
            self.x0 = self.x1 = vx
            self.y0 = self.y1 = vy
            self._sync_coord_entries()
            self._redraw_canvas()
            self._evaluate_all_rows()

    def _on_canvas_drag(self, event):
        if not self._drag_mode:
            return
        vx, vy = self._canvas_to_virt(event.x, event.y)
        ox0, oy0, ox1, oy1 = self._drag_orig_roi
        sx, sy = self._drag_start_virt
        m = self._drag_mode

        if m == "nw":
            self.x0 = min(ox1, vx)
            self.y0 = min(oy1, vy)
        elif m == "ne":
            self.x1 = max(ox0, vx)
            self.y0 = min(oy1, vy)
        elif m == "sw":
            self.x0 = min(ox1, vx)
            self.y1 = max(oy0, vy)
        elif m == "se":
            self.x1 = max(ox0, vx)
            self.y1 = max(oy0, vy)
        elif m == "n":
            self.y0 = min(oy1, vy)
        elif m == "s":
            self.y1 = max(oy0, vy)
        elif m == "w":
            self.x0 = min(ox1, vx)
        elif m == "e":
            self.x1 = max(ox0, vx)
        elif m == "move":
            # Clamp translation delta so width & height never squash against frame borders
            dx = max(-ox0, min((self.fw - 1) - ox1, vx - sx))
            dy = max(-oy0, min((self.fh - 1) - oy1, vy - sy))
            self.x0, self.x1 = ox0 + dx, ox1 + dx
            self.y0, self.y1 = oy0 + dy, oy1 + dy
        elif m == "new":
            self.x0, self.x1 = min(sx, vx), max(sx, vx)
            self.y0, self.y1 = min(sy, vy), max(sy, vy)

        self._sync_coord_entries()
        self._redraw_canvas()
        self._evaluate_all_rows()

        if self.live_write.get():
            self.trigger_write(immediate=False)

    def _on_canvas_release(self, event):
        if self._drag_mode is not None:
            self._drag_mode = None
            if self.live_write.get():
                # Flush final resting coordinates immediately on release
                self.trigger_write(immediate=True, warn_unresolved=True)

    # --- COALESCED ATOMIC BATCH JTAG WRITE ---
    def trigger_write(self, immediate=False, warn_unresolved=None):
        if not self.winfo_exists():
            return
        if warn_unresolved is None:
            warn_unresolved = immediate

        if self._debounce_timer is not None:
            self.after_cancel(self._debounce_timer)
            self._debounce_timer = None

        if immediate:
            self._dispatch_batch_write(warn_unresolved=warn_unresolved)
        else:
            self._debounce_timer = self.after(
                100, lambda: self._dispatch_batch_write(warn_unresolved=False)
            )

    def _dispatch_batch_write(self, warn_unresolved=False):
        self._debounce_timer = None
        if not self.winfo_exists():
            return

        # Clear abort latch on explicit manual click
        if warn_unresolved:
            self.app.abort_transfers = False

        if self._write_busy:
            # Coalesce overlapping drag updates instead of queueing extra threads
            self._write_pending = True
            return

        jobs = self._evaluate_all_rows()
        if not jobs:
            return

        resolved_cmds = []
        summary_parts = []
        unresolved = []

        for sym_name, mem_cmd, hex_val, disp_str in jobs:
            addr = self.get_resolved_addr(sym_name)
            if addr:
                resolved_cmds.append(f"{mem_cmd} {addr} {hex_val}")
                summary_parts.append(f"{sym_name}={disp_str}")
            else:
                unresolved.append(sym_name)

        if unresolved and warn_unresolved:
            self.app.log_msg(f"ROI Wait: Unresolved map symbol(s): {', '.join(unresolved)}")

        if not resolved_cmds:
            return

        batch_tcl = "; ".join(resolved_cmds)
        summary_txt = ", ".join(summary_parts)
        self._write_busy = True

        def _write_task():
            try:
                with self.app.jtag_lock:
                    if getattr(self.app, "abort_transfers", False):
                        return
                    try:
                        with OpenOCDSession(timeout=2.0) as ocd:
                            state = ocd.send("[target current] curstate")
                            was_running = "running" in state
                            if was_running:
                                ocd.send("halt; wait_halt")
                            ocd.send(batch_tcl)
                            if was_running and not self.app.target_halted_at_bp:
                                ocd.send("resume")
                        if not getattr(self.app, "abort_transfers", False):
                            self.after(0, lambda: self.app.log_msg(f"ROI Write: {summary_txt}"))
                    except Exception as e:
                        if warn_unresolved and not getattr(self.app, "abort_transfers", False):
                            self.after(0, lambda: self.app.log_msg(f"ROI Write ERROR: {e}"))
            finally:
                self._write_busy = False
                if (
                    self._write_pending
                    and self.winfo_exists()
                    and not getattr(self.app, "abort_transfers", False)
                ):
                    self._write_pending = False
                    self.after(10, lambda: self._dispatch_batch_write(warn_unresolved=False))
                else:
                    self._write_pending = False

        threading.Thread(target=_write_task, daemon=True).start()

    # --- TARGET RAM SYNC & 4x4 INVERSE LINEAR SOLVER ---
    def _extract_linear_coeffs(self, expr: str):
        """Extracts [a_x0, a_y0, a_x1, a_y1], const_offset, uses_frame_dims for a linear ROI formula."""
        def _eval_pt(px0, py0, px1, py1, pfw=self.fw, pfh=self.fh):
            vd = {
                "UL_X": px0, "UL_Y": py0,
                "UR_X": px1, "UR_Y": py0,
                "LL_X": px0, "LL_Y": py1,
                "LR_X": px1, "LR_Y": py1,
                "FW": pfw, "FH": pfh,
            }
            return eval_roi_formula(expr, vd, relax_floor_div=True)

        c = _eval_pt(0.0, 0.0, 0.0, 0.0)
        a0 = _eval_pt(1.0, 0.0, 0.0, 0.0) - c
        a1 = _eval_pt(0.0, 1.0, 0.0, 0.0) - c
        a2 = _eval_pt(0.0, 0.0, 1.0, 0.0) - c
        a3 = _eval_pt(0.0, 0.0, 0.0, 1.0) - c

        # Verify linearity at an arbitrary test point
        test_lin = a0 * 7.0 + a1 * 13.0 + a2 * 29.0 + a3 * 41.0 + c
        test_act = _eval_pt(7.0, 13.0, 29.0, 41.0)
        if abs(test_lin - test_act) > 1e-5:
            raise ValueError("Non-linear ROI formula")

        uses_frame = abs(_eval_pt(0.0, 0.0, 0.0, 0.0, self.fw + 10, self.fh + 10) - c) > 1e-6
        return [a0, a1, a2, a3], c, uses_frame

    @staticmethod
    def _matrix_rank(rows):
        M = [r[:] for r in rows]
        rank = 0
        num_rows = len(M)
        for col in range(4):
            piv = next((r for r in range(rank, num_rows) if abs(M[r][col]) > 1e-9), None)
            if piv is None:
                continue
            M[rank], M[piv] = M[piv], M[rank]
            pval = M[rank][col]
            for r in range(rank + 1, num_rows):
                fac = M[r][col] / pval
                for c in range(col, 4):
                    M[r][c] -= fac * M[rank][c]
            rank += 1
        return rank

    @staticmethod
    def _solve_4x4(mat, rhs):
        M = [mat[i][:] + [rhs[i]] for i in range(4)]
        for col in range(4):
            piv = max(range(col, 4), key=lambda r: abs(M[r][col]))
            if abs(M[piv][col]) < 1e-9:
                return None
            M[col], M[piv] = M[piv], M[col]
            pval = M[col][col]
            for r in range(col + 1, 4):
                fac = M[r][col] / pval
                for c in range(col, 5):
                    M[r][c] -= fac * M[col][c]
        sol = [0.0] * 4
        for r in range(3, -1, -1):
            sol[r] = (M[r][4] - sum(M[r][c] * sol[c] for c in range(r + 1, 4))) / M[r][r]
        return sol

    def get_sync_requests(self):
        reqs = []
        for row in self.mapping_rows:
            if not row["enabled"].get():
                continue
            name = row["var_name"].get().strip()
            if not name:
                continue
            addr = self.get_resolved_addr(name)
            if not addr:
                self.app.log_msg(f"ROI Sync Skip: '{name}' is not a valid map symbol or hex address.")
                continue
            v_type = row["var_type"].get()
            mem_cmd = "mdw" if v_type == "float" else ("mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw"))
            reqs.append((addr, mem_cmd))
        return reqs

    def apply_sync_results(self, raw_map: dict):
        if not self.winfo_exists():
            return
        if self._debounce_timer is not None:
            self.after_cancel(self._debounce_timer)
            self._debounce_timer = None

        read_rows = []
        eq_mat = []
        eq_rhs = []
        any_uses_frame = False
        solvable = True

        for row in self.mapping_rows:
            if not row["enabled"].get():
                continue
            name = row["var_name"].get().strip()
            if not name:
                continue
            addr = self.get_resolved_addr(name)
            if not addr:
                continue
            v_type = row["var_type"].get()
            mem_cmd = "mdw" if v_type == "float" else ("mdb" if "8" in v_type else ("mdh" if "16" in v_type else "mdw"))
            raw_u32 = raw_map.get((addr, mem_cmd))
            if raw_u32 is None:
                continue

            target_val = decode_ocd_word(raw_u32, v_type)
            read_rows.append((row, name, v_type, target_val))

            try:
                coeffs, c_off, uses_fr = self._extract_linear_coeffs(row["formula"].get())
                any_uses_frame = any_uses_frame or uses_fr
                if len(eq_mat) < 4 and self._matrix_rank(eq_mat + [coeffs]) > len(eq_mat):
                    eq_mat.append(coeffs)
                    eq_rhs.append(float(target_val) - c_off)
                else:
                    solvable = False
            except Exception:
                solvable = False

        if not read_rows:
            return

        # Complete any unconstrained degrees of freedom (e.g. 2-var X-only crop) using current coordinates
        curr_u = [float(self.x0), float(self.y0), float(self.x1), float(self.y1)]
        if solvable and len(eq_mat) < 4:
            for j in range(4):
                unit = [1.0 if k == j else 0.0 for k in range(4)]
                if self._matrix_rank(eq_mat + [unit]) > len(eq_mat):
                    eq_mat.append(unit)
                    eq_rhs.append(curr_u[j])
                if len(eq_mat) == 4:
                    break

        sol = self._solve_4x4(eq_mat, eq_rhs) if (solvable and len(eq_mat) == 4) else None

        if sol is not None:
            nx0, ny0, nx1, ny1 = [int(round(v)) for v in sol]

            # Auto-expand virtual frame if target holds a valid larger ROI and formulas don't depend on FW/FH
            if not any_uses_frame and 0 <= nx0 <= nx1 < 65536 and 0 <= ny0 <= ny1 < 65536:
                if nx1 >= self.fw or ny1 >= self.fh:
                    self.fw = max(self.fw, nx1 + 1)
                    self.fh = max(self.fh, ny1 + 1)
                    self.frame_w_var.set(str(self.fw))
                    self.frame_h_var.set(str(self.fh))
                    self.app.log_msg(f"ROI Sync Note: Expanded virtual frame to {self.fw}x{self.fh} to fit target ROI.")

            clamped = (
                nx0 < 0 or ny0 < 0 or nx1 >= self.fw or ny1 >= self.fh or nx0 > nx1 or ny0 > ny1
            )
            self.x0 = max(0, min(self.fw - 1, nx0))
            self.x1 = max(self.x0, min(self.fw - 1, nx1))
            self.y0 = max(0, min(self.fh - 1, ny0))
            self.y1 = max(self.y0, min(self.fh - 1, ny1))

            self._sync_coord_entries()
            self._redraw_canvas()
            self._evaluate_all_rows()

            if clamped:
                self.app.log_msg(
                    "ROI Sync Warning: Target RAM registers mapped outside valid ROI bounds (e.g. zero/uninitialized size); visual box clamped, showing raw RAM values in amber."
                )

        # Highlight any row whose raw target RAM value differs from the clamped/current visual ROI
        vdict = self.get_vertex_dict()
        for row, name, v_type, target_val in read_rows:
            disp_target = f"{target_val:.4g}" if v_type == "float" else str(int(target_val))
            try:
                re_eval = eval_roi_formula(row["formula"].get(), vdict)
                _, _, disp_box = self._encode_typed_value(re_eval, v_type)
            except Exception:
                disp_box = None

            if sol is None or disp_box != disp_target:
                row["val_lbl"].configure(text=disp_target, text_color="#FFB74D")

    # --- LAYOUT PERSISTENCE ---
    def get_state(self):
        return {
            "widget_type": "roi_crop",
            "frame_w": str(self.fw),
            "frame_h": str(self.fh),
            "roi_coords": [self.x0, self.y0, self.x1, self.y1],
            "live_write": bool(self.live_write.get()),
            "mappings": [
                {
                    "enabled": bool(r["enabled"].get()),
                    "var_name": r["var_name"].get(),
                    "formula": r["formula"].get(),
                    "var_type": r["var_type"].get(),
                }
                for r in self.mapping_rows
            ],
        }

    def set_state(self, state):
        try:
            self.fw = max(1, int(state.get("frame_w", 1920)))
            self.fh = max(1, int(state.get("frame_h", 1080)))
        except (ValueError, TypeError):
            self.fw, self.fh = 1920, 1080

        self.frame_w_var.set(str(self.fw))
        self.frame_h_var.set(str(self.fh))

        coords = state.get("roi_coords", [0, 0, self.fw - 1, self.fh - 1])
        if isinstance(coords, list) and len(coords) == 4:
            self.x0 = max(0, min(self.fw - 1, int(coords[0])))
            self.y0 = max(0, min(self.fh - 1, int(coords[1])))
            self.x1 = max(self.x0, min(self.fw - 1, int(coords[2])))
            self.y1 = max(self.y0, min(self.fh - 1, int(coords[3])))

        self.live_write.set(bool(state.get("live_write", True)))

        saved_maps = state.get("mappings", [])
        for i, m_state in enumerate(saved_maps):
            if i < len(self.mapping_rows):
                self.mapping_rows[i]["enabled"].set(bool(m_state.get("enabled", True)))
                self.mapping_rows[i]["var_name"].set(m_state.get("var_name", ""))
                self.mapping_rows[i]["formula"].set(m_state.get("formula", ""))
                self.mapping_rows[i]["var_type"].set(m_state.get("var_type", "uint32"))

        self._sync_coord_entries()
        self._evaluate_all_rows()
        self.after(30, self._redraw_canvas)