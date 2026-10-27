#!/usr/bin/env python3
# cliente_v5.0 — LANZADOR del chat. Es lo único que hay que tener en cada ordenador.
#   · Al abrirlo pide la IP del servidor, descarga de él el programa del chat (programa_vX.py),
#     comprueba que está FIRMADO con la clave de quien gestiona el servidor y lo ejecuta.
#   · Así, para cambiar el chat solo hay que cambiar el programa en el servidor: nadie tiene que
#     instalar nada. Si se publica una versión nueva con el chat abierto, sale un botón para reiniciar.
#   · SEGURIDAD: solo se ejecuta código cuya firma Ed25519 coincida con CLAVE_PUBLICA (más abajo).
#     Si la firma no es válida, si el archivo se ha modificado o si el servidor ofrece una versión
#     más antigua que la última que se usó en este ordenador, NO se ejecuta nada.
#   · La clave pública la escribe aquí firmar.py crear-claves. Sin ella, el lanzador no abre nada.
#   · Necesita Python 3.8 o posterior con tkinter. No usa librerías externas.
#   · Archivos que guarda junto a sí: ultima_ip.txt, ajustes_chat.json (los usa también el programa),
#     lanzador_estado.json (última versión usada) y error_chat.txt (si algo falla al arrancar).

import base64
import hashlib
import json
import linecache
import os
import subprocess
import sys
import threading
import traceback
import types
import urllib.request
import tkinter as tk
from tkinter import messagebox

LANZADOR_VERSION = "5.0"
CLAVE_PUBLICA = "330858d9e7868deffac28eb23ebb528b6f9982b6785033927f9255576cb49599"   # 64 caracteres hexadecimales. La escribe firmar.py crear-claves.
PUERTO = 5000
MAX_PROGRAMA = 20 * 1024 * 1024
PREFIJO_FIRMA = b"chat-programa-firma-v1\n"   # lo que se firma es PREFIJO_FIRMA + manifiesto

CARPETA = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_IP = os.path.join(CARPETA, "ultima_ip.txt")
ARCHIVO_ESTADO = os.path.join(CARPETA, "lanzador_estado.json")
ARCHIVO_AJUSTES = os.path.join(CARPETA, "ajustes_chat.json")
ARCHIVO_ERROR = os.path.join(CARPETA, "error_chat.txt")


# ---------- Ed25519: solo comprobar firmas (RFC 8032). Con datos públicos: no hay secretos que filtrar.
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_RAIZ_M1 = pow(2, (_P - 1) // 4, _P)


def _sumar(a, b):
    x = (a[1] - a[0]) * (b[1] - b[0]) % _P
    y = (a[1] + a[0]) * (b[1] + b[0]) % _P
    z = 2 * a[3] * b[3] * _D % _P
    w = 2 * a[2] * b[2] % _P
    e, f, g, h = y - x, w - z, w + z, y + x
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiplicar(n, punto):
    r = (0, 1, 1, 0)
    while n > 0:
        if n & 1:
            r = _sumar(r, punto)
        punto = _sumar(punto, punto)
        n >>= 1
    return r


def _iguales(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recuperar_x(y, signo):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if signo else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _RAIZ_M1 % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != signo:
        x = _P - x
    return x


def _descomprimir(b):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    signo = y >> 255
    y &= (1 << 255) - 1
    x = _recuperar_x(y, signo)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recuperar_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def firma_valida(publica, mensaje, firma):
    """True solo si 'firma' (64 bytes) es la firma Ed25519 de 'mensaje' con la clave 'publica' (32 bytes)."""
    if len(publica) != 32 or len(firma) != 64:
        return False
    a = _descomprimir(publica)
    r = _descomprimir(firma[:32])
    s = int.from_bytes(firma[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(firma[:32] + publica + mensaje).digest(), "little") % _L
    return _iguales(_multiplicar(s, _G), _sumar(r, _multiplicar(h, a)))


# ---------- utilidades
def clave_version(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def leer_json(ruta):
    try:
        with open(ruta, encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except (OSError, ValueError):
        return {}


def guardar_json(ruta, datos):
    try:
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(datos, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def leer_ip():
    try:
        with open(ARCHIVO_IP, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def guardar_ip(ip):
    try:
        with open(ARCHIVO_IP, "w", encoding="utf-8") as fh:
            fh.write(ip)
    except OSError:
        pass


def python_sin_consola():
    if sys.platform == "win32":
        w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.isfile(w):
            return w
    return sys.executable


def ocultar_consola():
    """Con doble clic, Windows abre una consola solo para el chat: se oculta."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        ventana = k32.GetConsoleWindow()
        if not ventana:
            return
        procesos = (ctypes.c_uint * 4)()
        if k32.GetConsoleProcessList(procesos, 4) <= 1:
            ctypes.windll.user32.ShowWindow(ventana, 0)
    except Exception:
        pass


class ProgramaNoValido(Exception):
    pass


def descargar_y_comprobar(ip):
    """Descarga el programa del servidor y lo comprueba. Devuelve (código, manifiesto) o lanza una excepción."""
    if len(CLAVE_PUBLICA) != 64:
        raise ProgramaNoValido("Este lanzador no tiene clave pública. Hay que preparar el lanzador con "
                               "firmar.py crear-claves antes de repartirlo.")
    try:
        publica = bytes.fromhex(CLAVE_PUBLICA)
    except ValueError:
        raise ProgramaNoValido("La clave pública del lanzador está mal escrita.")
    url = f"http://{ip}:{PUERTO}/programa"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            cuerpo = r.read(MAX_PROGRAMA + 1)
    except OSError as e:
        raise OSError(f"No se pudo descargar el programa de {ip}:{PUERTO}.\n\n{e}")
    if len(cuerpo) > MAX_PROGRAMA:
        raise ProgramaNoValido("El servidor ha enviado un programa demasiado grande.")
    try:
        paquete = json.loads(cuerpo.decode("utf-8"))
        texto_manifiesto = paquete["manifiesto"]
        firma = bytes.fromhex(paquete["firma"])
        codigo = base64.b64decode(paquete["codigo"], validate=True)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise ProgramaNoValido("El servidor no ha enviado un programa en el formato esperado.")
    # 1) la firma tiene que ser de la clave que lleva este lanzador
    if not firma_valida(publica, PREFIJO_FIRMA + texto_manifiesto.encode("utf-8"), firma):
        raise ProgramaNoValido("El programa de este servidor NO tiene una firma válida.\n\n"
                               "Por seguridad no se ha ejecutado. Puede que el servidor no sea el de "
                               "siempre o que el programa se haya modificado sin volver a firmarlo.")
    # 2) el código tiene que ser exactamente el firmado
    try:
        m = json.loads(texto_manifiesto)
    except ValueError:
        raise ProgramaNoValido("El manifiesto del programa está dañado.")
    if m.get("formato") != 1 or hashlib.sha256(codigo).hexdigest() != m.get("sha256") or len(codigo) != m.get("tamano"):
        raise ProgramaNoValido("El programa no coincide con lo que se firmó. No se ha ejecutado.")
    # 3) nada de volver a versiones anteriores a la última que se usó aquí
    version = str(m.get("version", ""))
    maxima = str(leer_json(ARCHIVO_ESTADO).get("version_max", "0"))
    if clave_version(version) < clave_version(maxima):
        raise ProgramaNoValido(f"El servidor ofrece la versión {version}, pero en este ordenador ya se ha usado "
                               f"la {maxima}.\n\nPor seguridad no se abren versiones anteriores.")
    # 4) requisitos de este ordenador
    if sys.version_info[:2] < clave_version(m.get("python_min", "3.8"))[:2]:
        raise ProgramaNoValido(f"La versión {version} del chat necesita Python {m.get('python_min')} o posterior "
                               f"y este ordenador tiene Python {sys.version_info[0]}.{sys.version_info[1]}.")
    if clave_version(LANZADOR_VERSION) < clave_version(m.get("lanzador_min", "0")):
        raise ProgramaNoValido(f"La versión {version} del chat necesita un lanzador más nuevo "
                               f"(v{m.get('lanzador_min')} o posterior). Pide el cliente nuevo a quien "
                               "gestiona el servidor.")
    return codigo, m


def reiniciar():
    """Vuelve a abrir el lanzador (descarga la versión nueva) y conecta solo."""
    opciones = {"cwd": CARPETA}
    if sys.platform == "win32":
        opciones["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([python_sin_consola(), os.path.abspath(__file__), "--conectar"], **opciones)


def ejecutar(codigo, manifiesto, ip):
    """Ejecuta el programa descargado en memoria, como si se hubiera abierto con doble clic."""
    nombre = f"<servidor {ip}: programa v{manifiesto['version']}>"
    texto = codigo.decode("utf-8")
    linecache.cache[nombre] = (len(texto), None, texto.splitlines(True), nombre)  # trazas de error legibles
    modulo = types.ModuleType("programa_chat")
    modulo.__file__ = os.path.abspath(__file__)   # sus archivos (ajustes, última IP...) junto al lanzador
    sys.modules["programa_chat"] = modulo
    exec(compile(texto, nombre, "exec"), modulo.__dict__)
    modulo.main(lanzador={
        "version_lanzador": LANZADOR_VERSION,
        "version_programa": manifiesto["version"],
        "ip": ip,
        "conectar": True,
        "reiniciar": reiniciar,
    })


def mostrar_error(titulo, texto, detalle=None):
    if detalle:
        try:
            with open(ARCHIVO_ERROR, "w", encoding="utf-8") as fh:
                fh.write(detalle)
        except OSError:
            pass
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(titulo, texto, parent=r)
        r.destroy()
    except Exception:
        print(titulo + ": " + texto)


class Lanzador:
    def __init__(self, root, auto):
        self.root = root
        self.resultado = None
        oscuro = leer_json(ARCHIVO_AJUSTES).get("tema", "oscuro") != "claro"
        fondo, texto, caja, acento = (("#1e1f22", "#e6e6e6", "#2b2d31", "#5865f2") if oscuro
                                      else ("#f4f5f7", "#1f2328", "#ffffff", "#3b5bdb"))
        root.title(f"Chat LAN — cliente v{LANZADOR_VERSION}")
        root.configure(bg=fondo)
        root.resizable(False, False)
        marco = tk.Frame(root, bg=fondo, padx=18, pady=16)
        marco.pack()
        tk.Label(marco, text="Chat LAN", font=("Segoe UI", 15, "bold"), bg=fondo, fg=texto).grid(
            row=0, column=0, columnspan=3, sticky="w")
        tk.Label(marco, text="IP del servidor:", bg=fondo, fg=texto).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.ip = tk.StringVar(value=leer_ip() or "127.0.0.1")
        self.entrada = tk.Entry(marco, textvariable=self.ip, width=18, bg=caja, fg=texto, insertbackground=texto,
                                relief="flat", highlightthickness=1, highlightbackground=acento)
        self.entrada.grid(row=1, column=1, padx=8, pady=(10, 0))
        self.boton = tk.Button(marco, text="Abrir", command=self.abrir, bg=acento, fg="#ffffff",
                               activebackground=acento, activeforeground="#ffffff", relief="flat", padx=12)
        self.boton.grid(row=1, column=2, pady=(10, 0))
        self.estado = tk.Label(marco, text="El programa del chat se descarga del servidor cada vez que lo abres.",
                               bg=fondo, fg=texto, wraplength=330, justify="left")
        self.estado.grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.entrada.bind("<Return>", lambda e: self.abrir())
        self.entrada.focus_set()
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        if auto:
            root.after(150, self.abrir)

    def abrir(self):
        ip = self.ip.get().strip()
        if not ip:
            self.estado.config(text="Escribe la IP del servidor.")
            return
        self.boton.config(state="disabled")
        self.entrada.config(state="disabled")
        self.estado.config(text=f"Descargando el programa de {ip}…")
        threading.Thread(target=self.hilo, args=(ip,), daemon=True).start()

    def hilo(self, ip):
        try:
            codigo, m = descargar_y_comprobar(ip)
            self.root.after(0, self.listo, ip, codigo, m)
        except (OSError, ProgramaNoValido) as e:
            self.root.after(0, self.fallo, str(e), isinstance(e, ProgramaNoValido))

    def fallo(self, texto, seguridad):
        self.boton.config(state="normal")
        self.entrada.config(state="normal")
        self.estado.config(text="No se ha abierto el chat.")
        messagebox.showerror("Programa no válido" if seguridad else "Sin conexión", texto, parent=self.root)

    def listo(self, ip, codigo, m):
        guardar_ip(ip)
        estado = leer_json(ARCHIVO_ESTADO)
        if clave_version(m["version"]) > clave_version(estado.get("version_max", "0")):
            estado["version_max"] = m["version"]
            guardar_json(ARCHIVO_ESTADO, estado)
        self.resultado = (codigo, m, ip)
        self.root.destroy()


def main():
    os.chdir(CARPETA)
    ocultar_consola()
    try:
        root = tk.Tk()
        lanzador = Lanzador(root, auto="--conectar" in sys.argv[1:])
        root.mainloop()
    except Exception as e:
        mostrar_error("El chat no ha podido arrancar", str(e), traceback.format_exc())
        return
    if lanzador.resultado is None:
        return   # se ha cerrado la ventana sin abrir el chat
    codigo, m, ip = lanzador.resultado
    try:
        ejecutar(codigo, m, ip)
    except Exception as e:
        mostrar_error("El programa del chat ha fallado",
                      f"{e}\n\nEl detalle está en error_chat.txt, junto al cliente.", traceback.format_exc())


if __name__ == "__main__":
    main()
