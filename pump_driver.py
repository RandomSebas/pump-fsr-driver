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
Compilar a .exe:  ver build_exe.bat
"""

import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import customtkinter as ctk
import serial
import serial.tools.list_ports

# ------------------------------- Constantes -------------------------------
APP_TITLE = "Pump It Up - FSR Driver"
SERIAL_BAUD = 115200
POLL_MS = 30                      # refresco de la GUI (~33 fps)
SEND_DEBOUNCE_MS = 200            # envio coalescido al soltar el slider
ANALOG_MAX = 1023
UI_SYNC_GRACE_S = 1.0             # no pisar slider/entry que el usuario toca
HANDSHAKE_INTERVAL_MS = 800       # reintento de PING/GETALL mientras no responda
HANDSHAKE_MAX_TRIES = 6           # ~5 s antes de avisar "sin respuesta"

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
COL_TRACK = "#1a1a1a"
COL_BORDER_IDLE = "#3a3a3a"


def app_dir() -> Path:
    """Directorio del .exe (modo frozen) o del script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_FILE = app_dir() / "pump_driver_config.json"


# --------------------------- Barra de nivel (Canvas) ---------------------------
class LevelMeter(ctk.CTkFrame):
    """Barra vertical dibujada con Canvas: valor en vivo + marca de umbral."""

    def __init__(self, master, width=90, height=190, **kwargs):
        super().__init__(master, width=width, height=height, corner_radius=6,
                         fg_color=COL_TRACK, **kwargs)
        self.pack_propagate(False)
        self.canvas = tk.Canvas(self, width=width, height=height,
                                bg=COL_TRACK, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._value = 0
        self._threshold = 0
        self._active = False
        self._max = ANALOG_MAX

    def set_value(self, value):
        self._value = max(0, min(self._max, int(value)))

    def set_threshold(self, th):
        self._threshold = max(0, min(self._max, int(th)))

    def set_active(self, active):
        self._active = bool(active)

    def redraw(self):
        c = self.canvas
        c.delete("all")
        w = int(c.winfo_width()) or 90
        h = int(c.winfo_height()) or 190

        # escala: marcas cada 25%
        for k in (1, 2, 3):
            y = int(h - h * (k / 4.0))
            c.create_line(0, y, w, y, fill="#2b2b2b")

        # relleno proporcional (crece desde abajo)
        frac = self._value / float(self._max)
        fill_h = int(h * frac)
        if fill_h > 0:
            if self._active:
                color = COL_HIT
            elif frac >= 0.5:
                color = COL_WARN
            else:
                color = COL_IDLE
            c.create_rectangle(3, h - fill_h, w - 3, h - 3,
                               fill=color, outline="")

        # marca del umbral de activacion
        th_y = int(h - (self._threshold / float(self._max)) * h)
        th_y = max(2, min(h - 2, th_y))
        c.create_line(2, th_y, w - 2, th_y, fill="#f1faee",
                      width=2, dash=(4, 2))


# --------------------------- Vista del panel (input viewer) ---------------------------
class PadViewer(ctk.CTkFrame):
    """Mini-panel tipo Pump It Up: cada cuadrito se rellena con la presion
    del sensor y se enciende (borde rojo) al superar su umbral."""

    CELL_W, CELL_H, GAP = 78, 64, 10

    def __init__(self, master, width=280, height=280, **kwargs):
        super().__init__(master, width=width, height=height, corner_radius=10,
                         border_width=1, border_color=COL_BORDER_IDLE,
                         fg_color="#242424", **kwargs)
        self.pack_propagate(False)

        ctk.CTkLabel(self, text="INPUT VIEWER",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(6, 0))

        cw, ch = width - 10, height - 36
        self.canvas = tk.Canvas(self, width=cw, height=ch, bg="#161616",
                                highlightthickness=0, bd=0)
        self.canvas.pack(pady=(2, 6))

        # geometria de cada celda (indice sensor -> rectangulo)
        pair_w = self.CELL_W * 2 + self.GAP
        x_left = (cw - pair_w) // 2
        x_right = x_left + self.CELL_W + self.GAP
        cx = (cw - self.CELL_W) // 2
        y_top, y_mid, y_bot = 6, 6 + self.CELL_H + self.GAP, 6 + (self.CELL_H + self.GAP) * 2

        def rect(x, y):
            return x, y, x + self.CELL_W, y + self.CELL_H

        self.geo = {
            2: rect(x_left, y_top),    # ARRIBA-IZQ
            3: rect(x_right, y_top),   # ARRIBA-DER
            0: rect(cx, y_mid),        # CENTRO
            1: rect(x_left, y_bot),    # ABAJO-IZQ
            4: rect(x_right, y_bot),   # ABAJO-DER
        }

        self._values = [0] * 5
        self._thresholds = [0] * 5

    def set_values(self, values, thresholds):
        self._values = list(values)
        self._thresholds = list(thresholds)

    def redraw(self):
        c = self.canvas
        c.delete("all")

        for idx, (x0, y0, x1, y1) in self.geo.items():
            value = self._values[idx]
            th = self._thresholds[idx]
            frac = max(0.0, min(1.0, value / float(self._max)))
            pressed = value >= th

            # fondo de la celda
            c.create_rectangle(x0, y0, x1, y1, fill="#1e1e1e",
                               outline=COL_HIT if pressed else "#444444", width=2)

            # relleno proporcional (sube desde abajo)
            fill_h = int((y1 - y0) * frac)
            if fill_h > 0:
                if pressed:
                    color = COL_HIT
                elif frac >= 0.5:
                    color = COL_WARN
                else:
                    color = COL_IDLE
                c.create_rectangle(x0 + 3, y1 - fill_h, x1 - 3, y1 - 3,
                                   fill=color, outline="")

            # marca de umbral dentro de la celda
            th_y = int(y1 - (th / float(self._max)) * (y1 - y0))
            th_y = max(y0 + 2, min(y1 - 2, th_y))
            c.create_line(x0 + 2, th_y, x1 - 2, th_y, fill="#ffffff",
                          width=1, dash=(3, 2))

            # tecla asignada
            c.create_text((x0 + x1) / 2, y0 + 13,
                          text=SENSORS[idx]["key"], fill="#e8e8e8",
                          font=("Segoe UI", 11, "bold"))
            # valor numerico bajo la celda
            c.create_text((x0 + x1) / 2, y1 + 12,
                          text=str(value), fill="#bbbbbb",
                          font=("Segoe UI", 9))

    _max = ANALOG_MAX


# --------------------------- Panel de un sensor ---------------------------
class PanelWidget(ctk.CTkFrame):
    def __init__(self, master, idx, on_slider, on_entry_apply):
        super().__init__(master, corner_radius=10, border_width=2,
                         border_color=COL_BORDER_IDLE, fg_color="#242424")
        self.idx = idx
        self.info = SENSORS[idx]
        self.last_user_change = 0.0

        ctk.CTkLabel(self, text=f"{self.info['name']}  [{self.info['key']}]",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(8, 0))
        self.state_label = ctk.CTkLabel(self, text="libre",
                                        font=ctk.CTkFont(size=11),
                                        text_color=COL_IDLE)
        self.state_label.pack()

        self.meter = LevelMeter(self, width=88, height=180)
        self.meter.pack(pady=6)

        self.value_label = ctk.CTkLabel(self, text="0 / 0",
                                        font=ctk.CTkFont(size=12))
        self.value_label.pack()

        ctk.CTkLabel(self, text="Umbral (0-1023):",
                     font=ctk.CTkFont(size=11)).pack(pady=(6, 0))
        self.slider = ctk.CTkSlider(self, from_=0, to=ANALOG_MAX,
                                    number_of_steps=ANALOG_MAX,
                                    command=lambda v: on_slider(self, int(v)))
        self.slider.pack(fill="x", padx=16, pady=4)

        entry_row = ctk.CTkFrame(self, fg_color="transparent")
        entry_row.pack(pady=(0, 10))
        self.entry = ctk.CTkEntry(entry_row, width=72, justify="center")
        self.entry.pack(side="left", padx=4)
        self.entry.bind("<Return>", lambda e: on_entry_apply(self))
        ctk.CTkButton(entry_row, text="Aplicar", width=62,
                      command=lambda: on_entry_apply(self)).pack(side="left", padx=4)


# ------------------------------- Aplicacion -------------------------------
class FSRDriverApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x700")
        self.minsize(840, 620)

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
        self._request_sync = False
        self._needs_reopen = False
        self._port_map = {}  # etiqueta del combo -> dispositivo real

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
        # Barra superior: puerto + conectar + acciones globales
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=10, pady=(10, 6))

        ctk.CTkLabel(top, text="Puerto COM:").pack(side="left", padx=(8, 4))
        self.port_var = ctk.StringVar(value="")
        self.port_combo = ctk.CTkComboBox(top, variable=self.port_var,
                                          values=[], width=180, state="readonly")
        self.port_combo.pack(side="left", padx=4)

        self.refresh_btn = ctk.CTkButton(top, text="Refrescar", width=80,
                                         command=self.refresh_ports)
        self.refresh_btn.pack(side="left", padx=4)

        self.connect_btn = ctk.CTkButton(top, text="Conectar", width=110,
                                         command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=(14, 4))

        self.status_label = ctk.CTkLabel(top, text="\u25cf Desconectado",
                                         text_color="#e63946")
        self.status_label.pack(side="left", padx=10)

        ctk.CTkButton(top, text="Guardar", width=80,
                      command=self.send_all_thresholds).pack(side="right", padx=(4, 8))
        ctk.CTkButton(top, text="Sincronizar", width=100,
                      command=self.request_all_thresholds).pack(side="right", padx=4)

        # Rejilla de paneles (distribucion tipo Pump It Up)
        grid = ctk.CTkFrame(self)
        grid.pack(fill="both", expand=True, padx=10, pady=6)

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
            panel.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
            self.panels[idx] = panel

        # Input viewer (mini-panel tipo Pump It Up) en el espacio central superior
        self.pad_viewer = PadViewer(grid)
        self.pad_viewer.grid(row=0, column=1, padx=8, pady=8, sticky="n")

        for col in range(3):
            grid.grid_columnconfigure(col, weight=1)
        grid.grid_rowconfigure(0, weight=1)
        grid.grid_rowconfigure(1, weight=1)

        # Barra inferior: log + ultima linea serial
        bottom = ctk.CTkFrame(self)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        self.log_label = ctk.CTkLabel(bottom, text="Listo", anchor="w",
                                      font=ctk.CTkFont(size=12))
        self.log_label.pack(fill="x", padx=8, pady=(4, 0))
        self.rx_label = ctk.CTkLabel(bottom, text="RX: ---", anchor="w",
                                     text_color="#8a8a8a",
                                     font=ctk.CTkFont(size=11))
        self.rx_label.pack(fill="x", padx=8, pady=(0, 4))

    # -------------------------------------------------------------- Puertos
    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
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
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        port = self._selected_device()
        if not port:
            self.status_queue.put("Selecciona un puerto COM.")
            return
        try:
            # Apertura ESTILO IDE: con DTR ASERTADO (true). El Leonardo se
            # reinicia al abrir y su CDC queda activo y emitiendo, igual que
            # ocurre al abrir el Serial Monitor del IDE. Con DTR bajo esta
            # placa NO transmitia nada (por eso Python no veía datos).
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = SERIAL_BAUD
            ser.timeout = 0.2
            ser.dtr = True
            ser.rts = False
            ser.open()
            time.sleep(2.0)  # espera a que reinicie y arranque el stream FSR
            ser.reset_input_buffer()  # descarta la basura del arranque
        except Exception as exc:  # puerto ocupado / no existe
            self.status_queue.put(f"Error al conectar: {exc}")
            return

        with self.serial_lock:
            self.serial_port = ser
        self.connected = True
        self._confirmed = False
        self._handshake_tries = 0
        self._request_sync = False
        self.running.set()
        self.reader_thread = threading.Thread(target=self._reader, args=(ser,),
                                              daemon=True)
        self.reader_thread.start()

        self.connect_btn.configure(text="Desconectar")
        self.status_label.configure(text="\u25cf Conectado", text_color=COL_IDLE)
        self.status_queue.put(f"Conectado a {port} @ {SERIAL_BAUD} baud. "
                              f"Detectando Arduino...")
        self._send("PING\n")
        self._send("GETALL\n")
        self.after(HANDSHAKE_INTERVAL_MS, self._handshake_tick)

    def _handshake_tick(self):
        """Reenvia PING/GETALL hasta confirmar que el Arduino responde."""
        if not self.connected or self._confirmed:
            return
        self._handshake_tries += 1
        if self._handshake_tries >= HANDSHAKE_MAX_TRIES:
            self.status_label.configure(text="\u25cf Conectado (sin respuesta)",
                                        text_color="#e63946")
            self.status_queue.put(
                f"Sin respuesta del Arduino en {self.port_var.get()}. Revisa: "
                "1) firmware NUEVO cargado (FSR_PUMP_warrior, 115200 baud), "
                "2) Serial Monitor / IDE cerrados, 3) cable USB de datos (no "
                "de solo carga), 4) que el puerto sea el correcto (pulsa "
                "Refrescar si cambio).")
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
        self.status_queue.put("Arduino detectado: protocolo activo (115200).")
        self._request_sync = True  # se consume en el hilo principal (_poll)

    def _reopen(self):
        """Cierra y reabre el puerto (el Leonardo puede re-enumerar el USB)."""
        with self.serial_lock:
            ser = self.serial_port
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

        # recarga la lista: el COM pudo haber cambiado tras re-enumera
        self.refresh_ports()
        port = self._selected_device()
        if not port:
            self.status_queue.put(
                "El Arduino no reaparece en ningun puerto. Revisa el cable USB.")
            self._drop_connection()
            return

        try:
            new_ser = serial.Serial()
            new_ser.port = port
            new_ser.baudrate = SERIAL_BAUD
            new_ser.timeout = 0.2
            new_ser.dtr = True
            new_ser.rts = False
            new_ser.open()
            time.sleep(2.0)
            new_ser.reset_input_buffer()
        except Exception as exc:
            self.status_queue.put(f"No se pudo reconectar a {port}: {exc}")
            self._drop_connection()
            return

        with self.serial_lock:
            self.serial_port = new_ser
        self._handshake_tries = 0
        self.running.set()
        self.reader_thread = threading.Thread(target=self._reader,
                                              args=(new_ser,), daemon=True)
        self.reader_thread.start()
        self._send("PING\n")
        self._send("GETALL\n")
        self.after(HANDSHAKE_INTERVAL_MS, self._handshake_tick)
        self.status_queue.put(f"Reconectado a {port}. Detectando Arduino...")

    def _drop_connection(self):
        """Fuerza el estado desconectado sin tocar la lista de puertos."""
        self.running.clear()
        with self.serial_lock:
            if self.serial_port is not None:
                try:
                    self.serial_port.close()
                except Exception:
                    pass
            self.serial_port = None
        self.connected = False
        self._confirmed = False
        self.connect_btn.configure(text="Conectar")
        self.status_label.configure(text="\u25cf Desconectado", text_color="#e63946")

    def disconnect(self):
        self.running.clear()
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=1.0)
            self.reader_thread = None
        with self.serial_lock:
            if self.serial_port is not None:
                try:
                    self.serial_port.close()
                except Exception:
                    pass
            self.serial_port = None
        self.connected = False
        self._confirmed = False
        self.connect_btn.configure(text="Conectar")
        self.status_label.configure(text="\u25cf Desconectado", text_color="#e63946")
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
            try:
                data = ser.read(256)
            except Exception as exc:
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
        panel.last_user_change = time.time()
        with self.data_lock:
            self.thresholds[panel.idx] = value
        panel.entry.delete(0, "end")
        panel.entry.insert(0, str(value))
        with self._pending_lock:
            self._pending[panel.idx] = value
        self._schedule_flush()

    def on_entry_apply(self, panel):
        panel.last_user_change = time.time()
        value = self._entry_int(panel.entry)
        if value is None:
            return
        value = max(0, min(ANALOG_MAX, value))
        panel.slider.set(value)
        with self.data_lock:
            self.thresholds[panel.idx] = value
        self._send(f"SET,{panel.idx},{value}\n")
        self.status_queue.put(
            f"Enviado: sensor {SENSORS[panel.idx]['short']} umbral = {value}")

    def _schedule_flush(self):
        if self._flush_timer is None:
            timer = threading.Timer(SEND_DEBOUNCE_MS / 1000.0,
                                    self._flush_pending)
            timer.daemon = True
            self._flush_timer = timer
            timer.start()

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
    def _poll(self):
        # drena la cola de mensajes
        while True:
            try:
                msg = self.status_queue.get_nowait()
            except queue.Empty:
                break
            self.log_label.configure(text=msg)
        self.rx_label.configure(text=f"RX: {self.last_line[:90]}")

        # si el firmware se detecto recien, sincroniza los umbrales
        if self._request_sync and self.connected:
            self._request_sync = False
            self.request_all_thresholds()
        if self._confirmed and self.status_label.cget("text") != "\u25cf Conectado":
            self.status_label.configure(text="\u25cf Conectado", text_color=COL_IDLE)

        # si el puerto murio (Leonardo re-enumero el USB), reconectar
        if self._needs_reopen and self.connected:
            self._needs_reopen = False
            self._reopen()

        with self.data_lock:
            values = list(self.values)
            thresholds = list(self.thresholds)

        now = time.time()
        for idx, panel in self.panels.items():
            value, th = values[idx], thresholds[idx]
            pressed = value >= th

            panel.meter.set_value(value)
            panel.meter.set_threshold(th)
            panel.meter.set_active(pressed)
            panel.meter.redraw()

            panel.value_label.configure(text=f"{value} / {th}")

            if pressed:
                panel.state_label.configure(text="PISADO", text_color=COL_HIT)
                panel.configure(border_color=COL_HIT)
            else:
                panel.state_label.configure(text="libre", text_color=COL_IDLE)
                panel.configure(border_color=COL_BORDER_IDLE)

            # Replica cambios de umbral que vienen del Arduino (GETALL/ACK)
            # solo si el usuario no esta manipulando ese panel ahora mismo.
            if now - panel.last_user_change > UI_SYNC_GRACE_S:
                if int(round(panel.slider.get())) != th:
                    panel.slider.set(th)
                if panel.entry.focus_get() is not panel.entry:
                    if self._entry_int(panel.entry) != th:
                        panel.entry.delete(0, "end")
                        panel.entry.insert(0, str(th))

        # input viewer central
        self.pad_viewer.set_values(values, thresholds)
        self.pad_viewer.redraw()

        self.after(POLL_MS, self._poll)

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
