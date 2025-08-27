import os, sys, json, shutil, tempfile, subprocess, webbrowser
from io import BytesIO
from dataclasses import dataclass

# -------- GUI --------
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# -------- Imaging --------
from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageFile, ExifTags
# HEIF/HEIC/AVIF support (optional)
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:
    HEIF_OK = False
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 200_000_000

# -------- Optional: background removal --------
try:
    import rembg  # pip install rembg
    REMBG_OK = True
except Exception:
    REMBG_OK = False


# ---------- ExifTool discovery ----------
def resource_path(*parts):
    """Return absolute path to a bundled resource (works in dev and PyInstaller)."""
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))  # _MEIPASS is set by PyInstaller onefile
    return os.path.join(base, *parts)

def find_exiftool():
    """
    Search order:
    1) Bundled copy inside the PyInstaller image: tools/exiftool.exe
    2) Project-relative tools/ folders while developing
    3) PATH
    """
    candidates = [
        resource_path("tools", "exiftool.exe"),
        os.path.join("tools", "exiftool.exe"),
        os.path.join("tools", "exiftool_files", "exiftool.exe"),
        "exiftool",  # on PATH
    ]
    for c in candidates:
        # accept bundled path or resolved PATH entry
        if os.path.isabs(c) and os.path.exists(c):
            return c
        p = shutil.which(c)
        if p:
            return p
    return None

EXIFTOOL = find_exiftool()


# ---------- Utilities ----------
def read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

def save_file_bytes(path: str, data: bytes):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

def open_image_any(raw_or_path) -> Image.Image:
    if isinstance(raw_or_path, (bytes, bytearray)):
        return Image.open(BytesIO(raw_or_path))
    return Image.open(raw_or_path)

def to_png_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

def ensure_rgb(img: Image.Image) -> Image.Image:
    # JPEG cannot save RGBA/LA/P modes
    if img.mode in ("RGBA", "LA", "P"):
        return img.convert("RGB")
    return img


# ---------- EXIF Reading ----------
def exiftool_json_from_bytes(data: bytes, hint_ext: str = ".jpg") -> dict:
    """Write to temp file, call exiftool -j -n, return dict (or {})."""
    if not EXIFTOOL:
        return {}
    with tempfile.NamedTemporaryFile(delete=False, suffix=hint_ext) as t:
        t.write(data)
        temp_in = t.name
    try:
        cp = subprocess.run([EXIFTOOL, "-j", "-n", "-api", "largefilesupport=1", temp_in],
                            capture_output=True, text=True)
        if cp.returncode not in (0, 1):  # 1 is often "minor warnings"
            raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or f"exiftool exit {cp.returncode}")
        obj = json.loads(cp.stdout) if cp.stdout else [{}]
        return obj[0] if obj else {}
    except Exception:
        return {}
    finally:
        try: os.remove(temp_in)
        except Exception: pass

def pil_exif_fallback(data: bytes) -> dict:
    """Very small EXIF fallback using PIL, including GPS if present."""
    try:
        im = Image.open(BytesIO(data))
        info = getattr(im, "_getexif", lambda: None)()
        if not info:
            return {}
        tagmap = {ExifTags.TAGS.get(k, str(k)): v for k, v in info.items()}
        # GPS
        gps = tagmap.get("GPSInfo")
        if gps and isinstance(gps, dict):
            inv = {ExifTags.GPSTAGS.get(k, str(k)): v for k, v in gps.items()}
            tagmap["GPSLatitudeRef"] = inv.get("GPSLatitudeRef")
            tagmap["GPSLongitudeRef"] = inv.get("GPSLongitudeRef")
            tagmap["GPSLatitude"] = inv.get("GPSLatitude")
            tagmap["GPSLongitude"] = inv.get("GPSLongitude")
            # Convert to decimal if possible
            def _to_deg(v):
                try:
                    d, m, s = v
                    as_float = lambda r: float(r[0]) / float(r[1]) if isinstance(r, tuple) else float(r)
                    return as_float(d) + as_float(m)/60.0 + as_float(s)/3600.0
                except Exception:
                    return None
            lat = _to_deg(tagmap.get("GPSLatitude"))
            lon = _to_deg(tagmap.get("GPSLongitude"))
            if lat is not None and lon is not None:
                # apply N/S/E/W
                if (tagmap.get("GPSLatitudeRef") or "N").upper() == "S":
                    lat = -lat
                if (tagmap.get("GPSLongitudeRef") or "E").upper() == "W":
                    lon = -lon
                tagmap["GPSLatitude"] = lat
                tagmap["GPSLongitude"] = lon
        return tagmap
    except Exception:
        return {}

def read_exif_from_bytes(data: bytes, hint_ext: str = ".jpg") -> dict:
    d = exiftool_json_from_bytes(data, hint_ext)
    if d:
        return d
    return pil_exif_fallback(data)


# ---------- EXIF writing (selective strip) ----------
GPS_ARGS      = ["-GPS:all="]  # removes EXIF GPS block (covers common cases)
DEVICE_ARGS   = ["-Make=", "-Model=", "-SerialNumber=", "-BodySerialNumber=",
                 "-CameraSerialNumber=", "-LensSerialNumber=", "-LensModel=", "-LensID=", "-Artist=", "-OwnerName="]
DATETIME_ARGS = ["-DateTimeOriginal=", "-CreateDate=", "-ModifyDate=", "-SubSecTime*=", "-OffsetTime*="]

def build_strip_args(categories: list[str]) -> list[str]:
    args = []
    if "gps" in categories:      args += GPS_ARGS
    if "device" in categories:   args += DEVICE_ARGS
    if "datetime" in categories: args += DATETIME_ARGS
    return args

def exiftool_write_from_bytes(data: bytes, args_list, hint_ext: str = ".jpg") -> bytes:
    """Apply ExifTool edits and return output bytes (with better error reporting)."""
    if not EXIFTOOL:
        raise RuntimeError("ExifTool not available")
    with tempfile.NamedTemporaryFile(delete=False, suffix=hint_ext) as t:
        t.write(data)
        temp_in = t.name
    temp_out = temp_in + ".out"
    try:
        cmd = [EXIFTOOL, "-m", "-o", temp_out] + list(args_list) + [temp_in]
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode not in (0, 1):
            if os.path.exists(temp_out):
                out = read_file_bytes(temp_out)
            else:
                raise RuntimeError(f"exiftool exit {cp.returncode}\n\nCommand:\n{cmd}\n\nStdErr:\n{cp.stderr.strip() or '(none)'}")
        else:
            out = read_file_bytes(temp_out) if os.path.exists(temp_out) else read_file_bytes(temp_in)
        return out
    finally:
        for p in (temp_in, temp_out):
            try: os.remove(p)
            except Exception: pass


# ---------- Watermark ----------
def add_watermark(img: Image.Image, text="SAFE", opacity=160) -> Image.Image:
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0,0,0,0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.truetype("arial.ttf", max(18, img.width//18))
    except Exception:
        font = ImageFont.load_default()
    # bottom-right
    if hasattr(draw, "textlength"):
        tw = draw.textlength(text, font=font)
    else:
        tw = draw.textbbox((0,0), text, font=font)[2]
    th = getattr(font, "size", 16)
    x = max(8, img.width - int(tw) - 20)
    y = max(8, img.height - th - 20)
    draw.text((x, y), text, fill=(255,255,255,int(max(10,min(opacity,255)))), font=font)
    return Image.alpha_composite(img, overlay)


# ---------- Dataclass for summary ----------
@dataclass
class ExifSummary:
    gps: tuple | None
    make: str | None
    model: str | None
    owner: str | None
    software: str | None
    captured: str | None
    serials: list

def summarize_exif(ex: dict) -> ExifSummary:
    def gv(*keys):
        for k in keys:
            if k in ex and str(ex[k]).strip():
                return ex[k]
        return None
    gps = None
    lat = gv("GPSLatitude", "EXIF:GPSLatitude")
    lon = gv("GPSLongitude", "EXIF:GPSLongitude")
    if lat is not None and lon is not None:
        try:
            gps = (float(lat), float(lon))
        except Exception:
            gps = None
    make = gv("Make","EXIF:Make","XMP:Make")
    model = gv("Model","EXIF:Model","XMP:Model")
    owner = gv("Artist","EXIF:Artist","XMP:Creator","XMP:OwnerName","Microsoft:XPAuthor")
    software = gv("Software","EXIF:Software","XMP:CreatorTool")
    captured = gv("DateTimeOriginal","EXIF:DateTimeOriginal","CreateDate","EXIF:CreateDate")

    serials = []
    for k,v in ex.items():
        if "serial" in str(k).lower() and str(v).strip():
            serials.append(f"{k.split(':')[-1]}: {v}")
    return ExifSummary(gps, make, model, owner, software, captured, serials)


# ============ TKINTER APP ============
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DB-CyberImageToolset")
        self.geometry("1000x640")
        self.minsize(880, 560)

        # One-image state
        self.original_path: str | None = None
        self.original_bytes: bytes | None = None
        self.current_bytes: bytes | None = None
        self.current_image: Image.Image | None = None
        self.current_exif: dict = {}
        self.current_ext: str = ".jpg"  # hint for exiftool temp

        # Layout
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.left = ttk.Frame(self, padding=10)
        self.left.grid(row=0, column=0, sticky="ns")
        self.right = ttk.Frame(self, padding=10)
        self.right.grid(row=0, column=1, sticky="nsew")
        self.right.rowconfigure(1, weight=1)
        self.right.columnconfigure(0, weight=1)

        # --- Left panel: controls ---
        ttk.Style().configure("TButton", padding=6)
        ttk.Button(self.left, text="Open Image", command=self.action_open).grid(sticky="ew", pady=(0,6))
        self.btn_save = ttk.Button(self.left, text="Save As…", command=self.action_save, state="disabled")
        self.btn_save.grid(sticky="ew", pady=3)
        self.btn_revert = ttk.Button(self.left, text="Revert to Original", command=self.action_revert, state="disabled")
        self.btn_revert.grid(sticky="ew", pady=3)

        ttk.Separator(self.left).grid(sticky="ew", pady=8)

        # SAFE pipeline with checkbox OPTION
        self.var_striponly = tk.BooleanVar(value=False)
        self.btn_safe = ttk.Button(self.left, text="Make SAFE Copy", command=self.action_safe, state="disabled")
        self.btn_safe.grid(sticky="ew", pady=(3,0))
        ttk.Checkbutton(self.left, text="Strip-only (no WM/BG)", variable=self.var_striponly)            .grid(sticky="w", pady=(2,8))

        ttk.Separator(self.left).grid(sticky="ew", pady=8)

        self.btn_bg = ttk.Button(self.left, text="Remove Background", command=self.action_remove_bg, state="disabled")
        self.btn_bg.grid(sticky="ew", pady=3)

        self.btn_strip_gps = ttk.Button(self.left, text="Strip GPS (ExifTool)", command=lambda: self.action_strip("gps"), state="disabled")
        self.btn_strip_gps.grid(sticky="ew", pady=3)
        self.btn_strip_dev = ttk.Button(self.left, text="Strip Device (ExifTool)", command=lambda: self.action_strip("device"), state="disabled")
        self.btn_strip_dev.grid(sticky="ew", pady=3)
        self.btn_strip_dt  = ttk.Button(self.left, text="Strip Date/Time (ExifTool)", command=lambda: self.action_strip("datetime"), state="disabled")
        self.btn_strip_dt.grid(sticky="ew", pady=3)
        self.btn_strip_all = ttk.Button(self.left, text="Strip ALL metadata (re-encode to PNG)", command=self.action_strip_all, state="disabled")
        self.btn_strip_all.grid(sticky="ew", pady=3)

        ttk.Separator(self.left).grid(sticky="ew", pady=8)
        row = ttk.Frame(self.left); row.grid(sticky="ew")
        ttk.Label(row, text="Convert to: ").pack(side="left")
        self.fmt_var = tk.StringVar(value="PNG")
        ttk.OptionMenu(row, self.fmt_var, "PNG", "PNG", "JPEG", "WEBP").pack(side="left", padx=6)
        ttk.Button(row, text="Apply", command=self.action_convert, state="disabled").pack(side="left")
        self.btn_convert = row.winfo_children()[-1]  # to toggle enabled

        ttk.Separator(self.left).grid(sticky="ew", pady=8)
        self.btn_wm = ttk.Button(self.left, text='Add Watermark "SAFE"', command=self.action_watermark, state="disabled")
        self.btn_wm.grid(sticky="ew", pady=3)
        self.btn_map = ttk.Button(self.left, text="Open Map (if GPS)", command=self.action_open_map, state="disabled")
        self.btn_map.grid(sticky="ew", pady=3)

        # Tool availability info
        ttk.Label(self.left, text=f"ExifTool: {'OK' if EXIFTOOL else 'not found'}").grid(sticky="w", pady=(10,0))
        ttk.Label(self.left, text=f"rembg: {'OK' if REMBG_OK else 'not installed'}").grid(sticky="w")
        ttk.Label(self.left, text=f"HEIF: {'OK' if HEIF_OK else 'not installed'}").grid(sticky="w")

        # --- Right: preview + details ---
        self.preview = tk.Label(self.right, bg="#111827", fg="#e5e7eb", anchor="center")
        self.preview.grid(row=0, column=0, sticky="ew", pady=(0,8))

        info_row = ttk.Frame(self.right); info_row.grid(row=1, column=0, sticky="nsew")
        info_row.columnconfigure(0, weight=1); info_row.rowconfigure(0, weight=1)
        self.details = tk.Text(info_row, height=10, wrap="word")
        self.details.grid(row=0, column=0, sticky="nsew")

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, anchor="w").grid(row=1, column=0, columnspan=2, sticky="ew")

        self.right.bind("<Configure>", self._scale_preview)
        self._update_buttons()

    # -------- Shared helpers (single-image pipeline) --------
    def _set_from_bytes(self, data: bytes, ext_hint: str | None = None, status: str | None = None):
        """Make `data` the current image and refresh preview + EXIF."""
        self.current_bytes = data
        try:
            self.current_image = Image.open(BytesIO(data))
        except Exception as e:
            messagebox.showerror("Open", f"Cannot read image: {e}")
            self.current_image = None
            self.current_exif = {}
            self._update_buttons()
            return
        self.current_ext = ext_hint or self.current_ext
        self.current_exif = read_exif_from_bytes(self.current_bytes, self.current_ext)
        self._scale_preview(None)
        self._render_details()
        if status:
            self.status.set(status)
        self._update_buttons()

    def _render_details(self):
        ex = self.current_exif or {}
        summ = summarize_exif(ex) if ex else ExifSummary(None,None,None,None,None,None,[])
        lines = []
        if self.original_path:
            lines.append(f"File: {os.path.basename(self.original_path)}")
        if self.current_image:
            lines.append(f"Size: {self.current_image.width}×{self.current_image.height}")
        # short summary
        if summ.gps: lines.append(f"GPS: {summ.gps[0]:.6f}, {summ.gps[1]:.6f}")
        if summ.captured: lines.append(f"Captured: {summ.captured}")
        if summ.make: lines.append(f"Make: {summ.make}")
        if summ.model: lines.append(f"Model: {summ.model}")
        if summ.owner: lines.append(f"Owner: {summ.owner}")
        if summ.software: lines.append(f"Software: {summ.software}")
        if summ.serials: lines.append("Serials: " + ", ".join(summ.serials))
        if ex:
            lines.append("\nRaw EXIF (first 10k chars):\n" + json.dumps(ex, indent=2)[:10000])
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n".join(lines))

    def _scale_preview(self, event):
        w = self.right.winfo_width() or 700
        target_w = max(320, min(int(w * 0.95), 900))
        target_h = 320
        if self.current_image:
            im = self.current_image.copy()
            scale = min(target_w / im.width, target_h / im.height, 1.0)
            im = im.resize((max(1,int(im.width*scale)), max(1,int(im.height*scale))), Image.LANCZOS)
            tkimg = ImageTk.PhotoImage(im)
            self.preview.configure(image=tkimg, text="")
            self.preview.image = tkimg
        else:
            self.preview.configure(image="", text="(preview)", height=target_h)

    def _update_buttons(self):
        has_img = self.current_bytes is not None
        # Core
        self.btn_save["state"]   = "normal" if has_img else "disabled"
        self.btn_revert["state"] = "normal" if (self.original_bytes is not None and has_img) else "disabled"
        self.btn_safe["state"]   = "normal" if has_img else "disabled"
        # Tools
        self.btn_bg["state"]         = "normal" if (has_img and REMBG_OK) else "disabled"
        self.btn_strip_all["state"]  = "normal" if has_img else "disabled"
        self.btn_strip_gps["state"]  = "normal" if (has_img and EXIFTOOL) else "disabled"
        self.btn_strip_dev["state"]  = "normal" if (has_img and EXIFTOOL) else "disabled"
        self.btn_strip_dt["state"]   = "normal" if (has_img and EXIFTOOL) else "disabled"
        self.btn_wm["state"]         = "normal" if has_img else "disabled"
        self.btn_map["state"]        = "normal" if (has_img and summarize_exif(self.current_exif).gps) else "disabled"
        # Convert button:
        for child in self.left.winfo_children():
            if isinstance(child, ttk.Frame):
                for c in child.winfo_children():
                    if isinstance(c, ttk.Button) and c["text"] == "Apply":
                        c["state"] = "normal" if has_img else "disabled"

    # -------- Actions --------
    def action_open(self):
        fp = filedialog.askopenfilename(title="Open image",
            filetypes=[("Images","*.png;*.jpg;*.jpeg;*.webp;*.tif;*.tiff;*.bmp;*.heic;*.heif;*.avif"),("All","*.*")])
        if not fp: return
        try:
            raw = read_file_bytes(fp)
        except Exception as e:
            return messagebox.showerror("Open", str(e))
        self.original_path = fp
        self.original_bytes = raw
        # set ext hint from path
        _, ext = os.path.splitext(fp)
        self.current_ext = ext.lower() or ".jpg"
        self._set_from_bytes(raw, self.current_ext, status="Loaded.")

    def action_save(self):
        if not self.current_bytes: return
        fmt = (self.current_image.format or "").upper() if self.current_image else ""
        ext = ".png" if fmt == "PNG" else (".jpg" if fmt in ("JPG","JPEG") else ".png")
        out = filedialog.asksaveasfilename(title="Save current image", defaultextension=ext,
                                           filetypes=[("PNG","*.png"),("JPEG","*.jpg;*.jpeg"),("WEBP","*.webp"),("All","*.*")])
        if not out: return
        try:
            save_file_bytes(out, self.current_bytes)
            self.status.set(f"Saved: {os.path.basename(out)}")
        except Exception as e:
            messagebox.showerror("Save", str(e))

    def action_revert(self):
        if self.original_bytes is None: return
        self._set_from_bytes(self.original_bytes, os.path.splitext(self.original_path or "")[1].lower(), status="Reverted.")

    def action_open_map(self):
        ex = summarize_exif(self.current_exif or {})
        if not ex.gps:
            return messagebox.showinfo("Map", "No GPS data in the current image.")
        lat, lon = ex.gps
        webbrowser.open(f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")

    def action_remove_bg(self):
        if not REMBG_OK or not self.current_bytes:
            return
        try:
            if not hasattr(self, "_rembg_session"):
                self._rembg_session = rembg.new_session("u2net")
            out = rembg.remove(self.current_bytes, session=self._rembg_session)
            self._set_from_bytes(out, ".png", status="Background removed.")
        except Exception as e:
            messagebox.showerror("Remove Background", str(e))

    def action_strip_all(self):
        if not self.current_bytes:
            return
        try:
            img = open_image_any(self.current_bytes).convert("RGBA")
            self._set_from_bytes(to_png_bytes(img), ".png", status="Stripped ALL metadata (PNG re-encode)." )
        except Exception as e:
            messagebox.showerror("Strip ALL", str(e))

    def action_strip(self, kind: str):
        if not self.current_bytes:
            return
        if not EXIFTOOL:
            return messagebox.showwarning("ExifTool required", "Install/locate ExifTool to strip selectively.")
        try:
            args = build_strip_args(kind.split(","))
            out = exiftool_write_from_bytes(self.current_bytes, args, self.current_ext)
            self._set_from_bytes(out, self.current_ext, status=f"Stripped: {kind}." )
        except Exception as e:
            messagebox.showerror("Strip metadata", str(e))

    def action_convert(self):
        if not self.current_bytes: return
        fmt = (self.fmt_var.get() or "PNG").upper().replace("JPG","JPEG")
        try:
            img = open_image_any(self.current_bytes)
            if fmt == "JPEG":
                img = ensure_rgb(img)
            buf = BytesIO()
            img.save(buf, fmt)
            ext = ".jpeg" if fmt == "JPEG" else f".{fmt.lower()}"
            self._set_from_bytes(buf.getvalue(), ext, status=f"Converted to {fmt}." )
        except Exception as e:
            messagebox.showerror("Convert", str(e))

    def action_watermark(self):
        if not self.current_bytes: return
        try:
            img = open_image_any(self.current_bytes)
            img = add_watermark(img, "SAFE", opacity=160)
            self._set_from_bytes(to_png_bytes(img), ".png", status='Watermark "SAFE" added.' )
        except Exception as e:
            messagebox.showerror("Watermark", str(e))

    def action_safe(self):
        """SAFE pipeline with OPTION:
           - If 'strip-only' is checked → ONLY strip GPS/Device/Date (ExifTool), no WM/BG.
           - Otherwise → remove BG (if rembg) → strip (if ExifTool) → watermark.
           Result is set as the current image.
        """
        if not self.current_bytes: return
        strip_only = bool(self.var_striponly.get())

        if strip_only:
            if not EXIFTOOL:
                return messagebox.showwarning("ExifTool required", "Strip-only SAFE needs ExifTool.")
            try:
                args = build_strip_args(["gps","device","datetime"])
                data = exiftool_write_from_bytes(self.current_bytes, args, self.current_ext)
                self._set_from_bytes(data, self.current_ext, status="SAFE (strip-only) created and set as current.")
            except Exception as e:
                messagebox.showerror("SAFE (strip-only)", str(e))
            return

        # Full pipeline
        try:
            data = self.current_bytes
            ext_hint = self.current_ext
            if REMBG_OK:
                if not hasattr(self, "_rembg_session"):
                    self._rembg_session = rembg.new_session("u2net")
                data = rembg.remove(data, session=self._rembg_session)
                ext_hint = ".png"  # rembg returns PNG bytes
            if EXIFTOOL:
                args = build_strip_args(["gps","device","datetime"])
                data = exiftool_write_from_bytes(data, args, ext_hint)
            img = open_image_any(data)
            img = add_watermark(img, "SAFE", opacity=160)
            self._set_from_bytes(to_png_bytes(img), ".png", status="SAFE copy created (full) and set as current.")
        except Exception as e:
            messagebox.showerror("SAFE (full)", str(e))


if __name__ == "__main__":
    App().mainloop()
