"""
test_serial.py - Diagnostico BRUTO del puerto (como el Arduino IDE)
===================================================================
Abre el puerto COM de forma cruda y muestra TODO lo que llega del Arduino
(sin filtros, sin GUI), permitiendo tambien enviar comandos.

Esto sirve para confirmar si el cable/puerto/firmware funcionan: si aqui
ves lineas FSR:/THRESH:/READY: cada ~30 ms, el hardware esta bien y el
problema es de la app. Si no ves nada, revisa cable o firmware.

Uso:
    python test_serial.py                       # usa el primer puerto @115200
    python test_serial.py COM3 115200
    python test_serial.py COM3 9600             # en caso de firmware viejo

Comandos dentro del programa:
    PING          -> espera PONG
    GETALL        -> pide los 5 umbrales
    SET,0,500     -> fija umbral del sensor 0 a 500
    GET,i         -> pide umbral del sensor i
    quit / exit / CTRL+C  -> salir
"""

import sys
import threading
import time

import serial
import serial.tools.list_ports


def open_ide_style(device, baud):
    """Abre el puerto como el Arduino IDE: DTR asertado (la placa se
    reinicia y su CDC queda activo) con reintentos por si la re-enumeracion
    del Leonardo cambia el puerto al instante."""
    for attempt in range(4):
        try:
            s = serial.Serial()
            s.port = device
            s.baudrate = baud
            s.timeout = 0.2
            s.dtr = True      # igual que el Serial Monitor del IDE
            s.rts = False
            s.open()
            time.sleep(2.5)   # espera a que reinicie y arranque el stream
            _ = s.in_waiting  # fuerza contacto con el CDC
            return s
        except serial.SerialException as exc:
            print(f"  [intento {attempt + 1}] abriendo... ({exc})")
            time.sleep(1.0)
    return None


def main():
    ports = list(serial.tools.list_ports.comports())
    print("==", "Puertos detectados", "==")
    for p in ports:
        vid_pid = f"{p.vid:04X}:{p.pid:04X}" if p.vid and p.pid else "?"
        print(f"  {p.device:<6} {vid_pid:<10} {p.description}")
    if not ports:
        print("No se detecto ningun puerto. Conecta el Arduino al USB.")
        return

    # elegir puerto: argumento o el primero de la lista
    if len(sys.argv) >= 2:
        device = sys.argv[1]
    else:
        device = ports[0].device
    baud = int(sys.argv[2]) if len(sys.argv) >= 3 else 115200

    print(f"\nAbriendo {device} @ {baud} baud (DTR activado, como el IDE)...")
    ser = open_ide_style(device, baud)
    if ser is None:
        print(f"ERROR: no se pudo abrir {device}.")
        print("-> Puerto ocupado? Cierra el Arduino IDE / Serial Monitor / la app.")
        return

    ser.reset_input_buffer()
    print("Puerto abierto. Mostrando RX en vivo (como el Serial Monitor del IDE).")
    print("Escribe un comando y pulsa ENTER para enviarlo (PING, GETALL, SET,0,500...)")
    print("'quit' o CTRL+C para salir.\n")

    stop = threading.Event()
    tx_counter = [0]

    def sender():
        while not stop.is_set():
            try:
                line = input(">> ").strip()
            except (EOFError, KeyboardInterrupt):
                stop.set()
                break
            if not line:
                continue
            if line.lower() in ("quit", "exit", "salir"):
                stop.set()
                break
            try:
                ser.write((line + "\n").encode())
                tx_counter[0] += 1
                print(f"   [TX#{tx_counter[0]}] {line}")
            except Exception as exc:
                print(f"   [ERROR TX] {exc}")

    threading.Thread(target=sender, daemon=True).start()

    try:
        start = time.time()
        hinted = False
        while not stop.is_set():
            try:
                pending = ser.in_waiting
            except serial.SerialException:
                print("[ERROR] El puerto se cerro o el dispositivo se desconecto.")
                break
            if pending:
                hinted = True
                raw = ser.read(pending)
                try:
                    text = raw.decode(errors="replace")
                except Exception:
                    text = repr(raw)
                sys.stdout.write(text)
                sys.stdout.flush()
            else:
                if not hinted and time.time() - start > 4:
                    hinted = True
                    print("\n[AVISO] No llega NINGUN dato en 4 segundos.")
                    print("  -> El firmware NO se esta ejecutando o el puerto es fantasma.")
                    print("  1) Re-subi el sketch con Upload y fijate que el IDE diga")
                    print("     'Done uploading' (compilar NO es subir).")
                    print("  2) Desconecta y reconecta el USB; los LEDs deben parpadear")
                    print("     al encender (es la senal de que el sketch corre).")
                    print("  3) Abre Herramientas > Serial Monitor a 115200: si ahi")
                    print("     tampoco hay nada, el problema es del Arduino, no de la app.")
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            ser.close()
        except Exception:
            pass
        print("\nPuerto cerrado.")


if __name__ == "__main__":
    main()