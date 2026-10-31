"""
Pump It Up - FSR Driver (mini-driver de escritorio)
====================================================
Aplicacion de escritorio ligera (CustomTkinter + pyserial) que se comunica
por puerto serial con el firmware `FSR_PUMP_warrior.ino` (Arduino).

Funciones:
  - Visualiza en tiempo real los 5 sensores FSR (Centro, UL, UR, DL, DR)
    como barras verticales (valor analogRead 0-1023).
  - Muestra en cada barra la marca del umbral de activacion.
  - Permite ajustar el umbral de cada panel con un slider / campo numerico.
    Al soltar el slider o pulsar "Aplicar" se envia el comando serial al
    Arduino (cambio en vivo, sin recompilar). El boton "Guardar" envia los
    5 umbrales y los guarda en un JSON local para la proxima sesion.
  - Seleccion de puerto COM + boton Conectar/Desconectar.

PROTOCOLO SERIAL (115200 baud):
  Arduino -> PC:
      FSR:v1,v2,v3,v4,v5\n     valores analogicos de los 5 FSR
      THRESH:i=v\n             umbral actual del sensor i
      READY:...\n              anuncio de firmware al arrancar
      PONG\n                   respuesta a PING
      ERR:mensaje\n            error
  PC -> Arduino:
      SET,i,v\n                fija umbral del sensor i a v (0-1023)
      GET,i\n                  pide el umbral del sensor i
      GETALL\n                 pide los 5 umbrales
      PING\n                   comprobacion de conexion

INDICE DE LOS SENSORES (igual que el firmware):
  0 = CENTRO        (tecla w)
  1 = ABAJO-IZQ (DL)(tecla a)
  2 = ARRIBA-IZQ(UL)(tecla s)
  3 = ARRIBA-DER(UR)(tecla d)
  4 = ABAJO-DER (DR)(tecla x)

Dependencias:  pip install -r requirements.txt
Compilar a .exe:  ver build_exe.bat (Windows)
Compilar a binario Linux:  ver build_linux.sh
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

import customtkinter as ctk
import serial
import serial.tools.list_ports

# ------------------------------- Constantes -------------------------------
APP_TITLE = "Pump It Up - FSR Driver"
SERIAL_BAUD = 115200
POLL_MS = 16                      # ~60 fps de sondeo; solo redibuja si hay dato nuevo
SEND_DEBOUNCE_MS = 200            # envio coalescido al soltar el slider
ANALOG_MAX = 1023
UI_SYNC_GRACE_S = 1.0             # no pisar slider/entry que el usuario toca
HANDSHAKE_INTERVAL_MS = 800       # reintento de PING/GETALL mientras no responda
HANDSHAKE_MAX_TRIES = 6           # ~5 s antes de avisar "sin respuesta"
RESET_WAIT_MS = 2000              # espera al reset/re-enumeracion del Leonardo
DATA_STALE_S = 1.0                # aviso si el puerto sigue abierto pero no llegan FSR
RX_LABEL_INTERVAL_S = 0.2         # la linea RX es diagnostico: no hace falta a 60 fps
MAX_RX_BUFFER = 4096              # evita crecer sin limite si no llega un '\n'

# Orden, nombre, tecla y umbral inicial de cada sensor (igual que el firmware)
SENSORS = [
    {"name": "CENTRO",        "short": "C",  "key": "w"},
    {"name": "ABAJO-IZQ (DL)", "short": "DL", "key": "a"},
    {"name": "ARRIBA-IZQ (UL)", "short": "UL", "key": "s"},
    {"name": "ARRIBA-DER (UR)", "short": "UR", "key": "d"},
    {"name": "ABAJO-DER (DR)", "short": "DR", "key": "x"},
]
DEFAULT_THRESHOLDS = [120, 550, 200, 200, 450]   # CENTRO, DL, UL, UR, DR

COL_IDLE = "#2a9d8f"
COL_WARN = "#f4a261"
COL_HIT = "#e63946"
COL_TRACK = "#14171c"
COL_BORDER_IDLE = "#2d333d"
COL_BG = "#101216"
COL_SURFACE = "#181b21"
COL_SURFACE_ALT = "#222731"
COL_TEXT = "#e9edf2"
COL_MUTED = "#98a2ad"
COL_GRID = "#262b33"
FONT_FAMILY = "Segoe UI" if sys.platform.startswith("win") else "Noto Sans"


def app_dir() -> Path:
    """Directorio del binario/.exe (modo frozen) o del script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _is_linux() -> bool:
    return os.name == "posix" and not sys.platform.startswith("darwin")


def _port_permission_hint(exc) -> str:
    """Consejos extra para errores de puerto, solo en Linux."""
    if not _is_linux():
        return ""
    text = str(exc)
    if "Permission denied" in text or getattr(exc, "errno", None) == 13:
        return ("  > Permisos: anade tu usuario al grupo 'dialout' "
                "(Debian/Ubuntu), 'uucp' (Arch) o 'lock' (Fedora): "
                "sudo usermod -aG dialout $USER  y reinicia sesion. "
                "O instala la regla udev:  sudo cp udev/60-pump-fsr.rules "
                "/etc/udev/rules.d/ && sudo udevadm control --reload-rules && "
                "sudo udevadm trigger --subsystem-match=tty --action=change "
                "(desconecta y reconecta el USB).")
    if "ttyACM" in text:
        return ("  > Linux: si el puerto aparece y desaparece solo, "
                "ModemManager/brltty puede estar capturandolo "
                "(ver 'udev' en el README).")


CONFIG_FILE = app_dir() / "pump_driver_config.json"


# --------------------------- Barra de nivel (Canvas) ---------------------------
class LevelMeter(ctk.CTkFrame):
    """Barra vertical dibujada con Canvas: valor en vivo + marca de umbral.

    Los ítems se crean una sola vez y se actualizan in situ. Recrear el canvas
    completo en cada frame era la causa principal del lag de las barras.
    """

    def __init__(self, master, width=90, height=190, **kwargs):
        super().__init__(master, width=width, height=height, corner_radius=6,
                         fg_color=COL_TRACK, **kwargs)
        self.pack_propagate(False)
        self._base_width = width
        self._base_height = height

        self.canvas = tk.Canvas(self, width=width, height=height,
                                bg=COL_TRACK, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._value = 0
        self._threshold = 0
        self._active = False
        self._max = ANALOG_MAX
        self._last_key = None

        # Elementos estaticos (rejilla) y dinamicos (relleno + umbral).
        self._grid_items = [
            self.canvas.create_line(0, 0, 0, 0, fill=COL_GRID)
            for _ in range(3)
        ]
        self._fill_item = self.canvas.create_rectangle(
            0, 0, 0, 0, fill=COL_IDLE, outline="", state="hidden")
        self._threshold_item = self.canvas.create_line(
            0, 0, 0, 0, fill="#f1faee", width=2, dash=(4, 2))

        self.canvas.bind("<Configure>", self._on_configure)

    def _on_configure(self, _event=None):
        # El tamano cambio: hay que recalcular rejilla y coordenadas.
        self._last_key = None
        self.redraw()

    def set_value(self, value):
        self._value = max(0, min(self._max, int(value)))

    def set_threshold(self, th):
        self._threshold = max(0, min(self._max, int(th)))

    def set_active(self, active):
        self._active = bool(active)

    def redraw(self):
        w = int(self.canvas.winfo_width())
        h = int(self.canvas.winfo_height())
        if w <= 1:
            w = self._base_width
        if h <= 1:
            h = self._base_height

        key = (self._value, self._threshold, self._active, w, h)
        if key == self._last_key:
            return
        self._last_key = key

        # Escala: marcas cada 25%.
        for item, k in zip(self._grid_items, (1, 2, 3)):
            y = int(h - h * (k / 4.0))
            self.canvas.coords(item, 0, y, w, y)

        # Relleno proporcional (crece desde abajo).
        frac = self._value / float(self._max)
        fill_h = int(h * frac)
        if fill_h > 0:
            if self._active:
                color = COL_HIT
            elif frac >= 0.5:
                color = COL_WARN
            else:
                color = COL_IDLE
            self.canvas.coords(self._fill_item, 3, h - fill_h, w - 3, h - 3)
            self.canvas.itemconfigure(self._fill_item, fill=color, state="normal")
        else:
            self.canvas.itemconfigure(self._fill_item, state="hidden")

        # Marca del umbral de activacion.
        th_y = int(h - (self._threshold / float(self._max)) * h)
        th_y = max(2, min(h - 2, th_y))
        self.canvas.coords(self._threshold_item, 2, th_y, w - 2, th_y)


# --------------------------- Vista del panel (input viewer) ---------------------------
class PadViewer(ctk.CTkFrame):
    """Mini-panel tipo Pump It Up: cada cuadrito se rellena con la presion
    del sensor y se enciende (borde rojo) al superar su umbral.

    Igual que LevelMeter, los rectangulos y textos se crean una sola vez.
    """

    CELL_W, GAP = 64, 8
    VALUE_H = 12      # espacio reservado bajo cada celda para el valor
    MIN_CELL_H = 34
    MAX_CELL_H = 60

    def __init__(self, master, width=240, height=200, **kwargs):
        super().__init__(master, width=width, height=height, corner_radius=10,
                         border_width=1, border_color=COL_BORDER_IDLE,
                         fg_color=COL_SURFACE, **kwargs)
        self.pack_propagate(False)

        ctk.CTkLabel(self, text="INPUT VIEWER",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=11,
                                      weight="bold"),
                     text_color=COL_MUTED).pack(pady=(5, 0))

        cw, ch = width - 10, height - 38
        self.canvas = tk.Canvas(self, width=cw, height=ch, bg=COL_TRACK,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, pady=(1, 6))

        self._key_font = tkfont.Font(family=FONT_FAMILY, size=9, weight="bold")
        self._value_font = tkfont.Font(family=FONT_FAMILY, size=8)
        self._values = [0] * 5
        self._thresholds = list(DEFAULT_THRESHOLDS)
        self._cw, self._ch = cw, ch
        self.geo = {}

        # Crear los 25 items una sola vez (antes se recreaban en cada frame).
        self._cells = {}
        for idx in range(5):
            bg_item = self.canvas.create_rectangle(
                0, 0, 0, 0, fill=COL_SURFACE_ALT,
                outline=COL_BORDER_IDLE, width=2)
            fill_item = self.canvas.create_rectangle(
                0, 0, 0, 0, fill=COL_IDLE, outline="", state="hidden")
            th_item = self.canvas.create_line(
                0, 0, 0, 0, fill="#ffffff", width=1, dash=(3, 2))
            key_item = self.canvas.create_text(
                0, 0, text=SENSORS[idx]["key"], fill=COL_TEXT,
                font=self._key_font)
            value_item = self.canvas.create_text(
                0, 0, text="0", fill=COL_MUTED, font=self._value_font)
            self._cells[idx] = {
                "bg": bg_item, "fill": fill_item, "threshold": th_item,
                "key_text": key_item, "value_text": value_item,
                "value": -1, "th": -1, "pressed": None,
            }

        self._layout(cw, ch)
        self.canvas.bind("<Configure>", self._on_resize)

    def _layout(self, cw, ch):
        """Recalcula las celdas con el tamano real del canvas.

        La altura de celda se deriva del espacio disponible reservando una
        franja fija para el valor bajo cada fila. Asi el texto nunca queda
        cortado cuando la ventana se achica o el panel central se estira.
        """
        if cw <= 1 or ch <= 1:
            return

        cell_w = min(self.CELL_W, max(34, (cw - self.GAP - 8) // 2))
        cell_h = (ch - 3 * self.VALUE_H - 4) // 3
        cell_h = max(self.MIN_CELL_H, min(self.MAX_CELL_H, cell_h))

        pair_w = cell_w * 2 + self.GAP
        block_h = 3 * (cell_h + self.VALUE_H)
        x_left = max(2, (cw - pair_w) // 2)
        x_right = x_left + cell_w + self.GAP
        cx = max(2, (cw - cell_w) // 2)
        y_top = max(2, (ch - block_h) // 2)
        y_mid = y_top + cell_h + self.VALUE_H
        y_bot = y_mid + cell_h + self.VALUE_H
        value_y_off = self.VALUE_H // 2 + 1

        def rect(x, y):
            return x, y, x + cell_w, y + cell_h

        self.geo = {
            2: rect(x_left, y_top),    # ARRIBA-IZQ
            3: rect(x_right, y_top),   # ARRIBA-DER
            0: rect(cx, y_mid),        # CENTRO
            1: rect(x_left, y_bot),    # ABAJO-IZQ
            4: rect(x_right, y_bot),   # ABAJO-DER
        }
        for idx, (x0, y0, x1, y1) in self.geo.items():
            cell = self._cells[idx]
            self.canvas.coords(cell["bg"], x0, y0, x1, y1)
            self.canvas.coords(cell["fill"], x0 + 3, y1, x1 - 3, y1)
            self.canvas.coords(cell["threshold"], x0 + 2, y0 + 2, x1 - 2, y0 + 2)
            self.canvas.coords(cell["key_text"], (x0 + x1) / 2,
                               y0 + max(10, cell_h // 3))
            self.canvas.coords(cell["value_text"], (x0 + x1) / 2,
                               y1 + value_y_off)
            cell["value"] = -1
            cell["th"] = -1
            cell["pressed"] = None
        self.redraw()

    def _on_resize(self, event):
        cw, ch = int(event.width), int(event.height)
        if cw <= 1 or ch <= 1 or (cw, ch) == (self._cw, self._ch):
            return
        self._cw, self._ch = cw, ch
        self._layout(cw, ch)

    def set_values(self, values, thresholds):
        self._values = list(values)
        self._thresholds = list(thresholds)

    def redraw(self):
        for idx, cell in self._cells.items():
            value = self._values[idx]
            th = self._thresholds[idx]
            if value == cell["value"] and th == cell["th"]:
                continue

            frac = max(0.0, min(1.0, value / float(ANALOG_MAX)))
            pressed = value >= th
            x0, y0, x1, y1 = self.geo[idx]

            if pressed != cell["pressed"]:
                self.canvas.itemconfigure(
                    cell["bg"],
                    outline=COL_HIT if pressed else COL_BORDER_IDLE)
                cell["pressed"] = pressed

            fill_h = int((y1 - y0) * frac)
            if fill_h > 0:
                if pressed:
                    color = COL_HIT
                elif frac >= 0.5:
                    color = COL_WARN
                else:
                    color = COL_IDLE
                self.canvas.coords(cell["fill"], x0 + 3, y1 - fill_h,
                                   x1 - 3, y1 - 3)
                self.canvas.itemconfigure(cell["fill"], fill=color,
                                          state="normal")
            else:
                self.canvas.itemconfigure(cell["fill"], state="hidden")

            if th != cell["th"]:
                th_y = int(y1 - (th / float(ANALOG_MAX)) * (y1 - y0))
                th_y = max(y0 + 2, min(y1 - 2, th_y))
                self.canvas.coords(cell["threshold"], x0 + 2, th_y,
                                   x1 - 2, th_y)

            if value != cell["value"]:
                self.canvas.itemconfigure(cell["value_text"], text=str(value))

            cell["value"] = value
            cell["th"] = th


# --------------------------- Panel de un sensor ---------------------------
class PanelWidget(ctk.CTkFrame):
    def __init__(self, master, idx, on_slider, on_entry_apply):
        super().__init__(master, corner_radius=10, border_width=1,
                         border_color=COL_BORDER_IDLE, fg_color=COL_SURFACE)
        self.idx = idx
        self.info = SENSORS[idx]
        self.last_user_change = 0.0
        self._last_value_text = None
        self._last_pressed = False

        # Barra a la izquierda; controles a la derecha. Este layout compacto
        # deja visibles slider, campo y boton incluso en ventanas bajas.
        self.meter = LevelMeter(self, width=68, height=180)
        self.meter.pack(side="left", fill="y", padx=(10, 8), pady=10)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        ctk.CTkLabel(
            body, text=self.info["name"].split(" (")[0], anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COL_TEXT).pack(fill="x")
        ctk.CTkLabel(
            body, text=f"tecla {self.info['key'].upper()}", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=9),
            text_color=COL_MUTED).pack(fill="x")

        self.state_label = ctk.CTkLabel(
            body, text="libre", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COL_IDLE)
        self.state_label.pack(fill="x", pady=(3, 0))

        self.value_label = ctk.CTkLabel(
            body, text="0 / 0", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COL_TEXT)
        self.value_label.pack(fill="x", pady=(4, 0))

        self.slider = ctk.CTkSlider(
            body, from_=0, to=ANALOG_MAX, number_of_steps=ANALOG_MAX,
            command=lambda v: on_slider(self, int(v)),
            progress_color=COL_IDLE, button_color=COL_IDLE,
            button_hover_color="#35b8a8", fg_color=COL_TRACK, height=12)
        self.slider.pack(fill="x", pady=(10, 5))

        entry_row = ctk.CTkFrame(body, fg_color="transparent")
        entry_row.pack(fill="x")
        self.entry = ctk.CTkEntry(
            entry_row, width=52, height=26, justify="center",
            fg_color=COL_TRACK, border_color=COL_BORDER_IDLE,
            text_color=COL_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: on_entry_apply(self))
        self.apply_btn = ctk.CTkButton(
            entry_row, text="Aplicar", width=54, height=26,
            command=lambda: on_entry_apply(self),
            fg_color=COL_SURFACE_ALT, hover_color="#2c333e",
            text_color=COL_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=9))
        self.apply_btn.pack(side="left", padx=(4, 0))

    def update_live(self, value, threshold, pressed):
        """Actualiza la parte viva del panel evitando redibujados innecesarios."""
        self.meter.set_value(value)
        self.meter.set_threshold(threshold)
        self.meter.set_active(pressed)
        self.meter.redraw()

        text = f"{value} / {threshold}"
        if text != self._last_value_text:
            self.value_label.configure(text=text)
            self._last_value_text = text

        if pressed != self._last_pressed:
            self.configure(border_color=COL_HIT if pressed else COL_BORDER_IDLE)
            self.state_label.configure(
                text="PISADO" if pressed else "libre",
                text_color=COL_HIT if pressed else COL_IDLE)
            self._last_pressed = pressed


# ------------------------------- Aplicacion -------------------------------
class FSRDriverApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1040x740")
        self.minsize(900, 640)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Estado compartido con el hilo de lectura
        self.values = [0] * 5
        self.thresholds = list(DEFAULT_THRESHOLDS)
        self.data_lock = threading.Lock()

        # Conexion serial
        self.serial_port = None
        self.serial_lock = threading.Lock()
        self.running = threading.Event()
        self.reader_thread = None
        self.connected = False

        # Cola de mensajes para la GUI y ultima linea serial cruda
        self.status_queue = queue.Queue()
        self.last_line = ""

        # Handshake: confirma que el Arduino responde con el protocolo nuevo
        self._confirmed = False
        self._handshake_tries = 0
        self._handshake_failed = False
        self._request_sync = False
        self._needs_reopen = False
        self._port_map = {}  # etiqueta del combo -> dispositivo real

        # Apertura/reconexion no bloqueante (el Leonardo tarda ~2 s en reiniciar)
        self._connecting = False
        self._pending_serial = None
        self._connect_after_id = None

        # Versionado: la GUI solo redibuja cuando llega un dato nuevo o cambia
        # un umbral. Evita repintar lo mismo a 60 fps.
        self._data_version = 0
        self._threshold_version = 0
        self._last_version = None
        self._last_fsr_monotonic = 0.0
        self._data_stale = False
        self._last_rx_update = 0.0
        self._last_rx_text = None
        self._log_text = None
        self._status_text = None
        self._status_color = None

        # Envio coalescido de umbrales (evita spam al arrastrar el slider)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._flush_timer = None

        self._build_ui()
        self._load_config()
        self.refresh_ports()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._poll)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.configure(fg_color=COL_BG)

        # Cabecera: identidad de la app + estado de conexion visible.
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(10, 2))
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")
        ctk.CTkLabel(title_box, text="PUMP FSR",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=18,
                                      weight="bold"),
                     text_color=COL_TEXT).pack(anchor="w")
        ctk.CTkLabel(title_box, text="Monitor y calibracion de los 5 sensores",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=10),
                     text_color=COL_MUTED).pack(anchor="w")

        self.status_label = ctk.CTkLabel(
            header, text="\u25cf Desconectado",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=COL_HIT)
        self.status_label.pack(side="right", padx=(8, 2))
        self._status_text = "\u25cf Desconectado"
        self._status_color = COL_HIT

        # Barra de conexion + acciones globales en una sola fila compacta.
        top = ctk.CTkFrame(self, fg_color=COL_SURFACE, corner_radius=10,
                           border_width=1, border_color=COL_BORDER_IDLE)
        top.pack(fill="x", padx=16, pady=(8, 6))

        ctk.CTkLabel(top, text="Puerto",
                     font=ctk.CTkFont(family=FONT_FAMILY, size=10),
                     text_color=COL_MUTED).pack(side="left", padx=(10, 3),
                                                pady=8)
        self.port_var = ctk.StringVar(value="")
        self.port_combo = ctk.CTkComboBox(
            top, variable=self.port_var, values=[], width=170,
            state="readonly", fg_color=COL_TRACK,
            border_color=COL_BORDER_IDLE, button_color=COL_SURFACE_ALT,
            button_hover_color="#2c333e", dropdown_fg_color=COL_SURFACE,
            dropdown_hover_color=COL_SURFACE_ALT, text_color=COL_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10))
        self.port_combo.pack(side="left", padx=3, pady=8)

        self.refresh_btn = ctk.CTkButton(
            top, text="Refrescar", width=72,
            command=self.refresh_ports, fg_color=COL_SURFACE_ALT,
            hover_color="#2c333e", text_color=COL_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10))
        self.refresh_btn.pack(side="left", padx=3, pady=8)

        self.connect_btn = ctk.CTkButton(
            top, text="Conectar", width=104,
            command=self.toggle_connect, fg_color=COL_IDLE,
            hover_color="#238276", text_color="#08110f",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"))
        self.connect_btn.pack(side="left", padx=(10, 3), pady=8)

        ctk.CTkButton(top, text="Guardar", width=76,
                      command=self.send_all_thresholds,
                      fg_color=COL_SURFACE_ALT, hover_color="#2c333e",
                      text_color=COL_TEXT,
                      font=ctk.CTkFont(family=FONT_FAMILY, size=10)).pack(
                          side="right", padx=(3, 10), pady=8)
        ctk.CTkButton(top, text="Sincronizar", width=92,
                      command=self.request_all_thresholds,
                      fg_color=COL_SURFACE_ALT, hover_color="#2c333e",
                      text_color=COL_TEXT,
                      font=ctk.CTkFont(family=FONT_FAMILY, size=10)).pack(
                          side="right", padx=3, pady=8)

        # Rejilla de paneles (distribucion tipo Pump It Up)
        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="both", expand=True, padx=12, pady=4)

        layout = [
            (2, 0, 0),   # ARRIBA-IZQ (UL) -> fila 0, col 0
            (3, 0, 2),   # ARRIBA-DER (UR) -> fila 0, col 2
            (1, 1, 0),   # ABAJO-IZQ (DL) -> fila 1, col 0
            (0, 1, 1),   # CENTRO         -> fila 1, col 1
            (4, 1, 2),   # ABAJO-DER (DR) -> fila 1, col 2
        ]
        self.panels = {}
        for idx, row, col in layout:
            panel = PanelWidget(grid, idx, self.on_slider_change,
                                self.on_entry_apply)
            panel.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            self.panels[idx] = panel

        # Input viewer (mini-panel tipo Pump It Up) en el espacio central superior
        self.pad_viewer = PadViewer(grid)
        self.pad_viewer.grid(row=0, column=1, padx=6, pady=6, sticky="nsew")

        for col in range(3):
            grid.grid_columnconfigure(col, weight=1)
        grid.grid_rowconfigure(0, weight=1)
        grid.grid_rowconfigure(1, weight=1)

        # Barra inferior: log + ultima linea serial.
        bottom = ctk.CTkFrame(self, fg_color=COL_SURFACE, corner_radius=10,
                              border_width=1, border_color=COL_BORDER_IDLE)
        bottom.pack(fill="x", padx=16, pady=(4, 14))
        self.log_label = ctk.CTkLabel(
            bottom, text="Listo", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COL_TEXT)
        self.log_label.pack(fill="x", padx=12, pady=(8, 0))
        self.rx_label = ctk.CTkLabel(
            bottom, text="RX: ---", anchor="w", text_color=COL_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10))
        self.rx_label.pack(fill="x", padx=12, pady=(0, 8))
        self._log_text = "Listo"
        self._last_rx_text = "RX: ---"

    # -------------------------------------------------------------- Puertos
    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        if _is_linux():
            # En Linux el kernel siempre lista la serie del SoC (/dev/ttyS*,
            # /dev/ttyAMA*, ...) que nunca es el Arduino: se ocultan para que
            # el combo muestre solo candidatos reales (ttyACM0/ttyUSB0...).
            ports = [p for p in ports
                     if not p.device.startswith(
                         ("/dev/ttyS", "/dev/ttyLP", "/dev/ttyAMA",
                          "/dev/ttyTHS", "/dev/ttyprintk"))]
        self._port_map = {}
        labels = []
        for p in ports:
            desc = (p.description or "").split("(")[0].strip()
            label = p.device if not desc else f"{p.device} - {desc}"
            self._port_map[label] = p.device
            labels.append(label)
        self.port_combo.configure(values=labels)
        if labels:
            if self.port_var.get() not in labels:
                self.port_var.set(labels[0])
        else:
            self.port_var.set("")
        self.status_queue.put(f"{len(ports)} puerto(s) detectado(s)")

    def _selected_device(self):
        """Devuelve el nombre real del COM a partir de la etiqueta elegida."""
        label = self.port_var.get()
        if not label:
            return ""
        return self._port_map.get(label, label.split(" - ")[0])

    # ------------------------------------------------------- Conectar/cerrar
    def toggle_connect(self):
        if self.connected or self._connecting:
            self.disconnect()
        else:
            self.connect()

    def _open_serial(self, port):
        """Abre el puerto estilo IDE: DTR asertado (el Leonardo lo necesita)."""
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = SERIAL_BAUD
        ser.timeout = 0.2
        ser.dtr = True
        ser.rts = False
        ser.open()
        return ser

    def connect(self):
        if self.connected or self._connecting:
            return
        port = self._selected_device()
        if not port:
            self.status_queue.put("Selecciona un puerto COM.")
            return
        try:
            ser = self._open_serial(port)
        except Exception as exc:  # puerto ocupado / no existe / sin permisos
            hint = _port_permission_hint(exc)
            self.status_queue.put(f"Error al conectar: {exc}{hint}")
            return
        self._begin_connect(ser, port)

    def _begin_connect(self, ser, port, reconnect=False):
        """Abre el puerto y espera el reset SIN bloquear el hilo de Tk."""
        self._connecting = True
        self._pending_serial = ser
        self.connected = False
        self._confirmed = False
        self._handshake_tries = 0
        self._handshake_failed = False
        self._request_sync = False
        self._data_stale = False
        self.connect_btn.configure(text="Conectando...", state="disabled")
        self._set_status(
            "\u25cf Reconectando..." if reconnect else "\u25cf Conectando...",
            COL_WARN)
        self.status_queue.put(
            f"{'Reabriendo' if reconnect else 'Abriendo'} {port} @ "
            f"{SERIAL_BAUD} baud. Esperando reinicio del Arduino...")
        self._connect_after_id = self.after(
            RESET_WAIT_MS, lambda: self._finish_connect(ser, port))

    def _finish_connect(self, ser, port):
        self._connect_after_id = None
        if not self._connecting or ser is not self._pending_serial:
            self._close_quietly(ser)
            return
        try:
            ser.reset_input_buffer()  # descarta la basura del arranque
        except Exception as exc:
            hint = _port_permission_hint(exc)
            self.status_queue.put(
                f"No se pudo preparar {port}: {exc}{hint}")
            self._close_quietly(ser)
            self._pending_serial = None
            self._connecting = False
            self._reset_connection_ui()
            return

        with self.serial_lock:
            self.serial_port = ser
        self._pending_serial = None
        self._connecting = False
        self.connected = True
        self._confirmed = False
        self._handshake_tries = 0
        self._handshake_failed = False
        self._data_stale = False
        self._last_fsr_monotonic = time.monotonic()
        self._last_version = None  # fuerza un primer render con el estado actual
        self.running.set()
        self.reader_thread = threading.Thread(target=self._reader, args=(ser,),
                                              daemon=True)
        self.reader_thread.start()

        self.connect_btn.configure(text="Desconectar", state="normal")
        self._set_status("\u25cf Conectado (detectando...)", COL_WARN)
        self.status_queue.put(f"Conectado a {port} @ {SERIAL_BAUD} baud. "
                              f"Detectando Arduino...")
        self._send("PING\n")
        self._send("GETALL\n")
        self.after(HANDSHAKE_INTERVAL_MS, self._handshake_tick)

    def _handshake_tick(self):
        """Reenvia PING/GETALL hasta confirmar que el Arduino responde."""
        if not self.connected or self._confirmed or self._connecting:
            return
        self._handshake_tries += 1
        if self._handshake_tries >= HANDSHAKE_MAX_TRIES:
            self._handshake_failed = True
            self._set_status("\u25cf Conectado (sin respuesta)", COL_HIT)
            tips = (
                "1) firmware NUEVO cargado (FSR_PUMP_warrior, 115200 baud), "
                "2) Serial Monitor / IDE cerrados, "
                "3) cable USB de datos (no de solo carga), "
                "4) el puerto sea el correcto (pulsa Refrescar si cambio)"
            )
            if _is_linux():
                tips += (", "
                         "5) tu usuario tenga permisos sobre /dev/ttyACM* "
                         "(grupo dialout/uucp o regla udev, ver README)")
            self.status_queue.put(
                f"Sin respuesta del Arduino en {self.port_var.get()}. "
                f"Revisa: {tips}.")
            return
        self._send("PING\n")
        self._send("GETALL\n")
        self.after(HANDSHAKE_INTERVAL_MS, self._handshake_tick)

    def _mark_confirmed(self):
        """Primera linea valida recibida: protocolo activo detectado.
        SOLO toca flags/colas (se llama desde el hilo de lectura;
        cualquier acceso a Tk aqui crashea ese hilo)."""
        if self._confirmed:
            return
        self._confirmed = True
        self._handshake_failed = False
        self._last_fsr_monotonic = time.monotonic()
        self._data_stale = False
        self.status_queue.put("Arduino detectado: protocolo activo (115200).")
        self._request_sync = True  # se consume en el hilo principal (_poll)

    def _close_quietly(self, ser):
        if ser is None:
            return
        try:
            ser.close()
        except Exception:
            pass

    def _teardown_serial(self):
        """Cierra el puerto y espera brevemente al lector, sin sleeps largos."""
        self.running.clear()
        if self._connect_after_id is not None:
            try:
                self.after_cancel(self._connect_after_id)
            except Exception:
                pass
            self._connect_after_id = None
        pending = self._pending_serial
        self._pending_serial = None
        with self.serial_lock:
            ser = self.serial_port
            self.serial_port = None
        # Cerrar primero desbloquea el read() del hilo lector.
        self._close_quietly(pending)
        self._close_quietly(ser)
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=0.3)
            self.reader_thread = None

    def _reset_connection_ui(self):
        self.connect_btn.configure(text="Conectar", state="normal")
        self._set_status("\u25cf Desconectado", COL_HIT)

    def _reopen(self):
        """Cierra y reabre el puerto (el Leonardo puede re-enumerar el USB)."""
        if self._connecting:
            return
        self.running.clear()
        with self.serial_lock:
            old = self.serial_port
            self.serial_port = None
        self._close_quietly(old)
        self.connected = False
        self._confirmed = False

        # recarga la lista: el COM pudo haber cambiado tras re-enumerar
        self.refresh_ports()
        port = self._selected_device()
        if not port:
            self.status_queue.put(
                "El Arduino no reaparece en ningun puerto. Revisa el cable USB.")
            self._drop_connection()
            return

        try:
            new_ser = self._open_serial(port)
        except Exception as exc:
            hint = _port_permission_hint(exc)
            self.status_queue.put(f"No se pudo reconectar a {port}: {exc}{hint}")
            self._drop_connection()
            return

        self._begin_connect(new_ser, port, reconnect=True)

    def _drop_connection(self):
        """Fuerza el estado desconectado sin tocar la lista de puertos."""
        self._teardown_serial()
        self.connected = False
        self._connecting = False
        self._confirmed = False
        self._handshake_failed = False
        self._data_stale = False
        self._reset_connection_ui()

    def disconnect(self):
        self._teardown_serial()
        self.connected = False
        self._connecting = False
        self._confirmed = False
        self._handshake_failed = False
        self._data_stale = False
        self._reset_connection_ui()
        self.status_queue.put("Desconectado")

    def _on_close(self):
        self.disconnect()
        self.destroy()

    # ------------------------------------------------------------- Serial
    def _send(self, text):
        with self.serial_lock:
            ser = self.serial_port
        if ser is None:
            return False
        try:
            ser.write(text.encode())
            return True
        except Exception:
            self.status_queue.put("Error de escritura serial.")
            return False

    def _reader(self, ser):
        buf = ""
        while self.running.is_set():
            # Si el puerto se cerro/reemplazo, este lector ya no es el dueno.
            with self.serial_lock:
                if self.serial_port is not ser:
                    break
            try:
                data = ser.read(256)
            except Exception as exc:
                if not self.running.is_set():
                    break
                with self.serial_lock:
                    if self.serial_port is not ser:
                        break
                self.status_queue.put(
                    f"Error de lectura ({exc}). Reintentando conexion...")
                self._needs_reopen = True
                break
            if not data:
                continue
            try:
                buf += data.decode(errors="ignore")
            except Exception:
                buf = ""
            # Linea corrupta sin '\n': no dejar crecer el buffer sin limite.
            if len(buf) > MAX_RX_BUFFER:
                buf = buf[-MAX_RX_BUFFER:]
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip("\r").strip()
                if line:
                    self._handle_line(line)

    def _handle_line(self, line):
        self.last_line = line
        try:
            if line.startswith("FSR:"):
                self._mark_confirmed()
                parts = line[4:].split(",")
                vals = []
                for part in parts[:5]:
                    try:
                        vals.append(max(0, min(ANALOG_MAX, int(part))))
                    except ValueError:
                        vals.append(0)
                while len(vals) < 5:
                    vals.append(0)
                with self.data_lock:
                    self.values = vals
                self._last_fsr_monotonic = time.monotonic()
                self._data_stale = False
                self._data_version += 1

            elif line.startswith("THRESH:"):
                self._mark_confirmed()
                body = line[7:]
                if "=" in body:
                    idx_str, val_str = body.split("=", 1)
                    idx, value = int(idx_str), int(val_str)
                    if 0 <= idx < 5:
                        value = max(0, min(ANALOG_MAX, value))
                        with self.data_lock:
                            self.thresholds[idx] = value
                        self._threshold_version += 1
                        self.status_queue.put(
                            f"Arduino: umbral sensor {SENSORS[idx]['short']} = {value}")

            elif line == "PONG":
                self._mark_confirmed()
                self.status_queue.put("Arduino respondio (PONG).")

            elif line.startswith("READY:"):
                self._mark_confirmed()
                self.status_queue.put(f"Firmware detectado: {line[6:]}")

            elif line.startswith("ERR:"):
                self.status_queue.put(f"Arduino: {line}")

        except (ValueError, IndexError):
            pass  # linea corrupta / parcial: se ignora

    # ----------------------------------------------- Umbrales (slider/entry)
    def on_slider_change(self, panel, value):
        panel.last_user_change = time.monotonic()
        with self.data_lock:
            self.thresholds[panel.idx] = value
        self._threshold_version += 1
        text = str(value)
        if panel.entry.get() != text:
            panel.entry.delete(0, "end")
            panel.entry.insert(0, text)
        with self._pending_lock:
            self._pending[panel.idx] = value
        self._schedule_flush()

    def on_entry_apply(self, panel):
        panel.last_user_change = time.monotonic()
        value = self._entry_int(panel.entry)
        if value is None:
            return
        value = max(0, min(ANALOG_MAX, value))
        panel.slider.set(value)
        with self.data_lock:
            self.thresholds[panel.idx] = value
        self._threshold_version += 1
        self._send(f"SET,{panel.idx},{value}\n")
        self.status_queue.put(
            f"Enviado: sensor {SENSORS[panel.idx]['short']} umbral = {value}")

    def _schedule_flush(self):
        # Se ejecuta en el hilo de Tk: no crear un threading.Timer por rafaga.
        if self._flush_timer is None:
            self._flush_timer = self.after(SEND_DEBOUNCE_MS,
                                           self._flush_pending)

    def _flush_pending(self):
        self._flush_timer = None
        with self._pending_lock:
            pending = dict(self._pending)
            self._pending.clear()
        for idx, value in pending.items():
            if self._send(f"SET,{idx},{value}\n"):
                self.status_queue.put(
                    f"Enviado: sensor {SENSORS[idx]['short']} umbral = {value}")

    # --------------------------------------------- Acciones globales
    def send_all_thresholds(self):
        with self.data_lock:
            thresholds = list(self.thresholds)
        if self.connected:
            for idx, value in enumerate(thresholds):
                self._send(f"SET,{idx},{value}\n")
            self.status_queue.put("Umbrales enviados al Arduino.")
        else:
            self.status_queue.put("Sin conexion: solo guardados en local.")
        self._save_config()

    def request_all_thresholds(self):
        if not self.connected:
            return
        self._send("GETALL\n")
        self.status_queue.put("Solicitando umbrales al Arduino...")

    # -------------------------------------------------- Config local (JSON)
    def _load_config(self):
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            th = data.get("thresholds")
            if isinstance(th, list) and len(th) == 5:
                with self.data_lock:
                    self.thresholds = [max(0, min(ANALOG_MAX, int(x))) for x in th]
                self._threshold_version += 1
        except Exception:
            pass  # primera ejecucion: se usan los valores por defecto

    def _save_config(self):
        try:
            with self.data_lock:
                thresholds = list(self.thresholds)
            CONFIG_FILE.write_text(
                json.dumps({"thresholds": thresholds}, indent=2),
                encoding="utf-8")
        except Exception as exc:
            self.status_queue.put(f"No se pudo guardar la configuracion: {exc}")

    # --------------------------------------------------- Bucle de refresco
    def _set_status(self, text, color):
        if text == self._status_text and color == self._status_color:
            return
        self.status_label.configure(text=text, text_color=color)
        self._status_text = text
        self._status_color = color

    def _set_log(self, text):
        if text != self._log_text:
            self.log_label.configure(text=text)
            self._log_text = text

    def _sync_threshold_controls(self, now, thresholds):
        """Replica cambios de umbral del Arduino sin pisar al usuario."""
        for idx, panel in self.panels.items():
            th = thresholds[idx]
            if now - panel.last_user_change <= UI_SYNC_GRACE_S:
                continue
            if int(round(panel.slider.get())) != th:
                panel.slider.set(th)
            if panel.entry.focus_get() is not panel.entry:
                if self._entry_int(panel.entry) != th:
                    panel.entry.delete(0, "end")
                    panel.entry.insert(0, str(th))

    def _poll(self):
        started = time.perf_counter()

        # Drena la cola y muestra solo el ultimo mensaje (evita N redraws).
        msg = None
        while True:
            try:
                msg = self.status_queue.get_nowait()
            except queue.Empty:
                break
        if msg is not None:
            self._set_log(msg)

        # Si el firmware se detecto recien, sincroniza los umbrales.
        if self._request_sync and self.connected:
            self._request_sync = False
            self.request_all_thresholds()

        # Si el puerto murio (Leonardo re-enumero el USB), reconectar.
        if self._needs_reopen and self.connected:
            self._needs_reopen = False
            self._reopen()

        now = time.monotonic()

        # Vigilancia de datos: el puerto puede seguir abierto sin FSR.
        if (self.connected and self._confirmed and not self._data_stale
                and now - self._last_fsr_monotonic > DATA_STALE_S):
            self._data_stale = True
            self._set_status("\u25cf Conectado (sin datos)", COL_WARN)
            self._set_log(
                "Sin datos FSR durante mas de 1 s; el puerto sigue abierto.")

        if (self.connected and self._confirmed and not self._data_stale
                and not self._handshake_failed):
            self._set_status("\u25cf Conectado", COL_IDLE)

        # Redibujar solo cuando llego un FSR nuevo o cambio un umbral.
        version = (self._data_version, self._threshold_version)
        if version != self._last_version:
            self._last_version = version
            with self.data_lock:
                values = list(self.values)
                thresholds = list(self.thresholds)
            for idx, panel in self.panels.items():
                value = values[idx]
                th = thresholds[idx]
                panel.update_live(value, th, value >= th)
            self.pad_viewer.set_values(values, thresholds)
            self.pad_viewer.redraw()
            self._sync_threshold_controls(now, thresholds)

        # La linea RX es diagnostico: basta con ~5 Hz.
        if now - self._last_rx_update >= RX_LABEL_INTERVAL_S:
            text = f"RX: {self.last_line[:90]}"
            if text != self._last_rx_text:
                self.rx_label.configure(text=text)
                self._last_rx_text = text
            self._last_rx_update = now

        # Temporizador de frecuencia fija: descuenta el trabajo del frame.
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.after(max(1, int(POLL_MS - elapsed_ms)), self._poll)

    @staticmethod
    def _entry_int(entry):
        text = entry.get().strip()
        try:
            return int(text)
        except ValueError:
            return None


def main():
    app = FSRDriverApp()
    app.mainloop()


if __name__ == "__main__":
    main()
