# v4.1 — cambios sobre la 4.0 (que sigue funcionando igual):
#   · La galería tiene un panel de VISTA PREVIA: al seleccionar un archivo se ve sin descargarlo.
#       - .png / .jpg  → se muestra la imagen ajustada al panel.
#       - .py / .java / .txt → se muestra el texto (solo lectura, letra monoespaciada).
#   · La vista previa se guarda en memoria: si luego pulsas "Descargar", no se vuelve a pedir al servidor.
#   · Para ver .jpg hace falta Pillow; el programa intenta instalarlo solo (como winotify).
#     Los .png se ven siempre, aunque no haya Pillow.
#   · No hace falta cambiar el servidor: funciona con servidor_v4.0.py.
#   · El color del nombre no puede ser blanco ni casi blanco (no se leería sobre el fondo blanco).
#     Si alguien lo tiene así (por ejemplo, con un cliente antiguo), se muestra oscurecido.

# v3.2 — cambios sobre la 3.1 (que sigue funcionando igual):
#   · Botón "Mi color": eliges el color de tu nombre y se ve igual en todos los ordenadores.
#   · Solo se pueden subir archivos .py, .java, .txt, .jpg y .png.
#   · Nueva ventana "Galería de archivos" (fuera del chat principal) para ver y descargar
#     los archivos guardados por el servidor en la carpeta "chat_archivos".
# Lo que ya funcionaba (contraseña, nombres, privados, historial, reconexión...) no se ha tocado.

import io
import math
import os
import json
import queue
import re
import socket
import subprocess
import sys
import threading
import tkinter as tk
import zlib
import base64
from tkinter import filedialog, messagebox, simpledialog, colorchooser
from datetime import datetime
from tkinter import scrolledtext

PUERTO = 5000
MAX_MSG = 500
EXTENSIONES_PERMITIDAS = {'.py', '.java', '.txt', '.jpg', '.png'}
MAX_ARCHIVO = 10 * 1024 * 1024
REINTENTO_MS = 5000
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ultima_ip.txt")

PATRON_CHAT = re.compile(r"^(\[\d\d:\d\d\]) ([^:]+): (.*)$")
PATRON_PRIV = re.compile(r"^(\[\d\d:\d\d\]) \(privado\) (de|para) ([^:]+): (.*)$")
PATRON_HOLA = re.compile(r"^Hola (.+?), ya estás en el chat")
PATRON_LISTA = re.compile(r"^\*\*\* Conectados: (.*) \*\*\*$")
PATRON_NICK = re.compile(r"^\*\*\* (.+?) ahora se llama (.+?) \*\*\*$")
PATRON_EVENTO = re.compile(r"^\*\*\* .+? (se ha conectado|se ha desconectado|ahora se llama .+?) \*\*\*$")
PATRON_SUBIDA = re.compile(r"^\*\*\* .+ ha subido el archivo .+ \*\*\*$")

CONTRASTE_MIN = 2.5  # contraste mínimo del nombre sobre el fondo blanco del chat


def luminancia(hexa):
    """Luminancia relativa (0 = negro, 1 = blanco) de un color #rrggbb."""
    def canal(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hexa[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * canal(r) + 0.7152 * canal(g) + 0.0722 * canal(b)


def color_legible(hexa):
    """True si el color se lee bien sobre blanco."""
    try:
        return 1.05 / (luminancia(hexa) + 0.05) >= CONTRASTE_MIN
    except (ValueError, TypeError, IndexError):
        return False


def oscurecer(hexa):
    """Devuelve el mismo tono, oscurecido lo justo para que se lea sobre blanco."""
    try:
        r, g, b = (int(hexa[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, TypeError, IndexError):
        return "#495057"
    if color_legible(hexa):
        return hexa
    for paso in range(100, -1, -2):
        f = paso / 100
        nuevo = "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))
        if color_legible(nuevo):
            return nuevo
    return "#495057"


PALETA = ["#0b7285", "#9c36b5", "#c92a2a", "#e67700", "#2b8a3e", "#1864ab", "#a61e4d", "#5f3dc4"]

Notification = None  # se carga en segundo plano (solo Windows)
Image = None         # Pillow (para ver .jpg y redimensionar mejor); se carga en segundo plano
ImageTk = None

EXT_IMAGEN = {'.png', '.jpg'}
EXT_TEXTO = {'.py', '.java', '.txt'}
MAX_TEXTO_VISTA = 300_000  # caracteres máximos que se muestran en la vista previa de texto


def cargar_pillow():
    global Image, ImageTk
    try:
        from PIL import Image as I, ImageTk as IT
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "pillow"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            from PIL import Image as I, ImageTk as IT
        except Exception:
            return
    Image, ImageTk = I, IT


def cargar_notificaciones():
    global Notification
    if sys.platform != "win32":
        return
    try:
        from winotify import Notification as N
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "winotify"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            from winotify import Notification as N
        except Exception:
            return
    Notification = N


def leer_ip_guardada():
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def guardar_ip(ip):
    try:
        with open(CONFIG, "w", encoding="utf-8") as fh:
            fh.write(ip)
    except OSError:
        pass


def hora():
    return datetime.now().strftime("%H:%M")


class App:
    def __init__(self, root):
        self.root = root
        self.cola = queue.Queue()
        self.conn = None
        self.gen = 0                 # identifica la conexión vigente; los eventos de otras se ignoran
        self.quiere = False          # el usuario quiere estar conectado (reintenta si se corta)
        self.intento_auto = False
        self.reintento_id = None
        self.registrado = False
        self.password_pedida = False
        self.nombre = ""
        self.listas_silenciosas = 0  # respuestas de /usuarios pedidas por el programa, no por el usuario
        self.sin_leer = 0
        self.historial = []
        self.pos = 0
        self.silencio = tk.BooleanVar(value=False)
        self.ip_var = tk.StringVar(value=leer_ip_guardada() or "127.0.0.1")
        self.colores = {}            # nombre -> "#rrggbb" (lo difunde el servidor)
        self.tags_nombre = {}        # nombre -> etiqueta de texto para ese color
        self.archivos = {}           # nombre_archivo -> tamaño en bytes (galería)
        self.galeria = None          # ventana de la galería
        self.galeria_nombres = []    # nombres reales de los archivos listados
        self.descarga = None         # descarga en curso: {"nombre", "tamano", "ruta", "datos", "modo"}
        self.cache_vista = {}        # nombre_archivo -> bytes ya recibidos (vista previa)
        self.vista_pendiente = None  # archivo que se quiere previsualizar cuando acabe la transferencia actual
        self.vista_actual = None     # archivo mostrado ahora en el panel
        self.foto_vista = None       # referencia a la imagen (si no, tkinter la borra)

        root.title("Chat LAN")
        root.geometry("820x540")
        root.minsize(560, 360)
        self.construir()
        root.protocol("WM_DELETE_WINDOW", self.cerrar)
        root.bind("<FocusIn>", self.al_enfocar)
        self.ui_estado(False)
        self.estado("Pon la IP del servidor y pulsa Conectar.")
        self.sistema("Bienvenido. Conéctate a un servidor para empezar.")
        threading.Thread(target=cargar_notificaciones, daemon=True).start()
        threading.Thread(target=cargar_pillow, daemon=True).start()
        self.root.after(100, self.procesar)

    # ---------- interfaz ----------
    def construir(self):
        r = self.root
        barra = tk.Frame(r, padx=8, pady=6)
        barra.pack(fill="x")
        tk.Label(barra, text="Servidor:").pack(side="left")
        self.ip_entry = tk.Entry(barra, textvariable=self.ip_var, width=14)
        self.ip_entry.pack(side="left", padx=6)
        self.ip_entry.bind("<Return>", lambda e: self.alternar_conexion())
        self.btn_con = tk.Button(barra, text="Conectar", width=10, command=self.alternar_conexion)
        self.btn_con.pack(side="left")
        self.btn_archivo = tk.Button(barra, text="Enviar archivo", command=self.seleccionar_archivo, state="disabled")
        self.btn_archivo.pack(side="left", padx=6)
        self.btn_color = tk.Button(barra, text="Color", command=self.elegir_color, state="disabled")
        self.btn_color.pack(side="left")
        self.btn_galeria = tk.Button(barra, text="Archivos", command=self.abrir_galeria)
        self.btn_galeria.pack(side="left", padx=6)
        tk.Checkbutton(barra, text="Silenciar notificaciones", variable=self.silencio).pack(side="right")

        centro = tk.Frame(r, padx=8)
        centro.pack(fill="both", expand=True)

        lateral = tk.Frame(centro)
        lateral.pack(side="right", fill="y", padx=(8, 0))
        tk.Label(lateral, text="Conectados", anchor="w").pack(fill="x")
        self.lista = tk.Listbox(lateral, width=18, activestyle="none", exportselection=False)
        self.lista.pack(fill="y", expand=True)
        self.lista.bind("<Double-Button-1>", self.privado_a_seleccion)

        self.txt = scrolledtext.ScrolledText(
            centro, wrap="word", state="disabled", font=("Segoe UI", 10),
            bg="white", relief="solid", borderwidth=1, padx=6, pady=4,
        )
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_config("hora", foreground="#868e96")
        self.txt.tag_config("sistema", foreground="#868e96", font=("Segoe UI", 9, "italic"))
        self.txt.tag_config("privado", foreground="#c2255c")
        self.txt.tag_config("privado_b", foreground="#c2255c", font=("Segoe UI", 10, "bold"))
        self.txt.tag_config("propio", foreground="#2b8a3e", font=("Segoe UI", 10, "bold"))
        self.txt.tag_config("servidor", foreground="#e8590c", font=("Segoe UI", 10, "bold"))

        pie = tk.Frame(r, padx=8, pady=6)
        pie.pack(fill="x")
        self.entrada = tk.Entry(pie, font=("Segoe UI", 10))
        self.entrada.pack(side="left", fill="x", expand=True, ipady=4)
        self.entrada.bind("<Return>", self.enviar)
        self.entrada.bind("<Up>", lambda e: self.navegar(-1))
        self.entrada.bind("<Down>", lambda e: self.navegar(1))
        self.btn_env = tk.Button(pie, text="Enviar", width=10, command=self.enviar)
        self.btn_env.pack(side="left", padx=(6, 0))

        self.barra_estado = tk.Label(r, anchor="w", relief="sunken", bd=1, padx=6)
        self.barra_estado.pack(fill="x", side="bottom")

    def estado(self, texto):
        self.barra_estado.config(text=texto)

    def ui_estado(self, conectado):
        self.btn_con.config(text="Desconectar" if conectado else "Conectar")
        self.ip_entry.config(state="disabled" if conectado else "normal")
        st = "normal" if conectado else "disabled"
        self.entrada.config(state=st)
        self.btn_env.config(state=st)
        self.btn_archivo.config(state=st if self.registrado else "disabled")
        self.btn_color.config(state=st if self.registrado else "disabled")
        self.galeria_botones()
        if conectado:
            self.entrada.focus_set()

    def escribir(self, segmentos, forzar_abajo=False):
        abajo = self.txt.yview()[1] >= 0.999
        self.txt.config(state="normal")
        for texto, tag in segmentos:
            self.txt.insert("end", texto, tag)
        self.txt.insert("end", "\n")
        self.txt.config(state="disabled")
        if abajo or forzar_abajo:
            self.txt.see("end")

    def sistema(self, texto):
        self.escribir([(f"{hora()} ", "hora"), (f"*** {texto} ***", "sistema")])

    def etiqueta_nombre(self, nombre):
        """Etiqueta de color para un nombre. El color lo decide el servidor (@@COLOR)."""
        color = self.colores.get(nombre)
        if color is None:
            color = PALETA[zlib.crc32(nombre.lower().encode("utf-8")) % len(PALETA)]
        color = oscurecer(color)
        tag = self.tags_nombre.get(nombre)
        if tag is None:
            tag = f"usr{len(self.tags_nombre)}"
            self.tags_nombre[nombre] = tag
        self.txt.tag_config(tag, foreground=color, font=("Segoe UI", 10, "bold"))
        return tag

    # ---------- galería de archivos ----------
    def abrir_galeria(self):
        if self.galeria is not None and self.galeria.winfo_exists():
            self.galeria.deiconify()
            self.galeria.focus_set()
            return
        g = tk.Toplevel(self.root)
        g.title("Galería de archivos")
        g.geometry("900x540")
        g.minsize(680, 360)
        self.galeria = g

        tk.Label(g, text="Archivos compartidos en el servidor (carpeta chat_archivos). "
                         "Haz clic en uno para verlo; doble clic para descargarlo.", anchor="w").pack(
            fill="x", padx=8, pady=(8, 0)
        )

        self.galeria_estado_txt = tk.Label(g, anchor="w", text="", relief="sunken", bd=1, padx=6)
        self.galeria_estado_txt.pack(fill="x", side="bottom")

        paneles = tk.PanedWindow(g, orient="horizontal", sashwidth=6)
        paneles.pack(fill="both", expand=True, padx=8, pady=6)

        # --- izquierda: lista de archivos + botones ---
        izquierda = tk.Frame(paneles)
        marco = tk.Frame(izquierda)
        marco.pack(fill="both", expand=True)
        barra_l = tk.Scrollbar(marco)
        barra_l.pack(side="right", fill="y")
        self.lista_archivos = tk.Listbox(marco, activestyle="none", exportselection=False, yscrollcommand=barra_l.set)
        self.lista_archivos.pack(side="left", fill="both", expand=True)
        barra_l.config(command=self.lista_archivos.yview)
        self.lista_archivos.bind("<Double-Button-1>", lambda e: self.galeria_descargar())
        self.lista_archivos.bind("<<ListboxSelect>>", lambda e: self.vista_seleccion())

        botones = tk.Frame(izquierda)
        botones.pack(fill="x", pady=(6, 2))
        self.btn_actualizar = tk.Button(botones, text="Actualizar", command=self.pedir_archivos)
        self.btn_actualizar.pack(side="left")
        self.btn_descargar = tk.Button(botones, text="Descargar", command=self.galeria_descargar)
        self.btn_descargar.pack(side="left", padx=6)
        self.btn_subir_galeria = tk.Button(botones, text="Subir archivo", command=self.seleccionar_archivo)
        self.btn_subir_galeria.pack(side="left")
        paneles.add(izquierda, minsize=320, width=330)

        # --- derecha: vista previa ---
        derecha = tk.Frame(paneles, relief="solid", bd=1, bg="white")
        self.vista_titulo = tk.Label(derecha, text="Vista previa", anchor="w", bg="#f1f3f5",
                                     font=("Segoe UI", 10, "bold"), padx=6, pady=3)
        self.vista_titulo.pack(fill="x")
        self.vista_cuerpo = tk.Frame(derecha, bg="white")
        self.vista_cuerpo.pack(fill="both", expand=True)
        # imagen
        self.vista_imagen = tk.Label(self.vista_cuerpo, bg="white")
        # texto (con barra de desplazamiento)
        self.vista_texto = scrolledtext.ScrolledText(
            self.vista_cuerpo, wrap="none", font=("Consolas", 10), bg="white",
            relief="flat", padx=6, pady=4, state="disabled",
        )
        barra_h = tk.Scrollbar(self.vista_texto.frame, orient="horizontal", command=self.vista_texto.xview)
        self.vista_texto.config(xscrollcommand=barra_h.set)
        barra_h.pack(side="bottom", fill="x", before=self.vista_texto.vbar)
        # mensaje (vacío, cargando, error...)
        self.vista_mensaje = tk.Label(self.vista_cuerpo, bg="white", fg="#868e96",
                                      font=("Segoe UI", 10, "italic"), wraplength=380, justify="center")
        paneles.add(derecha, minsize=300)
        self.vista_aviso("Selecciona un archivo de la lista para verlo aquí.")
        self.vista_cuerpo.bind("<Configure>", self.vista_redimensionar)

        g.protocol("WM_DELETE_WINDOW", self.cerrar_galeria)
        self.galeria_mostrar()
        self.galeria_botones()
        self.pedir_archivos()

    def cerrar_galeria(self):
        if self.galeria is not None:
            try:
                self.galeria.destroy()
            except tk.TclError:
                pass
            self.galeria = None
            self.vista_actual = None
            self.vista_pendiente = None
            self.foto_vista = None

    # ---------- vista previa ----------
    def galeria_abierta(self):
        return self.galeria is not None and self.galeria.winfo_exists()

    def vista_limpiar(self):
        for w in (self.vista_imagen, self.vista_texto, self.vista_mensaje):
            w.pack_forget()

    def vista_aviso(self, texto, titulo="Vista previa"):
        if not self.galeria_abierta():
            return
        self.vista_limpiar()
        self.vista_titulo.config(text=titulo)
        self.vista_mensaje.config(text=texto)
        self.vista_mensaje.pack(fill="both", expand=True, padx=12, pady=12)

    def vista_seleccion(self):
        """Clic en un archivo de la lista: lo previsualiza (desde memoria o pidiéndolo al servidor)."""
        if not self.galeria_abierta():
            return
        sel = self.lista_archivos.curselection()
        if not sel:
            return
        nombre = self.galeria_nombres[sel[0]]
        ext = os.path.splitext(nombre)[1].lower()
        if ext not in EXT_IMAGEN | EXT_TEXTO:
            self.vista_aviso("Este tipo de archivo no tiene vista previa.", nombre)
            return
        if nombre in self.cache_vista:
            self.vista_mostrar(nombre, self.cache_vista[nombre])
            return
        if not (self.conn and self.registrado):
            self.vista_aviso("Conéctate al servidor para ver el archivo.", nombre)
            return
        self.vista_aviso("Cargando vista previa…", nombre)
        self.vista_actual = nombre
        if self.descarga is not None:
            # hay otra transferencia en marcha: se pide al terminar (solo la última selección)
            self.vista_pendiente = nombre
            return
        self.vista_pedir(nombre)

    def vista_pedir(self, nombre):
        self.descarga = {"nombre": nombre, "tamano": None, "ruta": None, "datos": [], "modo": "vista"}
        if not self.enviar_crudo(f"/descargar {nombre}"):
            self.descarga = None
            self.vista_aviso("No se pudo pedir el archivo al servidor.", nombre)

    def vista_siguiente(self):
        """Al terminar una transferencia, lanza la vista previa que estaba esperando."""
        nombre, self.vista_pendiente = self.vista_pendiente, None
        if not nombre or self.descarga is not None or not self.galeria_abierta():
            return
        if nombre != self.vista_actual:
            return
        if nombre in self.cache_vista:
            self.vista_mostrar(nombre, self.cache_vista[nombre])
        elif self.conn and self.registrado:
            self.vista_pedir(nombre)

    def vista_mostrar(self, nombre, contenido):
        if not self.galeria_abierta():
            return
        self.vista_actual = nombre
        ext = os.path.splitext(nombre)[1].lower()
        if ext in EXT_IMAGEN:
            self.vista_mostrar_imagen(nombre, contenido)
        else:
            self.vista_mostrar_texto(nombre, contenido)

    def vista_mostrar_texto(self, nombre, contenido):
        try:
            texto = contenido.decode("utf-8")
        except UnicodeDecodeError:
            texto = contenido.decode("latin-1", errors="replace")
        recortado = len(texto) > MAX_TEXTO_VISTA
        if recortado:
            texto = texto[:MAX_TEXTO_VISTA]
        lineas = texto.count("\n") + (1 if texto and not texto.endswith("\n") else 0)
        self.vista_limpiar()
        info = f"{nombre}  —  {lineas} línea(s)" + ("  (recortado)" if recortado else "")
        self.vista_titulo.config(text=info)
        self.vista_texto.config(state="normal")
        self.vista_texto.delete("1.0", "end")
        if texto:
            self.vista_texto.insert("1.0", texto)
        else:
            self.vista_texto.insert("1.0", "(archivo vacío)")
        self.vista_texto.config(state="disabled")
        self.vista_texto.yview_moveto(0)
        self.vista_texto.pack(fill="both", expand=True)

    def vista_mostrar_imagen(self, nombre, contenido):
        ancho = max(self.vista_cuerpo.winfo_width() - 16, 100)
        alto = max(self.vista_cuerpo.winfo_height() - 16, 100)
        ext = os.path.splitext(nombre)[1].lower()
        foto, dim = None, None
        try:
            if Image is not None:
                img = Image.open(io.BytesIO(contenido))
                img.load()
                dim = img.size
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                img.thumbnail((ancho, alto))
                foto = ImageTk.PhotoImage(img, master=self.galeria)
            elif ext == ".png":
                # sin Pillow: tkinter sabe abrir PNG por sí solo (solo reduce a escala entera)
                foto = tk.PhotoImage(master=self.galeria, data=base64.b64encode(contenido).decode("ascii"))
                dim = (foto.width(), foto.height())
                factor = max(1, math.ceil(max(dim[0] / ancho, dim[1] / alto)))
                if factor > 1:
                    foto = foto.subsample(factor, factor)
            else:
                self.vista_aviso("Para ver imágenes .jpg hace falta Pillow.\n"
                                 "Ejecuta: pip install pillow\n(o descarga el archivo).", nombre)
                return
        except Exception:
            self.vista_aviso("No se pudo abrir la imagen (puede estar dañada).", nombre)
            return
        self.foto_vista = foto
        self.vista_limpiar()
        self.vista_titulo.config(text=f"{nombre}  —  {dim[0]}×{dim[1]} px")
        self.vista_imagen.config(image=foto)
        self.vista_imagen.pack(fill="both", expand=True)

    def vista_redimensionar(self, _evento=None):
        """Al cambiar el tamaño de la ventana, reajusta la imagen mostrada."""
        if getattr(self, "_redim_id", None):
            self.galeria.after_cancel(self._redim_id)
        def rehacer():
            self._redim_id = None
            n = self.vista_actual
            if (self.galeria_abierta() and n and n in self.cache_vista
                    and os.path.splitext(n)[1].lower() in EXT_IMAGEN and self.vista_imagen.winfo_ismapped()):
                self.vista_mostrar_imagen(n, self.cache_vista[n])
        self._redim_id = self.galeria.after(250, rehacer)

    def galeria_mostrar(self):
        if self.galeria is None or not self.galeria.winfo_exists():
            return
        self.lista_archivos.delete(0, "end")
        self.galeria_nombres = []
        for nombre in sorted(self.archivos, key=str.lower):
            self.galeria_nombres.append(nombre)
            tam = self.archivos[nombre]
            if tam >= 1024 * 1024:
                tam_txt = f"{tam / (1024 * 1024):.1f} MB"
            else:
                tam_txt = f"{tam / 1024:.1f} KB"
            self.lista_archivos.insert("end", f"{nombre}  ({tam_txt})")
            if nombre == self.vista_actual:
                self.lista_archivos.selection_set("end")  # mantiene marcado el archivo que se está viendo

    def galeria_botones(self):
        if self.galeria is None or not self.galeria.winfo_exists():
            return
        st = "normal" if (self.conn and self.registrado) else "disabled"
        self.btn_actualizar.config(state=st)
        self.btn_descargar.config(state=st)
        self.btn_subir_galeria.config(state=st)
        if st == "disabled":
            self.galeria_estado_txt.config(text="Conéctate al servidor para usar la galería.")

    def pedir_archivos(self):
        if self.conn and self.registrado:
            self.enviar_crudo("/archivos")
        elif self.galeria is not None and self.galeria.winfo_exists():
            self.galeria_estado_txt.config(text="Conéctate al servidor para usar la galería.")

    def galeria_descargar(self):
        if not (self.conn and self.registrado):
            return
        sel = self.lista_archivos.curselection()
        if not sel:
            self.galeria_estado_txt.config(text="Selecciona un archivo de la lista.")
            return
        nombre = self.galeria_nombres[sel[0]]
        if nombre not in self.cache_vista and self.descarga is not None:
            self.galeria_estado_txt.config(text="Ya hay una descarga en curso; espera a que termine.")
            return
        ext = os.path.splitext(nombre)[1]
        ruta = filedialog.asksaveasfilename(
            parent=self.galeria, title="Guardar archivo descargado",
            initialfile=nombre, defaultextension=ext,
            filetypes=[("Archivo", "*" + ext)],
        )
        if not ruta:
            return
        if nombre in self.cache_vista:
            # ya se recibió para la vista previa: se guarda directamente, sin volver a pedirlo
            self.guardar_en_disco(ruta, self.cache_vista[nombre])
            return
        if self.descarga is not None:
            self.galeria_estado_txt.config(text="Ya hay una descarga en curso; espera a que termine.")
            return
        self.descarga = {"nombre": nombre, "tamano": None, "ruta": ruta, "datos": [], "modo": "descarga"}
        self.galeria_estado_txt.config(text=f"Descargando {nombre}…")
        if not self.enviar_crudo(f"/descargar {nombre}"):
            self.descarga = None
            self.galeria_estado_txt.config(text="No se pudo pedir la descarga.")

    def galeria_mensaje(self, texto):
        if self.galeria is not None and self.galeria.winfo_exists():
            self.galeria_estado_txt.config(text=texto)
        self.sistema(texto)

    # ---------- conexión ----------
    def alternar_conexion(self):
        if self.conn or self.quiere:
            self.desconectar()
        else:
            self.quiere = True
            self.conectar(auto=False)

    def conectar(self, auto):
        if self.reintento_id:
            self.root.after_cancel(self.reintento_id)
            self.reintento_id = None
        ip = self.ip_var.get().strip()
        if not ip:
            self.quiere = False
            self.estado("Necesito una IP.")
            return
        self.intento_auto = auto
        self.gen += 1
        gen = self.gen
        self.btn_con.config(state="disabled")
        self.estado(f"Conectando a {ip}:{PUERTO}…")
        threading.Thread(target=self.hilo_red, args=(ip, gen), daemon=True).start()

    def hilo_red(self, ip, gen):
        try:
            c = socket.create_connection((ip, PUERTO), timeout=5)
        except OSError as e:
            self.cola.put((gen, "error", str(e)))
            return
        c.settimeout(None)
        self.cola.put((gen, "conectado", (c, ip)))
        try:
            f = c.makefile("r", encoding="utf-8", errors="replace")
            for linea in f:
                self.cola.put((gen, "linea", linea.rstrip("\r\n")))
        except (OSError, ValueError):
            pass
        self.cola.put((gen, "cierre", None))

    def cerrar_socket(self):
        c, self.conn = self.conn, None
        if c:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def desconectar(self):
        self.quiere = False
        if self.reintento_id:
            self.root.after_cancel(self.reintento_id)
            self.reintento_id = None
        self.gen += 1  # ignora lo que aún llegue de la conexión vieja
        habia = self.conn is not None
        self.cerrar_socket()
        self.al_desconectar()
        if habia:
            self.sistema("Te has desconectado")
        self.estado("Desconectado.")

    def al_desconectar(self):
        self.registrado = False
        self.listas_silenciosas = 0
        self.lista.delete(0, "end")
        self.colores = {}
        # self.tags_nombre NO se reinicia al desconectar: las etiquetas usr0, usr1...
        # siguen asociadas a los mensajes ya escritos (si se reutilizaran, se
        # recolorearía el historial de otra persona)
        self.archivos = {}
        self.descarga = None
        self.cache_vista = {}
        self.vista_pendiente = None
        self.vista_actual = None
        self.galeria_mostrar()
        self.galeria_botones()
        self.vista_aviso("Conéctate al servidor para ver los archivos.")
        self.root.title("Chat LAN")
        self.btn_con.config(state="normal")
        self.ui_estado(False)

    def programar_reintento(self):
        self.estado(f"Sin conexión. Reintentando en {REINTENTO_MS // 1000} s…")
        self.btn_con.config(text="Cancelar", state="normal")
        self.reintento_id = self.root.after(REINTENTO_MS, lambda: self.conectar(auto=True))

    def procesar(self):
        try:
            while True:
                gen, tipo, dato = self.cola.get_nowait()
                if gen != self.gen:
                    if tipo == "conectado":  # conexión que ya no interesa
                        try:
                            dato[0].close()
                        except OSError:
                            pass
                    continue
                if tipo == "conectado":
                    self.conn, ip = dato
                    guardar_ip(ip)
                    self.btn_con.config(state="normal")
                    self.ui_estado(True)
                    self.estado(f"Conectado a {ip}:{PUERTO}")
                    self.sistema(f"Conectado a {ip}")
                elif tipo == "linea":
                    self.recibir(dato)
                elif tipo == "error":
                    self.btn_con.config(state="normal")
                    if self.intento_auto and self.quiere:
                        self.programar_reintento()
                    else:
                        self.quiere = False
                        self.al_desconectar()
                        self.sistema(f"No se pudo conectar: {dato}")
                        self.estado("No se pudo conectar. Revisa la IP y que el servidor esté en marcha.")
                elif tipo == "cierre":
                    self.cerrar_socket()
                    self.al_desconectar()
                    self.sistema("Desconectado del servidor")
                    if self.quiere:
                        self.programar_reintento()
                    else:
                        self.estado("Desconectado.")
        except queue.Empty:
            pass
        self.root.after(100, self.procesar)

    # ---------- recepción ----------
    def pedir_lista(self):
        if self.conn and self.registrado:
            self.listas_silenciosas += 1
            self.enviar_crudo("/usuarios")

    def recibir(self, linea):
        # Mensajes de control del servidor (colores, galería, descargas): no se muestran en el chat.
        if linea.startswith("@@"):
            self.procesar_control(linea)
            return

        if linea.strip() == "CONTRASENA":
            clave = simpledialog.askstring("Contraseña del servidor", "Introduce la contraseña:", show="*", parent=self.root)
            if clave is None:
                self.desconectar()
            elif not self.enviar_crudo("/clave " + clave):
                self.sistema("No se pudo enviar la contraseña.")
            return

        if linea.startswith("*** CONTRASENA_INCORRECTA"):
            messagebox.showerror("Acceso denegado", "La contraseña es incorrecta.")
            self.desconectar()
            return

        m = PATRON_HOLA.match(linea)
        if m:
            self.nombre = m.group(1)
            self.registrado = True
            self.password_pedida = False
            self.btn_archivo.config(state="normal")
            self.btn_color.config(state="normal")
            self.root.title(f"Chat LAN — {self.nombre}")
            self.estado(f"Conectado como {self.nombre}")
            self.pedir_lista()
            self.pedir_archivos()
            self.galeria_botones()

        m = PATRON_LISTA.match(linea)
        if m and self.listas_silenciosas > 0:
            self.listas_silenciosas -= 1
            self.lista.delete(0, "end")
            for n in [x for x in m.group(1).split(", ") if x]:
                self.lista.insert("end", n + ("  (tú)" if n == self.nombre else ""))
            return

        m = PATRON_NICK.match(linea)
        if m and m.group(1) == self.nombre:
            self.nombre = m.group(2)
            self.root.title(f"Chat LAN — {self.nombre}")
            self.estado(f"Conectado como {self.nombre}")
        if PATRON_EVENTO.match(linea):
            self.pedir_lista()
        if PATRON_SUBIDA.match(linea):
            self.pedir_archivos()

        self.mostrar(linea)

    def procesar_control(self, linea):
        tipo, _, resto = linea.partition(" ")
        try:
            if tipo == "@@COLOR":
                self.colores = json.loads(resto)
                # actualiza también las etiquetas ya creadas (no espera al siguiente mensaje)
                for nombre, tag in self.tags_nombre.items():
                    color = self.colores.get(nombre)
                    if color:
                        self.txt.tag_config(tag, foreground=oscurecer(color))
            elif tipo == "@@ARCHIVOS":
                self.archivos = json.loads(resto)
                # la memoria de vistas previas solo conserva archivos que siguen igual en el servidor
                self.cache_vista = {n: b for n, b in self.cache_vista.items() if self.archivos.get(n) == len(b)}
                self.galeria_mostrar()
                if self.galeria is not None and self.galeria.winfo_exists():
                    self.galeria_estado_txt.config(text=f"{len(self.archivos)} archivo(s) en el servidor.")
            elif tipo == "@@DATAIN":
                info = json.loads(resto)
                pendiente = self.descarga
                if pendiente and not pendiente.get("descartar") and pendiente.get("nombre") == info.get("nombre"):
                    # es el archivo que este cliente pidió
                    pendiente["tamano"] = info.get("tamano")
                    pendiente["datos"] = []
                else:
                    # transferencia no solicitada o que no coincide: se consume y se descarta
                    self.descarga = {"descartar": True, "datos": []}
            elif tipo == "@@DATA":
                if self.descarga is not None:
                    self.descarga["datos"].append(resto)
            elif tipo == "@@DATAFIN":
                self.completar_descarga()
            elif tipo == "@@ERRARCH":
                d, self.descarga = self.descarga, None
                if d and d.get("modo") == "vista":
                    self.vista_aviso("No se pudo cargar: " + resto, d.get("nombre") or "Vista previa")
                else:
                    self.galeria_mensaje("Error de descarga: " + resto)
                self.vista_siguiente()
        except (ValueError, TypeError):
            pass

    def completar_descarga(self):
        d, self.descarga = self.descarga, None
        try:
            self._completar(d)
        finally:
            self.vista_siguiente()  # si se seleccionó otro archivo mientras tanto, se carga ahora

    def _completar(self, d):
        if d is None or d.get("descartar"):
            return
        vista = d.get("modo") == "vista"
        nombre = d.get("nombre")
        try:
            contenido = base64.b64decode("".join(d.get("datos", [])), validate=True)
        except (ValueError, TypeError):
            if vista:
                self.vista_aviso("El archivo llegó corrupto.", nombre)
            else:
                self.galeria_mensaje("La descarga llegó corrupta.")
            return
        if d.get("tamano") is not None and len(contenido) != d["tamano"]:
            if vista:
                self.vista_aviso("El archivo llegó incompleto.", nombre)
            else:
                self.galeria_mensaje("La descarga está incompleta.")
            return
        self.recordar_vista(nombre, contenido)
        if vista:
            # solo se pinta si sigue siendo el archivo que el usuario está mirando
            if nombre == self.vista_actual and self.vista_pendiente is None:
                self.vista_mostrar(nombre, contenido)
            return
        ruta = d.get("ruta")
        if ruta:
            self.guardar_en_disco(ruta, contenido)

    def recordar_vista(self, nombre, contenido):
        """Guarda el archivo en memoria (máx. ~50 MB; se olvidan primero los más antiguos)."""
        if not nombre:
            return
        self.cache_vista.pop(nombre, None)
        self.cache_vista[nombre] = contenido
        while sum(len(b) for b in self.cache_vista.values()) > 50 * 1024 * 1024 and len(self.cache_vista) > 1:
            self.cache_vista.pop(next(iter(self.cache_vista)))

    def guardar_en_disco(self, ruta, contenido):
        try:
            with open(ruta, "wb") as fh:
                fh.write(contenido)
        except OSError as e:
            self.galeria_mensaje(f"No se pudo guardar el archivo: {e}")
            return
        self.galeria_mensaje(f"Archivo descargado en {ruta}")

    def mostrar(self, linea):
        es_chat = False
        if linea.startswith("***"):
            self.escribir([(f"{hora()} ", "hora"), (linea, "sistema")])
        elif PATRON_PRIV.match(linea):
            h, dir_, quien, msg = PATRON_PRIV.match(linea).groups()
            self.escribir([
                (h + " ", "hora"),
                (f"(privado {dir_}) ", "privado_b"),
                (quien, self.etiqueta_nombre(quien)),
                (": ", "privado_b"),
                (msg, "privado"),
            ])
            es_chat = dir_ == "de"
        elif PATRON_CHAT.match(linea):
            h, nombre, msg = PATRON_CHAT.match(linea).groups()
            self.escribir([(h + " ", "hora"), (nombre, self.etiqueta_nombre(nombre)), (": " + msg, "")])
            es_chat = True
        else:
            self.escribir([(linea, "servidor")])
            if "nombre" in linea.lower():
                self.estado("Escribe tu nombre abajo y pulsa Enter.")
        if es_chat:
            self.avisar(linea)

    def avisar(self, texto):
        if self.root.focus_displayof() is not None:
            return
        self.sin_leer += 1
        self.root.title(f"({self.sin_leer}) Chat LAN — {self.nombre}")
        if not self.silencio.get() and Notification is not None:
            try:
                Notification(app_id="Chat LAN", title="Nuevo mensaje", msg=texto, duration="short").show()
            except Exception:
                pass

    def al_enfocar(self, _evento=None):
        if self.sin_leer:
            self.sin_leer = 0
            self.root.title(f"Chat LAN — {self.nombre}" if self.nombre else "Chat LAN")

    # ---------- envío ----------
    def enviar_crudo(self, texto):
        if not self.conn:
            return False
        try:
            self.conn.sendall((texto + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def elegir_color(self):
        if not (self.conn and self.registrado):
            self.sistema("Conéctate para elegir tu color")
            return
        inicial = self.colores.get(self.nombre)
        while True:
            rgb, hexa = colorchooser.askcolor(color=inicial, parent=self.root, title="Elige el color de tu nombre")
            if not hexa:
                return
            if color_legible(hexa):
                self.enviar_crudo(f"/color {hexa}")
                return
            messagebox.showwarning(
                "Color demasiado claro",
                "Ese color es blanco o casi blanco y tu nombre no se podría leer.\n"
                "Elige un color más oscuro.", parent=self.root)
            inicial = oscurecer(hexa)

    def enviar(self, _evento=None):
        txt = self.entrada.get().strip()
        if not txt:
            return "break"
        self.entrada.delete(0, "end")
        if not self.conn:
            self.sistema("No estás conectado")
            return "break"
        if txt.lower() in ("salir", "/salir"):
            self.desconectar()
            return "break"
        if txt.lower() == "/silencio":
            self.silencio.set(not self.silencio.get())
            self.sistema("Notificaciones " + ("desactivadas" if self.silencio.get() else "activadas"))
            return "break"
        partes = txt.split()
        if partes[0].lower() == "/color" and len(partes) > 1 and re.fullmatch(r"#[0-9a-fA-F]{6}", partes[1]) \
                and not color_legible(partes[1]):
            self.sistema("Ese color es demasiado claro: tu nombre no se podría leer. Elige uno más oscuro")
            return "break"
        if len(txt) > MAX_MSG:
            txt = txt[:MAX_MSG]
            self.sistema(f"Mensaje recortado a {MAX_MSG} caracteres")
        self.historial.append(txt)
        self.pos = len(self.historial)
        if not self.enviar_crudo(txt):
            self.sistema("No se pudo enviar: la conexión se ha cerrado")
            return "break"
        if self.registrado and not txt.startswith("/"):
            self.escribir([(f"{hora()} ", "hora"), ("Tú", self.etiqueta_nombre(self.nombre)), (": " + txt, "")], forzar_abajo=True)
        return "break"

    def navegar(self, d):
        if not self.historial:
            return "break"
        self.pos = max(0, min(len(self.historial), self.pos + d))
        self.entrada.delete(0, "end")
        if self.pos < len(self.historial):
            self.entrada.insert(0, self.historial[self.pos])
        return "break"

    def privado_a_seleccion(self, _evento=None):
        sel = self.lista.curselection()
        if not sel or not self.conn:
            return
        nombre = self.lista.get(sel[0]).replace("  (tú)", "")
        if nombre == self.nombre:
            return
        self.entrada.delete(0, "end")
        self.entrada.insert(0, f"/privado {nombre} ")
        self.entrada.focus_set()
        self.entrada.icursor("end")

    def seleccionar_archivo(self):
        if not self.conn or not self.registrado:
            return
        ruta = filedialog.askopenfilename(
            title="Selecciona un archivo para subir",
            filetypes=[
                ("Archivos permitidos", "*.py *.java *.txt *.jpg *.png"),
                ("Python", "*.py"), ("Java", "*.java"), ("Texto", "*.txt"), ("Imágenes", "*.jpg *.png"),
            ],
        )
        if not ruta:
            return
        extension = os.path.splitext(ruta)[1].lower()
        if extension not in EXTENSIONES_PERMITIDAS:
            messagebox.showerror("Tipo no permitido", "Solo se permiten archivos .py, .java, .txt, .jpg y .png.")
            return
        try:
            tamaño = os.path.getsize(ruta)
            if tamaño > MAX_ARCHIVO:
                messagebox.showerror("Archivo demasiado grande", "El tamaño máximo es 10 MB.")
                return
            with open(ruta, "rb") as fh:
                contenido = fh.read()
        except OSError as e:
            messagebox.showerror("Error", f"No se pudo leer el archivo: {e}")
            return

        nombre = os.path.basename(ruta).replace(" ", "_").replace("\\n", "_").replace("\\r", "_")
        codificado = base64.b64encode(contenido).decode("ascii")
        if not self.enviar_crudo(f"/subir {nombre} {len(contenido)}"):
            self.sistema("No se pudo iniciar la subida.")
            return
        # Las líneas de datos se mantienen por debajo del límite de mensajes del servidor.
        for i in range(0, len(codificado), 400):
            if not self.enviar_crudo("DATA " + codificado[i:i + 400]):
                self.sistema("La conexión se perdió durante la subida.")
                return
        if not self.enviar_crudo("FINARCHIVO"):
            self.sistema("La conexión se perdió al finalizar la subida.")
            return
        self.sistema(f"Archivo enviado al servidor: {nombre}")

    def cerrar(self):
        self.quiere = False
        self.gen += 1
        self.cerrar_socket()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
