# v4.2 — cambios sobre la 4.0 (que sigue funcionando igual):
#   · /privado funciona también con nombres que llevan espacios, p. ej. "/privado Ruben (apodo) hola"
#     (el cliente 4.2 usa /privado para los mensajes directos en pestañas).
#   · La ayuda ya no muestra /archivos, /descargar ni /subir: los usa el programa por dentro
#     (botones "Enviar archivo" y "Archivos"), no se escriben en el chat.

# v3.2 — cambios sobre la 3.1 (que sigue funcionando igual):
#   · Cada usuario elige el color de su nombre (/color #RRGGBB) y se sincroniza en todos los clientes.
#   · Solo se permiten archivos .py, .java, .txt, .jpg y .png (validado en el servidor).
#   · Los archivos se guardan en la carpeta "chat_archivos" y se pueden descargar.
#   · Nuevos comandos: /color, /archivos (lista) y /descargar nombre.
# Lo que ya funcionaba (contraseña, nombres reservados por IP, privados, /nick, etc.) no se ha tocado.

import re
import os
import json
import base64
import socket
import threading
from datetime import datetime

PUERTO = 5000
MAX_MSG = 500
CONTRASENA_SERVIDOR = "1DAMAMM"  # Cámbiala antes de ejecutar el servidor.
MAX_ARCHIVO = 10 * 1024 * 1024
EXTENSIONES_PERMITIDAS = {'.py', '.java', '.txt', '.jpg', '.png'}
CARPETA_SUBIDAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_archivos")

# Colores por defecto (el usuario puede cambiar el suyo con /color #RRGGBB).
PALETA = ["#0b7285", "#9c36b5", "#c92a2a", "#e67700", "#2b8a3e", "#1864ab", "#a61e4d", "#5f3dc4"]
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Asigna aquí un nombre a cada IP.
# Estos nombres quedan RESERVADOS: solo pueden usarlos las IPs de esta lista.
NOMBRES = {
    "127.0.0.1": "Ruben",
    "10.21.0.1": "Miriam",
    "10.21.0.2": "Jorge",
    "10.21.0.3": "Rafa",
    "10.21.0.4": "Ruben",
    "10.21.0.5": "Dani",
    "10.21.0.6": "Sebas",
}

clientes = {}          # conexión -> nombre
ips = {}               # conexión -> IP
colores = {}           # conexión -> "#rrggbb" elegido
locks_envio = {}       # conexión -> cerrojo para que los envíos no se mezclen
lock = threading.Lock()

NOMBRE_RE = re.compile(r"^[\w.\-]{1,20}$")

AYUDA = (
    "Comandos: /usuarios  /privado nombre mensaje  /nick nuevonombre  /color #RRGGBB  /ayuda  "
    "(en tu cliente: /silencio para notificaciones, salir para irte; @nombre para mencionar)"
)


def hora():
    return datetime.now().strftime("%H:%M")


def lock_envio_de(conn):
    with lock:
        if conn not in locks_envio:
            locks_envio[conn] = threading.Lock()
        return locks_envio[conn]


def enviar(conn, texto):
    # Un cerrojo por conexión: varios hilos (chat, descargas...) no pueden
    # escribir a la vez sobre el mismo socket y mezclar líneas.
    with lock_envio_de(conn):
        try:
            conn.sendall((texto + "\n").encode("utf-8"))
            return True
        except OSError:
            return False


def enviar_a_todos(texto, excepto=None):
    # Se copia la lista y se envía fuera del lock para que un cliente lento no bloquee a todos
    with lock:
        destinos = [c for c in clientes if c is not excepto]
    for c in destinos:
        enviar(c, texto)


def nombre_valido(n):
    return bool(NOMBRE_RE.match(n))


def reservado_para_otro(nombre, ip):
    """True si el nombre (o ese nombre con número al final, p. ej. Miriam2)
    pertenece a otra IP de NOMBRES. Así nadie puede hacerse pasar por Miriam."""
    base = nombre.rstrip("0123456789").lower()
    for ip_dueña, n in NOMBRES.items():
        if ip_dueña == ip:
            continue
        if nombre.lower() == n.lower() or (base and base == n.lower()):
            return True
    return False


def nombre_libre(nombre):
    """Llamar con el lock cogido. Evita nombres repetidos añadiendo un número."""
    usados = {n.lower() for n in clientes.values()}
    base, i = nombre, 2
    while nombre.lower() in usados:
        nombre = f"{base}{i}"
        i += 1
    return nombre


def buscar_conn(nombre):
    """Llamar con el lock cogido."""
    for c, n in clientes.items():
        if n.lower() == nombre.lower():
            return c
    return None


def color_libre():
    """Primer color de la paleta que no esté usando nadie.
    Llamar con el lock ya cogido (no vuelve a cogerlo)."""
    usados = set(colores.values())
    for c in PALETA:
        if c not in usados:
            return c
    return "#495057"


def enviar_colores():
    """Difunde la tabla nombre -> color a todos los conectados."""
    with lock:
        tabla = {clientes[c]: colores.get(c) for c in clientes}
    enviar_a_todos("@@COLOR " + json.dumps(tabla))


def drenar_subida(f):
    """Si una subida se rechaza, descarta las líneas DATA pendientes hasta FINARCHIVO
    para que no acaben apareciendo como mensajes de chat.
    Si el terminador no llega (cliente roto o malicioso), cierra la conexión."""
    for _ in range(60000):
        linea = f.readline()
        if not linea:
            raise ConnectionError("subida sin terminar: cliente desconectado")
        if linea.strip() == "FINARCHIVO":
            return
    raise ConnectionError("subida sin terminar: límite de drenado agotado")


def comando(conn, linea, f=None):
    partes = linea.split(maxsplit=2)
    cmd = partes[0].lower()

    if cmd == "/ayuda":
        enviar(conn, "*** " + AYUDA + " ***")

    elif cmd == "/usuarios":
        with lock:
            lista = ", ".join(sorted(clientes.values(), key=str.lower))
        enviar(conn, f"*** Conectados: {lista} ***")

    elif cmd == "/color":
        argumento = linea[len("/color"):].strip()
        if not COLOR_RE.match(argumento):
            enviar(conn, "*** Uso: /color #RRGGBB (p. ej. /color #1864ab) ***")
            return
        color = argumento.lower()
        with lock:
            colores[conn] = color
        enviar(conn, f"*** Tu color es ahora {color} ***")
        enviar_colores()

    elif cmd == "/archivos":
        try:
            entradas = sorted(os.listdir(CARPETA_SUBIDAS))
        except OSError:
            entradas = []
        tabla = {}
        for e in entradas:
            ruta = os.path.join(CARPETA_SUBIDAS, e)
            if os.path.isfile(ruta) and os.path.splitext(e)[1].lower() in EXTENSIONES_PERMITIDAS:
                try:
                    tabla[e] = os.path.getsize(ruta)
                except OSError:
                    pass
        enviar(conn, "@@ARCHIVOS " + json.dumps(tabla))

    elif cmd == "/descargar":
        resto = linea[len("/descargar"):].strip()
        nombre_archivo = os.path.basename(resto)
        extension = os.path.splitext(nombre_archivo)[1].lower()
        if not resto or nombre_archivo != resto or extension not in EXTENSIONES_PERMITIDAS:
            enviar(conn, "@@ERRARCH Ese archivo no está disponible")
            return
        ruta = os.path.join(CARPETA_SUBIDAS, nombre_archivo)
        if not os.path.isfile(ruta):
            enviar(conn, "@@ERRARCH Ese archivo no existe en el servidor")
            return
        try:
            with open(ruta, "rb") as fh:
                contenido = fh.read(MAX_ARCHIVO + 1)
        except OSError:
            enviar(conn, "@@ERRARCH No se pudo leer el archivo")
            return
        if len(contenido) > MAX_ARCHIVO:
            enviar(conn, "@@ERRARCH El archivo es demasiado grande")
            return
        enviar(conn, "@@DATAIN " + json.dumps({"nombre": nombre_archivo, "tamano": len(contenido)}))
        codificado = base64.b64encode(contenido).decode("ascii")
        for i in range(0, len(codificado), 400):
            if not enviar(conn, "@@DATA " + codificado[i:i + 400]):
                return
        enviar(conn, "@@DATAFIN")

    elif cmd == "/nick":
        nuevo = linea[len(partes[0]):].strip()
        if not nuevo:
            enviar(conn, "*** Uso: /nick nuevonombre ***")
            return
        error = ""
        with lock:
            # validación y asignación dentro del mismo bloque: dos clientes no
            # pueden validar el mismo nombre a la vez y adoptarlo ambos
            ip = ips.get(conn, "")
            nombre_reservado = NOMBRES.get(ip)
            if nombre_reservado:
                # Las IP reservadas conservan su identidad: Nombre (nombrenuevo).
                patron = re.compile(r"^" + re.escape(nombre_reservado) + r" \(([\w.\-]{1,20})\)$")
                if not patron.match(nuevo):
                    error = f"*** Esta IP solo puede cambiar el nombre con el formato: {nombre_reservado} (nombrenuevo) ***"
            elif not nombre_valido(nuevo):
                error = "*** Uso: /nick nombre (sin espacios ni símbolos raros, máx. 20 caracteres) ***"
            if not error and reservado_para_otro(nuevo, ip):
                error = "*** Ese nombre está reservado para otra persona ***"
            if not error:
                otro = buscar_conn(nuevo)
                if otro is not None and otro is not conn:
                    error = "*** Ese nombre ya está en uso ***"
            if not error:
                viejo = clientes[conn]
                clientes[conn] = nuevo
        if error:
            enviar(conn, error)  # fuera del lock: enviar() vuelve a cogerlo
            return
        enviar_a_todos(f"*** {viejo} ahora se llama {nuevo} ***")
        enviar_colores()  # el color se conserva aunque cambies de nombre

    elif cmd == "/subir":
        if f is None:
            enviar(conn, "*** No se puede recibir el archivo en este momento ***")
            return
        partes_subida = linea.split(maxsplit=2)
        if len(partes_subida) != 3:
            enviar(conn, "*** Uso interno de subida no válido ***")
            drenar_subida(f)
            return
        nombre_archivo, tamaño_txt = partes_subida[1], partes_subida[2]
        nombre_archivo = os.path.basename(nombre_archivo).replace("\\x00", "")
        extension = os.path.splitext(nombre_archivo)[1].lower()
        try:
            tamaño = int(tamaño_txt)
        except ValueError:
            enviar(conn, "*** Tamaño de archivo no válido ***")
            drenar_subida(f)
            return
        if not nombre_archivo or extension not in EXTENSIONES_PERMITIDAS:
            enviar(conn, "*** Tipo de archivo no permitido: solo .py, .java, .txt, .jpg y .png ***")
            drenar_subida(f)
            return
        if tamaño < 0 or tamaño > MAX_ARCHIVO:
            enviar(conn, "*** El tamaño máximo permitido es 10 MB ***")
            drenar_subida(f)
            return
        trozos = []
        total = 0
        while True:
            linea_datos = f.readline()
            if not linea_datos:
                enviar(conn, "*** Subida interrumpida ***")
                return
            linea_datos = linea_datos.strip()
            if linea_datos == "FINARCHIVO":
                break
            if not linea_datos.startswith("DATA "):
                enviar(conn, "*** Protocolo de subida no válido ***")
                drenar_subida(f)
                return
            bloque = linea_datos[5:]
            total += len(bloque)
            if total > ((MAX_ARCHIVO + 2) // 3) * 4 + 8:
                enviar(conn, "*** Archivo demasiado grande ***")
                drenar_subida(f)
                return
            trozos.append(bloque)
        try:
            contenido = base64.b64decode("".join(trozos), validate=True)
        except (ValueError, base64.binascii.Error):
            enviar(conn, "*** El archivo recibido no es válido ***")
            return
        if len(contenido) != tamaño:
            enviar(conn, "*** El tamaño recibido no coincide ***")
            return
        os.makedirs(CARPETA_SUBIDAS, exist_ok=True)
        base, ext = os.path.splitext(nombre_archivo)
        destino = os.path.join(CARPETA_SUBIDAS, nombre_archivo)
        contador = 2
        while True:
            try:
                with open(destino, "xb") as archivo:  # creación exclusiva: evita sobrescrituras simultáneas
                    archivo.write(contenido)
                break
            except FileExistsError:
                destino = os.path.join(CARPETA_SUBIDAS, f"{base}_{contador}{ext}")
                contador += 1
            except OSError:
                enviar(conn, "*** No se pudo guardar el archivo en el servidor ***")
                return
        enviar(conn, f"*** Archivo recibido y guardado en el servidor: {os.path.basename(destino)} ***")
        enviar_a_todos(f"*** {clientes.get(conn, 'Un usuario')} ha subido el archivo {os.path.basename(destino)} ***", excepto=conn)

    elif cmd == "/privado":
        if len(partes) < 3:
            enviar(conn, "*** Uso: /privado nombre mensaje ***")
            return
        destino, texto = partes[1], partes[2]
        resto = linea[len(partes[0]):].strip()
        with lock:
            # se busca el nombre conectado más largo que encaje, así funcionan también
            # los nombres con espacios ("Ruben (apodo)") aunque haya otro "Ruben"
            c_dest = None
            for c, n in sorted(clientes.items(), key=lambda par: -len(par[1])):
                if resto.lower().startswith(n.lower() + " ") and resto[len(n):].strip():
                    c_dest, texto = c, resto[len(n):].strip()
                    break
            origen = clientes[conn]
            nombre_dest = clientes.get(c_dest)
        if c_dest is None:
            enviar(conn, f"*** No hay nadie conectado llamado {destino} ***")
            return
        enviar(c_dest, f"[{hora()}] (privado) de {origen}: {texto}")
        enviar(conn, f"[{hora()}] (privado) para {nombre_dest}: {texto}")

    else:
        enviar(conn, "*** Comando desconocido. Escribe /ayuda ***")


def pedir_nombre(conn, f, ip):
    """Pide un nombre hasta que sea válido y no esté reservado. None si se desconecta."""
    enviar(conn, "Tu IP no tiene nombre asignado. Escribe tu nombre:")
    while True:
        linea = f.readline()
        if not linea:
            return None
        candidato = linea.strip().replace(" ", "_")[:20]
        if not candidato:
            enviar(conn, "El nombre no puede estar vacío. Escribe tu nombre:")
        elif not nombre_valido(candidato):
            enviar(conn, "Usa solo letras, números, punto, guion o guion bajo. Escribe tu nombre:")
        elif reservado_para_otro(candidato, ip):
            enviar(conn, "Ese nombre está reservado para otra persona. Elige otro:")
        else:
            return candidato


def atender(conn, addr):
    ip = addr[0]
    f = conn.makefile("r", encoding="utf-8", errors="replace")
    nombre = NOMBRES.get(ip)
    registrado = False
    try:
        enviar(conn, "CONTRASENA")
        respuesta_clave = f.readline().strip()
        if not respuesta_clave.startswith("/clave ") or respuesta_clave[len("/clave "):] != CONTRASENA_SERVIDOR:
            enviar(conn, "*** CONTRASENA_INCORRECTA ***")
            return
        if not nombre:
            nombre = pedir_nombre(conn, f, ip)
            if nombre is None:
                return

        with lock:
            nombre = nombre_libre(nombre)
            clientes[conn] = nombre
            ips[conn] = ip
            colores[conn] = color_libre()
        registrado = True

        print(f"[+] {nombre} conectado desde {ip}")
        enviar(conn, f"Hola {nombre}, ya estás en el chat. Escribe /ayuda para ver los comandos.")
        enviar_a_todos(f"*** {nombre} se ha conectado ***", excepto=conn)
        enviar_colores()

        for linea in f:
            linea = linea.strip()[:MAX_MSG]
            if not linea:
                continue
            if linea.startswith("/"):
                comando(conn, linea, f)
                continue
            with lock:
                nombre = clientes[conn]
            print(f"[{hora()}] {nombre}: {linea}")
            enviar_a_todos(f"[{hora()}] {nombre}: {linea}", excepto=conn)
    except (OSError, ValueError):
        pass
    finally:
        with lock:
            nombre = clientes.pop(conn, nombre)
            ips.pop(conn, None)
            colores.pop(conn, None)
            locks_envio.pop(conn, None)
        try:
            conn.close()
        except OSError:
            pass
        if registrado:
            print(f"[-] {nombre} desconectado")
            enviar_a_todos(f"*** {nombre} se ha desconectado ***")
            enviar_colores()


def main():
    os.makedirs(CARPETA_SUBIDAS, exist_ok=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PUERTO))
    s.listen()
    print(f"Servidor activo en el puerto {PUERTO}. Ctrl+C para parar.")
    try:
        while True:
            conn, addr = s.accept()
            threading.Thread(target=atender, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
