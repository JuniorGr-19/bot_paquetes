# bot_paquetes.py
# Llena el PAQUETE de las SOTs en Witlink, sede por sede, a partir de lo que dice SGA.
#
# Por cada sede de SEDES (ANCASH, CAJAMARCA Chota/Jaén, LA LIBERTAD Witlink/Atel, LAMBAYEQUE, PIURA):
#   1) WITLINK: 'Cambiar dep' -> SEDES -> departamento -> sede; abre la programación del día (hoy o DIAS_ATRAS)
#      y lee de la tabla las SOTs con Edo. Contrata ATENDIDA o EN ATENCION.
#   2) SGA - CONTRATISTA ALTA BAJA HFC -> WITLINK S.A.C: Buscar, pega las SOTs (tacho, pegar, confirmar,
#      palomita, Enter) y recorre la lista SOT por SOT:
#        - INSTALACION (26 tipos): paquete según el Detalle Servicios (PLAY, TLF, DECOS, MESH);
#        - paquete FIJO: traslado interno, cambio de equipo, punto adicional, bajas, recojos;
#        - WA ENTREGA / ACCESORIO ADICIONAL / DECO ADICIONAL: cuenta mesh o decos del Detalle Servicios;
#        - CAMBIO DE PLAN / SERVICIOS MENORES: compara con la SOT anterior en Consulta Histórica
#          (la anterior: mismo Customer ID, Atendida, y de INSTALACION, MIGRACION, CAMBIO DE PLAN o PORTABILIDAD).
#   3) SGA - CONTRATISTA MANTO HFC -> WITLINK S.A.C: pega los mantenimientos y en Agendamiento lee el
#      Motivo Solución de la fila Ejecutado -> código (codigos_solucion.json) -> 'XX00 - SIN SERVICIO'.
#   4) WITLINK: coloca los paquetes en la fila ATENDIDA (o EN ATENCION) de cada SOT y pulsa Guardar.
# Al final muestra un aviso con el resumen de todas las sedes. Todo queda en bot_log.txt y en registros/.
#
# Requisitos (solo la primera vez): python -m pip install playwright pywinauto
# Uso: python bot_paquetes.py

import ctypes                                # ctypes permite llamar a funciones de Windows (saber qué ventana está al frente)
import datetime                              # datetime permite calcular la fecha de ayer
import json                                  # json guarda el avance en progreso_paquetes.json
import os                                    # os arma las rutas de los archivos del bot
import re                                    # re permite buscar palabras completas en los servicios
import subprocess                            # subprocess permite usar el comando 'clip' de Windows (copiar)
import sys                                   # sys permite copiar todo lo que se imprime a bot_log.txt
import time                                  # time permite hacer pausas
import unicodedata                           # unicodedata permite quitar tildes (Instalación = INSTALACION)

# Se importa antes que Playwright para evitar conflictos al inicializar componentes de Windows
from pywinauto import Desktop, clipboard, keyboard, mouse   # ventanas, portapapeles, teclado y mouse
from pywinauto.controls.common_controls import TreeViewWrapper   # permite leer y pulsar los nodos del árbol de SGA
from playwright.sync_api import sync_playwright   # sync_playwright controla el navegador

# ---------------- CONFIGURACIÓN ----------------
URL_PROGRAMACION = "https://www.witlink.com.pe/sots/programacion.php"   # página de programación
# El bot usa su propio Chrome (perfil_chrome guarda tu sesión de Witlink); si está cerrado, lo abre
CHROME_DEPURACION = "http://127.0.0.1:9222"  # dirección por la que el bot se conecta a ese Chrome
def _buscar_chrome():
    """Ruta de Chrome en esta PC (sirve aunque el bot se copie a otra computadora)."""
    candidatos = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    ]
    return next((c for c in candidatos if os.path.exists(c)), candidatos[0])


CHROME_EXE = _buscar_chrome()                # Chrome instalado en esta PC
PERFIL_CHROME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perfil_chrome")   # recuerda tu sesión
DEPARTAMENTO = "ANCASH"                      # departamento en curso (lo va cambiando el recorrido SEDES)
# Sedes que se recorren en orden (departamento, sede dentro del departamento o None)
SEDES = [
    ("ANCASH", None),
    ("CAJAMARCA", "chota"),
    ("CAJAMARCA", "jaen"),
    ("LA LIBERTAD", "witlink"),
    ("LA LIBERTAD", "atel"),
    ("LAMBAYEQUE", None),
    ("PIURA", None),
]
SEDE_ACTUAL = None                           # sede dentro del departamento que se está trabajando (o None)
DIAS_ATRAS = 0                               # 0 = hoy; 1 = ayer; 2 = anteayer...
ESPERA_LOGIN_MS = 5 * 60 * 1000              # 5 minutos (en milisegundos) para que inicies sesión a mano
ESPERA_CORTA_MS = 15 * 1000                  # 15 segundos para los pasos que ya no dependen de ti
ESTADOS_BUSCADOS = ["ATENDIDA", "EN ATENCION"]   # estados de las SOTs que se capturan y se llenan
MANTENIMIENTOS_WITLINK = []                  # lo llena leer_sots_witlink: SOTs de mantenimiento de la sede
FUERA_TOA_WITLINK = []                         # y las 'Fuera de TOA' (su tipo real se ve en SGA)


# ---------------- PANTALLA: ESCALA Y CLICS QUE SIRVEN EN CUALQUIER MONITOR ----------------
# SGA (PowerBuilder) trabaja como si la pantalla estuviera al 100% aunque Windows esté al 125%, 150%...
# Por eso todas las posiciones fijas del bot se escriben "al 100%" (en píxeles lógicos) y se multiplican por el
# factor de la PC donde se ejecuta. Así funciona igual en un monitor chico, uno grande o con otra escala.
def factor_escala(h):
    """Cuánto hay que multiplicar las posiciones de SGA para llevarlas a la pantalla real (1.0, 1.25, 1.5...)."""
    u = ctypes.windll.user32
    try:
        dpi_ventana = u.GetDpiForWindow(ctypes.c_void_p(h)) or 96   # 96 = SGA trabaja "al 100%"
        monitor = u.MonitorFromWindow(ctypes.c_void_p(h), 2)
        x, y = ctypes.c_uint(96), ctypes.c_uint(96)
        ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(monitor), 0, ctypes.byref(x), ctypes.byref(y))
        return x.value / dpi_ventana
    except Exception:                        # Windows antiguo: sin escala
        return 1.0


def punto_en(h, x_logico, y_logico, desde_derecha=False):
    """Punto de pantalla a (x, y) píxeles lógicos de la esquina superior izquierda (o derecha) de la ventana h."""
    r = RECTANGULO()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))
    f = factor_escala(h)
    x = r.right - int(x_logico * f) if desde_derecha else r.left + int(x_logico * f)
    return x, r.top + int(y_logico * f)


def _paginas_de(barra):
    """Las páginas (pestañas) de una barra, en el mismo orden en que se ven de izquierda a derecha."""
    return [h for h in _hijos_livianos(barra) if _padre(h) == barra and _clase_liviana(h) == "FNUDO3170"]


def _pagina_abierta(paginas):
    """Cuál de las páginas está a la vista (o None)."""
    return next((h for h in paginas if _visible(h)), None)


def _clic_y_esperar(ventana, barra, paginas, x_log, y_log, abierta_antes, segundos=4):
    """Clic en (x_log, y_log) de la barra y espera a que cambie la página visible. Devuelve la página abierta."""
    px, py = punto_en(barra, x_log, y_log)
    _poner_al_frente(ventana)
    mouse.click(button="left", coords=(px, py))
    limite = time.time() + segundos
    abierta = _pagina_abierta(paginas)
    while time.time() < limite and abierta == abierta_antes:
        time.sleep(0.4)
        _esperar_sin_colgar(ventana)
        abierta = _pagina_abierta(paginas)
    return abierta


def clic_pestana(ventana, pagina, x_logico, y_logico, desde_derecha=False, nombre=""):
    """Abre una pestaña de PowerBuilder (Detalle Servicios, Agendamiento, Buscar, Solicitudes de OT...).
    1) Clic en su posición (escalada a esta pantalla) y se confirma que SU página quedó visible.
    2) Si cayó en otra pestaña (en otra PC caben más o menos pestañas), el bot mira EN CUÁL cayó y,
       como sabe el orden de las pestañas, se corre a la izquierda o a la derecha hasta dar con la buscada.
    3) Último recurso: recorre toda la barra. Un clic en otra pestaña no hace daño."""
    if _visible(pagina):
        return pagina
    barra = _padre(pagina)                   # la barra de pestañas contiene a la página
    paginas = _paginas_de(barra)
    objetivo = paginas.index(pagina)
    r = RECTANGULO()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(barra), ctypes.byref(r))
    f = factor_escala(barra)
    ancho_logico = int((r.right - r.left) / f)
    x_log = ancho_logico - x_logico if desde_derecha else x_logico   # todo se mide desde la izquierda

    print(f"Clic en la pestaña '{nombre}': x={punto_en(barra, x_log, y_logico)[0]}, y={punto_en(barra, x_log, y_logico)[1]}")
    abierta = _clic_y_esperar(ventana, barra, paginas, x_log, y_logico, _pagina_abierta(paginas), segundos=10)
    if abierta == pagina:
        return pagina

    # 2) Corrección dirigida: se mira en qué pestaña cayó y se avanza hacia la buscada, de a poquito
    vistos = set()
    for _ in range(60):
        if abierta == pagina:
            print(f"   Pestaña '{nombre}' encontrada (corregida a x={punto_en(barra, x_log, y_logico)[0]}).")
            return pagina
        if abierta not in paginas:
            break
        cayo = paginas.index(abierta)
        print(f"   Cayó en '{_texto_liviano(abierta)}'; me corro a la {'izquierda' if cayo > objetivo else 'derecha'}...")
        x_log += -15 if cayo > objetivo else 15
        if not 5 <= x_log <= ancho_logico - 5 or (x_log, cayo) in vistos:
            break
        vistos.add((x_log, cayo))
        abierta = _clic_y_esperar(ventana, barra, paginas, x_log, y_logico, abierta)

    # 3) Último recurso: recorrer toda la barra (las pestañas pueden estar en 1 o 2 filas)
    print(f"   La pestaña '{nombre}' no se abrió en su lugar; la busco por toda la barra de pestañas...")
    for fila_y in (y_logico, y_logico - 24, y_logico + 24):
        if fila_y <= 0:
            continue
        for x_scan in range(10, ancho_logico, 20):
            if _clic_y_esperar(ventana, barra, paginas, x_scan, fila_y, _pagina_abierta(paginas), segundos=1.5) == pagina:
                print(f"   Pestaña '{nombre}' encontrada en x={punto_en(barra, x_scan, fila_y)[0]}.")
                return pagina
    raise RuntimeError(f"No logré abrir la pestaña '{nombre}' en SGA.")


def clic_primera_fila(ventana, lista):
    """Clic en la primera fila de la lista de Control de Tareas (en la columna SOT), en cualquier pantalla."""
    barra = _barra_vertical(lista)
    if barra is not None:
        _desplazar(lista, barra, SB_TOP)     # la lista arriba del todo
    x, y = punto_en(lista.handle, PRIMERA_FILA_X, PRIMERA_FILA_Y)
    _poner_al_frente(ventana)
    print(f"Clic en la primera SOT: x={x}, y={y}")
    mouse.click(button="left", coords=(x, y))
    time.sleep(1)
    _esperar_sin_colgar(ventana)


# ---------------- PASO 1: WITLINK PROGRAMACIÓN ----------------
def conectar_chrome(p):
    """Se conecta al Chrome del bot (lo abre si está cerrado) y devuelve (navegador, pestaña de Programación).
    No abre ventanas nuevas: usa la pestaña donde ya estás en Programación."""
    try:
        navegador = p.chromium.connect_over_cdp(CHROME_DEPURACION)
    except Exception:
        # Chrome del bot cerrado: se abre con su perfil (perfil_chrome) y se vuelve a intentar
        print("Chrome del bot cerrado; abriéndolo...")
        subprocess.Popen([CHROME_EXE, "--remote-debugging-port=9222", f"--user-data-dir={PERFIL_CHROME}",
                          "--start-maximized", URL_PROGRAMACION])
        navegador = None
        for _ in range(30):                  # hasta 30 segundos a que Chrome termine de abrir
            time.sleep(1)
            try:
                navegador = p.chromium.connect_over_cdp(CHROME_DEPURACION)
                break
            except Exception:
                continue
        if navegador is None:
            raise RuntimeError("No pude abrir ni conectarme a Chrome. Ciérralo por completo y vuelve a ejecutar.")
    pestanas = [pg for ctx in navegador.contexts for pg in ctx.pages]
    for pestana in pestanas:
        if "programacion.php" in pestana.url:
            pestana.bring_to_front()
            print("Usando tu pestaña de Chrome:", pestana.url)
            return navegador, pestana
    # Ninguna pestaña en Programación: se usa la primera y se va a Programación
    pestana = pestanas[0] if pestanas else navegador.contexts[0].new_page()
    pestana.goto(URL_PROGRAMACION)
    pestana.bring_to_front()
    print("Pestaña de Chrome llevada a Programación.")
    return navegador, pestana


def abrir_programacion(page):
    """Espera a que la pestaña esté en Programación con sesión iniciada (no navega a otra página)."""
    print("Esperando la página de Programación (si te pide iniciar sesión, ingresa tus datos)...")
    if "login" in page.url.lower():          # la sesión de Witlink venció: hay que iniciarla a mano
        page.bring_to_front()
        print(">>> Witlink te pide INICIAR SESIÓN en la ventana de Chrome del bot. Ingresa tus datos;")
        print(">>> el bot sigue solo en cuanto veas la página de Programación (tienes 5 minutos).")
    # Con la sesión iniciada se ve el botón SEDES, la lista de tarjetas (a.dep-card) o el botón "Cambiar dep"
    page.locator("#prog-sedes-open").or_(page.locator("a.dep-card")).or_(page.get_by_text("Cambiar dep")).first.wait_for(
        state="visible", timeout=ESPERA_LOGIN_MS
    )
    print("Página de Programación detectada.")


def abrir_ventana_sedes(page, departamento):
    """Pulsa el botón SEDES hasta que se vea la tarjeta del departamento en la ventana de sedes."""
    tarjeta = page.locator("#prog-sedes-overlay").get_by_text(departamento, exact=True).first
    for intento in range(1, 6):              # hasta 5 intentos
        if tarjeta.is_visible():             # la ventana ya muestra la tarjeta
            return tarjeta
        boton = page.locator("#prog-sedes-open").filter(visible=True)
        if boton.count():
            boton.first.click(force=True)    # force: aunque algo lo tape, se pulsa igual
            print(f"Clic en 'SEDES' (intento {intento}).")
        try:
            tarjeta.wait_for(state="visible", timeout=3000)
            return tarjeta
        except Exception:
            continue
    raise RuntimeError("Pulsé SEDES pero no se abrió la ventana de sedes.")


def ir_a_sede(page, departamento, sede, fecha, departamento_anterior):
    """Deja Witlink en el departamento y la sede indicados.
    - Mismo departamento que el anterior (Chota -> Jaén, Witlink -> Atel): SOLO se pulsa el botón de la sede.
    - Otro departamento: 'Cambiar dep' (si no es el primero) -> SEDES -> tarjeta del departamento -> sede."""
    en_enlace = departamento.replace(" ", "+")
    if departamento != departamento_anterior:
        if departamento_anterior is not None:
            cambiar = page.locator('a[href*="reset=1"]').filter(visible=True)   # botón 'Cambiar dep'
            if cambiar.count():
                cambiar.first.click()
                page.wait_for_load_state("domcontentloaded")
                print("Clic en 'Cambiar dep'.")
        try:
            tarjeta = abrir_ventana_sedes(page, departamento)
            tarjeta.click()
            print(f"Clic en la tarjeta {departamento}.")
            page.locator("#btn-prog-cal").wait_for(state="visible", timeout=ESPERA_CORTA_MS)
        except Exception as error:           # si algo no se ve, se abre la programación del departamento directo
            print(f"No pude entrar por SEDES ({error}); abro la programación de {departamento} directamente.")
            page.goto(f"{URL_PROGRAMACION}?dep={en_enlace}")
            page.locator("#btn-prog-cal").wait_for(state="visible", timeout=ESPERA_CORTA_MS)
        insignia = page.locator(".prog-dep-badge").first.inner_text().strip().upper()
        if not insignia.startswith(departamento):   # puede decir 'CAJAMARCA · CHOTA' o 'LA LIBERTAD · WITLINK'
            raise RuntimeError(f"Quería abrir {departamento} pero Witlink muestra {insignia}.")
        print(f"Departamento {departamento} abierto.")
    else:
        print(f"Sigo en {departamento}; solo cambio de sede.")

    if sede:                                 # Chota / Jaén, Witlink / Atel
        boton = page.locator(f'a.prog-sede-btn[href*="sede={sede}"]')
        boton.first.wait_for(state="visible", timeout=ESPERA_CORTA_MS)
        if "is-active" not in (boton.first.get_attribute("class") or ""):
            boton.first.click()
            page.wait_for_load_state("domcontentloaded")
        page.locator(f'a.prog-sede-btn.is-active[href*="sede={sede}"]').wait_for(
            state="visible", timeout=ESPERA_CORTA_MS)
        print(f"Sede {sede.upper()} abierta.")


def url_del_dia(fecha):
    """Dirección de la programación de ese día (y de la sede actual, si hay)."""
    url = f"{URL_PROGRAMACION}?fecha={fecha:%Y-%m-%d}"
    return url + (f"&sede={SEDE_ACTUAL}" if SEDE_ACTUAL else "")


def nombre_sede():
    return f"{DEPARTAMENTO} - {SEDE_ACTUAL.upper()}" if SEDE_ACTUAL else DEPARTAMENTO


# ---------------- PASO 2: CAPTURAR SOTs ATENDIDAS ----------------
# Tipos de trabajo de la categoría INSTALACION (lista del SGA, 29/09/2026). Solo estas SOTs llevan paquete.
TIPOS_INSTALACION = [
    "FTTH - INSTALACION",
    "FTTH - INSTALACION MESH",
    "FTTH - MIGRACION",
    "FTTH - PORTABILIDAD INSTALACIONES PAQUETES CLARO",
    "FTTH - PV ALTA ACCESORIO ADICIONAL",
    "FTTH - PV MIGRACION HFC A FTTH",
    "FTTH - SISACT INSTALACION",
    "FTTH - SISACT INSTALACION PAQUETES TODO CLARO DIGITAL",
    "FTTH Instalacion",
    "FTTH Portabilidad",
    "FTTH Traslado Externo",
    "FTTH/SIAC - MIGRACION TECNOLOGICA DE HFC A FTTH",
    "FTTH/SIAC - TRASLADO EXTERNO",
    "FTTHMI001 - MIGRACION 1 PLAY + RECOJO DE EQUIPOS",
    "HFC - INSTALACION",
    "HFC - INSTALACION PAQUETES",
    "HFC - INSTALACION PAQUETES TODO CLARO DIGITAL",
    "HFC - PORTABILIDAD INSTALACIONES PAQUETES CLARO",
    "HFC - SISACT INSTALACION",
    "HFC - SISACT INSTALACION PAQUETES TODO CLARO DIGITAL",
    "HFC Instalación",
    "HFC Traslado Externo",
    "HFC/SIAC - TRASLADO EXTERNO",
    "INSTALACION",
    "MIGRACION",
    "MIGRACION TECNOLOGICA DE HFC A FTTH",
]


def _normalizar(texto):
    """Mayúsculas, sin tildes y con un solo espacio entre palabras ('HFC Instalación' = 'HFC INSTALACION')."""
    sin_tildes = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.upper().split())


_TIPOS_INSTALACION = {_normalizar(t) for t in TIPOS_INSTALACION}


# Tipos de trabajo con paquete FIJO: no hace falta mirar el detalle ni la Consulta Histórica
PAQUETES_FIJOS = {
    "FTTH - PLUME": "BAJA PARCIAL",
    "FTTH - RECOJO WA MESH": "BAJA PARCIAL",
    "FTTH - RECOJO WA PLUME": "BAJA PARCIAL",
    "HFC - RECOJO WA MESH": "BAJA PARCIAL",
    "HFC - RECOJO WA PLUME": "BAJA PARCIAL",
    "FTTHMES - RECOJO EQUIPOS MESH FTTH": "BAJA PARCIAL",
    "FTTH/SIAC - BAJA ACCESORIO ADICIONAL": "BAJA PARCIAL",
    "FTTH/SIAC - BAJA TOTAL DEL SERVICIO": "BAJA PARCIAL",
    "HFC - BAJA TODO CLARO TOTAL": "BAJA PARCIAL",
    "HFC/SIAC - BAJA ACCESORIO ADICIONAL": "BAJA PARCIAL",
    "HFC/SIAC - BAJA TOTAL DEL SERVICIO": "BAJA PARCIAL",
    "HFC/SIAC - BAJA DECO ALQUILER": "BAJA PARCIAL",
}


def paquete_fijo(tipo):
    """Paquete que no depende de los equipos (o None):
    - todo lo que diga TRASLADO INTERNO (incluye 'CE HFC - TRASLADO INTERNO') -> 'TRASLADO INTERNO';
    - todo lo que diga CAMBIO DE EQUIPO -> 'CAMBIO DE EQUIPO';
    - todo lo que diga PUNTO ADICIONAL -> 'PUNTO ADICIONAL';
    - FTTH - PLUME, los RECOJO (WA MESH / WA PLUME / EQUIPOS MESH) y las BAJAS -> 'BAJA PARCIAL'."""
    texto = _normalizar(tipo)
    if "TRASLADO INTERNO" in texto:
        return "TRASLADO INTERNO"
    if "CAMBIO DE EQUIPO" in texto:          # FTTH/SIAC, HFC y HFC/SIAC - CAMBIO DE EQUIPO
        return "CAMBIO DE EQUIPO"
    if "PUNTO ADICIONAL" in texto:           # FTTH/SIAC, FTTHC/SIAC, HFC y HFC/SIAC - PUNTO ADICIONAL
        return "PUNTO ADICIONAL"
    return next((p for t, p in PAQUETES_FIJOS.items() if _normalizar(t) == texto), None)


# Entregas de mesh/plume/repetidor: el paquete sale del Detalle Servicios (sin Consulta Histórica)
TIPOS_ENTREGA_MESH = [
    "FTTH - WA ENTREGA MESH",
    "FTTH - WA ENTREGA PLUME",
    "FTTH WA ENTREGA REPETIDOR",
    "FTTH WA ENTREGA REPETIDOR MESH",
    "HFC - WA ENTREGA MESH",
    "HFC - WA ENTREGA PLUME",
    "HFC WA ENTREGA REPETIDOR MESH",
]


def es_entrega_mesh(tipo):
    return _normalizar(tipo) in {_normalizar(t) for t in TIPOS_ENTREGA_MESH}


def paquete_entrega_mesh(servicios):
    """Solo para las WA ENTREGA: cuentan como MESH (según su cantidad) las líneas tipo
    'Velocidad Total 1000 Mbps - Bono', 'Full Claro 200 Mbps' (o parecidas) y los repetidores
    (por ejemplo 'Alquiler de Equipos INT-REPETIDOR...').
    -> 'INSTALACION MESH' (uno) / 'INSTALACION N MESH'. Devuelve None si no encuentra ninguno."""
    bonos = sum(_cantidad(c) for s_, c in servicios
                if any(p in _normalizar(s_) for p in ("BONO", "VELOCIDAD TOTAL", "FULL CLARO")))
    repetidores = sum(_cantidad(c) for s_, c in servicios
                      if es_equipo(s_) and ("REPETIDOR" in _normalizar(s_) or "MESH" in _normalizar(s_)))
    mesh = bonos + repetidores
    if not mesh:
        return None
    return "INSTALACION MESH" if mesh == 1 else f"INSTALACION {mesh} MESH"


# Accesorio adicional de POSTVENTA: el paquete sale contando los REPETIDORES del Detalle Servicios
# (aquí NO se cuentan las líneas 'Velocidad Total ... - Bono'; eso es solo para las WA ENTREGA)
TIPOS_ACCESORIO_POSTVENTA = ["FTTH/SIAC - ACCESORIO ADICIONAL", "HFC/SIAC - ACCESORIO ADICIONAL",
                             "HFC - PV ALTA ACCESORIO ADICIONAL"]   # igual que FTTH - PV ALTA ACCESORIO ADICIONAL


def es_accesorio_postventa(tipo):
    return _normalizar(tipo) in {_normalizar(t) for t in TIPOS_ACCESORIO_POSTVENTA}


def paquete_accesorio_postventa(servicios):
    """Cuenta los repetidores (según su cantidad) -> 'INSTALACION MESH' (uno) / 'INSTALACION N MESH' (o None),
    con los mismos nombres que FTTH - PV ALTA ACCESORIO ADICIONAL."""
    mesh = sum(_cantidad(c) for s_, c in servicios if es_equipo(s_) and "REPETIDOR" in _normalizar(s_))
    if not mesh:
        return None
    return "INSTALACION MESH" if mesh == 1 else f"INSTALACION {mesh} MESH"


# Deco adicional: el paquete sale contando los DECODIFICADORES (equipos) del Detalle Servicios.
# 'Inst. Punto Adicional TV Post Instalación' NO se cuenta (no es un equipo).
TIPOS_DECO_ADICIONAL = ["HFC Deco Adicional", "HFC/IPTV - DECO ADICIONAL", "HFC/SIAC - DECO ADICIONAL"]


def es_deco_adicional(tipo):
    return _normalizar(tipo) in {_normalizar(t) for t in TIPOS_DECO_ADICIONAL}


def paquete_deco_adicional(servicios):
    """Cuenta los decos (equipos con DECO/DECODIFICADOR, según su cantidad) -> 'INSTALACION 1 DECO' /
    'INSTALACION N DECOS' (o None si no hay)."""
    decos = sum(_cantidad(c) for s_, c in servicios if es_equipo(s_) and "DECO" in _normalizar(s_))
    if not decos:
        return None
    return "INSTALACION 1 DECO" if decos == 1 else f"INSTALACION {decos} DECOS"


def es_instalacion(tipo):
    """True si el tipo de trabajo está en la categoría INSTALACION (TIPOS_INSTALACION).
    Se compara el nombre completo: así 'FTTH - MANTENIMIENTOFTTH - SISACT INSTALACION...' (que es
    mantenimiento) no cuenta, y 'FTTH/SIAC - MIGRACION TECNOLOGICA DE HFC A FTTH' (sin la palabra) sí."""
    return _normalizar(tipo) in _TIPOS_INSTALACION


def leer_sots_witlink(page):
    """Lee de la tabla de Programación (la sede y el día que están abiertos) las SOTs con Edo. Contrata
    ATENDIDA o EN ATENCION. Devuelve (todas, instalaciones) y deja en MANTENIMIENTOS_WITLINK y FUERA_TOA_WITLINK
    las de mantenimiento y las 'Fuera de TOA'."""
    global MANTENIMIENTOS_WITLINK, FUERA_TOA_WITLINK
    page.locator("#prog-tbody").wait_for(state="attached", timeout=ESPERA_CORTA_MS)
    filas = page.evaluate("""() => [...document.querySelectorAll('#prog-tbody tr[data-sot]')].map(f => ({
        sot: (f.getAttribute('data-sot') || '').trim(),
        estado: ((f.querySelector('td.col-econt') || {}).getAttribute?.('data-val') || '').trim(),
        tipo: ((f.querySelector('td.col-tipo') || {}).innerText || '').trim(),
    }))""")
    sots, instalaciones = [], []
    MANTENIMIENTOS_WITLINK, FUERA_TOA_WITLINK = [], []
    for fila in filas:                       # una SOT puede aparecer varias veces: vale la fila ATENDIDA/EN ATENCION
        sot, tipo = fila["sot"], fila["tipo"]
        if fila["estado"].upper() in ESTADOS_BUSCADOS and sot and sot not in sots:
            sots.append(sot)
            if es_instalacion(tipo):
                instalaciones.append(sot)
            if es_mantenimiento(tipo):
                MANTENIMIENTOS_WITLINK.append(sot)
            if _normalizar(tipo) == "FUERA DE TOA":
                FUERA_TOA_WITLINK.append(sot)
    print(f"Filas en la programación: {len(filas)} | ATENDIDAS o EN ATENCION: {len(sots)}")
    print(f"   instalación: {len(instalaciones)} | mantenimiento: {len(MANTENIMIENTOS_WITLINK)} | "
          f"'Fuera de TOA' (se busca su tipo real en SGA): {len(FUERA_TOA_WITLINK)}")
    return sots, instalaciones


def copiar_al_portapapeles(sots):
    """Copia las SOTs al portapapeles de Windows (una por línea) y comprueba que quedaron copiadas."""
    texto = "\r\n".join(sots)                # una SOT por línea, con el salto de línea de Windows
    copiado = ""
    for intento in range(1, 11):             # el portapapeles puede estar ocupado un instante
        try:
            subprocess.run("clip", input=texto.encode("ascii"), check=True, shell=True)   # clip = copiar de Windows
            copiado = clipboard.GetData()    # se vuelve a leer el portapapeles para verificar
            break
        except Exception:
            if intento == 10:
                raise
            time.sleep(1.5)
    copiadas = [linea.strip() for linea in copiado.splitlines() if linea.strip()]
    if copiadas != sots:                     # lo leído debe ser exactamente lo que se copió
        raise RuntimeError(f"El portapapeles no coincide: se copiaron {len(sots)} SOTs y se leyeron {len(copiadas)}.")
    print(f"OK: {len(copiadas)} SOTs copiadas al portapapeles (puedes pegarlas con Ctrl+V).")
    for numero, sot in enumerate(copiadas, start=1):   # lista numerada para que las revises
        print(f"   {numero:>3}. {sot}")


# ---------------- PASO 2: SGA ----------------
def abrir_sga():
    """Trae al frente la ventana de SGA que ya tienes abierta (no abre otro SGA ni inicia sesión)."""
    # Solo la ventana PRINCIPAL de SGA (clase FNWND3170), no otra que tenga "SGA Operaciones" en el título
    candidatas = [w for w in Desktop(backend="win32").windows(class_name="FNWND3170", visible_only=True)
                  if w.window_text().startswith("SGA Operaciones")]
    if not candidatas:
        raise RuntimeError("No encontré la ventana de SGA. Ábrela, inicia sesión y vuelve a ejecutar.")
    ventana = Desktop(backend="win32").window(handle=candidatas[0].handle)
    # Si SGA está ocupado ("No responde"), se espera a que vuelva antes de tocarlo
    if not _esperar_sin_colgar(candidatas[0]):
        raise RuntimeError("SGA no responde desde hace varios minutos. Revísalo y vuelve a ejecutar.")

    if not ctypes.windll.user32.IsZoomed(ctypes.c_void_p(ventana.handle)):
        ventana.maximize()                   # SGA maximizado: se ve igual en cualquier monitor
    ventana.set_focus()                      # trae SGA al frente (y lo restaura si estaba minimizado)
    time.sleep(1)                            # pausa de 1 segundo para que Windows termine de cambiar la ventana

    # Windows a veces impide que otro programa robe el foco; se comprueba si SGA quedó al frente
    if ctypes.windll.user32.GetForegroundWindow() != ventana.handle:
        ventana.minimize()                   # segundo intento: minimizar...
        time.sleep(0.5)
        ventana.restore()                    # ...y restaurar, lo que suele forzar el cambio de ventana
        time.sleep(0.5)
        ventana.set_focus()                  # se vuelve a pedir el foco
        time.sleep(1)

    if ctypes.windll.user32.GetForegroundWindow() == ventana.handle:   # segunda comprobación
        print("SGA está al frente.")
    else:
        print("AVISO: SGA no quedó al frente. Haz clic en su ícono de la barra de tareas.")
    # SGA cambia su título al abrir ventanas (Control de Tareas...), así que desde aquí se sigue por su
    # identificador (handle), que no cambia; buscarlo otra vez por título fallaría
    return Desktop(backend="win32").window(handle=ventana.handle).wrapper_object()


# ---------------- PASO 3: SGA > CONTROL DE TAREAS > WITLINK > BUSCAR > "..." DE SOT ----------------
ESPERA_SGA_S = 180                           # SGA a veces tarda: hasta 3 minutos (en segundos) para cada carga
TITULO_CONTROL_TAREAS = "Control de Tareas"  # título de la ventana que se abre y texto que se busca en el menú
NODO_CONTRATISTA = "CONTRATISTA ALTA BAJA HFC"   # inicio del texto del nodo del árbol (el completo es ...&TPI-TPE)
NODO_EMPRESA = "WITLINK"                     # inicio del texto del nodo hijo (WITLINK S.A.C)
NODO_MANTENIMIENTO = "CONTRATISTA MANTO HFC"  # contratista de los mantenimientos (NO el '... MANTO HFC PEXT')
CAMPO_SOT = "codsolot"                       # nombre interno del campo SOT en la ventana Filtros
TITULO_SELECCION = "Multiple para Listado"   # parte del título de la ventana "Selección Multiple para Listado"
# Botones de la fila inferior de esa ventana, contados de izquierda a derecha (el primero es 0)
BOTON_PEGAR = 0                              # 1er botón: pega las SOTs copiadas
BOTON_BORRAR = 2                             # 3er botón (tacho): borra las SOTs que ya estaban en la lista
BOTON_CONFIRMAR = 1                          # 2do botón: confirmar
BOTON_PALOMITA = 3                           # 4to botón: palomita roja
# Posiciones fijas en píxeles "al 100%" (el bot las multiplica por la escala de la pantalla de cada PC)
ICONO_X, ICONO_Y = 56, 13                    # ícono verde de Control de Tareas, desde el área de trabajo de SGA
PRIMERA_FILA_X, PRIMERA_FILA_Y = 172, 27     # primera fila de la lista de Control de Tareas (columna SOT)
MF_BYPOSITION = 0x00000400                   # constante de Windows: el ítem del menú se identifica por su posición
WM_COMMAND = 0x0111                          # constante de Windows: mensaje que se envía cuando se elige una opción de menú


class PUNTO(ctypes.Structure):               # estructura de Windows que representa un punto (x, y)
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECTANGULO(ctypes.Structure):          # estructura de Windows que representa un rectángulo
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _api_menu():
    """Devuelve las funciones de Windows para leer menús, con los tipos de datos bien declarados."""
    u = ctypes.windll.user32                 # librería de Windows que maneja ventanas y menús
    u.GetMenu.argtypes = [ctypes.c_void_p]   # GetMenu recibe el identificador de una ventana...
    u.GetMenu.restype = ctypes.c_void_p      # ...y devuelve el identificador de su menú
    u.GetMenuItemCount.argtypes = [ctypes.c_void_p]           # cuenta las opciones de un menú
    u.GetMenuItemCount.restype = ctypes.c_int
    u.GetSubMenu.argtypes = [ctypes.c_void_p, ctypes.c_int]   # obtiene el submenú de una posición
    u.GetSubMenu.restype = ctypes.c_void_p
    # lee el texto de una opción: menú, posición, buffer donde se escribe, tamaño del buffer, modo
    u.GetMenuStringW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint]
    u.GetMenuStringW.restype = ctypes.c_int
    u.GetMenuItemID.argtypes = [ctypes.c_void_p, ctypes.c_int]   # obtiene el código interno de una opción
    u.GetMenuItemID.restype = ctypes.c_uint
    # envía un mensaje a una ventana: ventana, mensaje, dato 1, dato 2
    u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    u.PostMessageW.restype = ctypes.c_int
    return u


def _recorrer_menu(api, hmenu, ruta, encontrados):
    """Recorre el menú y sus submenús; guarda en 'encontrados' (ruta, texto, código) de cada opción."""
    total = api.GetMenuItemCount(hmenu)      # cantidad de opciones de este menú
    for posicion in range(max(total, 0)):    # max evita errores si Windows devuelve -1
        buffer = ctypes.create_unicode_buffer(256)                      # espacio donde Windows escribe el texto
        api.GetMenuStringW(hmenu, posicion, buffer, 256, MF_BYPOSITION)   # lee el texto de la opción
        # Se quita el atajo (lo que va después del tabulador), el símbolo & y los espacios
        texto = buffer.value.split("\t")[0].replace("&", "").strip()
        submenu = api.GetSubMenu(hmenu, posicion)    # si la opción abre un submenú, se obtiene aquí
        if submenu:                                  # si tiene submenú...
            _recorrer_menu(api, submenu, ruta + [texto], encontrados)   # ...se recorre también
        elif texto:                                  # si es una opción normal (los separadores no tienen texto)
            encontrados.append((" -> ".join(ruta + [texto]), texto, api.GetMenuItemID(hmenu, posicion)))


def _ejecutar_menu(ventana, texto_opcion):
    """Busca en el menú de SGA la opción 'texto_opcion' y la ejecuta. Devuelve True si envió la orden."""
    api = _api_menu()                        # funciones de Windows ya configuradas
    hmenu = api.GetMenu(ventana.handle)      # menú principal de la ventana de SGA
    if not hmenu:                            # si SGA no tiene un menú estándar, no se puede usar esta estrategia
        print("SGA no expone un menú estándar.")
        return False
    encontrados = []                         # aquí se guardan todas las opciones del menú
    _recorrer_menu(api, hmenu, [], encontrados)
    buscado = texto_opcion.lower()           # texto a buscar, en minúsculas
    exactos = [e for e in encontrados if e[1].lower() == buscado]   # opciones que se llaman exactamente así
    parecidos = [e for e in encontrados if buscado in e[1].lower()]  # opciones que contienen el texto
    elegido = (exactos or parecidos or [None])[0]   # se prefiere la exacta; si no hay, la primera parecida
    if elegido is None:                      # si no hay ninguna, se informa qué opciones existen
        print(f"No encontré '{texto_opcion}' en el menú. Opciones vistas:", [e[0] for e in encontrados][:20])
        return False
    ruta, _, codigo = elegido                # ruta legible y código interno de la opción elegida
    print("Opción de menú elegida:", ruta)
    api.PostMessageW(ventana.handle, WM_COMMAND, codigo, 0)   # equivale a elegir esa opción con el mouse
    return True


def _proceso_al_frente():
    """Devuelve el número de proceso (programa) de la ventana que está al frente."""
    proceso = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(proceso))
    return proceso.value


def _poner_al_frente(ventana, objetivo=None):
    """Trae al frente 'objetivo' (o SGA) y confirma que lo que quedó al frente es de SGA.
    Se aceptan también las ventanas sueltas de SGA, como 'Selección Multiple para Listado'."""
    for intento in range(1, 4):              # hasta 3 intentos: a veces Windows le da el foco a otro programa
        try:
            (objetivo or ventana).set_focus()
        except Exception:                    # la ventana puede estar cambiando; se reintenta
            pass
        time.sleep(1)                        # pausa para que Windows termine de cambiar la ventana
        if _proceso_al_frente() == ventana.process_id():
            return
        if intento == 2:                     # truco: minimizar y restaurar fuerza el cambio de ventana
            try:
                ventana.minimize()
                time.sleep(0.5)
                ventana.restore()
                time.sleep(0.5)
            except Exception:
                pass
    raise RuntimeError("SGA no está al frente; no se hizo clic para no pulsar otra aplicación.")


def _abrir_por_icono(ventana):
    """Estrategia B: hace clic con el mouse sobre el ícono verde de la barra de herramientas."""
    _poner_al_frente(ventana)
    f = factor_escala(ventana.handle)        # escala de esta pantalla
    punto = PUNTO(int(ICONO_X * f), int(ICONO_Y * f))   # posición del ícono dentro del área de trabajo de SGA
    ctypes.windll.user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(PUNTO)]   # tipos de la función
    ctypes.windll.user32.ClientToScreen(ventana.handle, ctypes.byref(punto))   # convierte a coordenadas de pantalla
    print(f"Clic en pantalla: x={punto.x}, y={punto.y}")   # sirve para comparar con la captura si el clic falla
    mouse.click(button="left", coords=(punto.x, punto.y))  # clic real con el mouse


def _buscar_control(ventana, clase, titulo):
    """Devuelve el primer control visible de SGA con esa clase y ese texto exacto, o None si no existe."""
    for control in ventana.descendants(class_name=clase):   # solo controles de esa clase
        try:
            if control.window_text().strip() == titulo and control.is_visible():
                return control
        except Exception:                    # si un control no se deja leer, se ignora
            continue
    return None


def _esperar_control(ventana, clase, titulo, segundos):
    """Espera hasta 'segundos' a que aparezca un control visible con esa clase y ese texto."""
    limite = time.time() + segundos          # momento en que se deja de esperar
    while time.time() < limite:              # se repite hasta agotar el tiempo
        control = _buscar_control(ventana, clase, titulo)
        if control is not None:
            return control
        time.sleep(1)                        # pausa de un segundo entre comprobaciones
    return None


def _esperar_sga_libre(ventana, segundos=ESPERA_SGA_S):
    """Espera a que SGA deje de estar ocupado (mientras carga datos no responde a Windows)."""
    SendMessageTimeoutW = ctypes.windll.user32.SendMessageTimeoutW
    SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                                    ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
    resultado = ctypes.c_size_t()
    limite = time.time() + segundos
    avisado = False
    while time.time() < limite:
        # Mensaje vacío (WM_NULL = 0): si SGA contesta en 1 segundo, ya está libre
        if SendMessageTimeoutW(ventana.handle, 0, 0, 0, 0x0002, 1000, ctypes.byref(resultado)):
            return True
        if not avisado:
            print("SGA está ocupado cargando; esperando...")
            avisado = True
        time.sleep(1)
    return False


def abrir_control_tareas(ventana):
    """Abre la ventana Control de Tareas en SGA (por menú y, si el menú no se pudo usar, con clic en el ícono)."""
    existente = _buscar_control(ventana, "FNWND3170", TITULO_CONTROL_TAREAS)
    if existente:                            # si ya estaba abierta...
        print("Control de Tareas ya estaba abierto.")
        _activar(ventana, existente.handle)  # al frente y maximizada
        return

    if _ejecutar_menu(ventana, TITULO_CONTROL_TAREAS):   # estrategia A: se envía la orden del menú
        if _esperar_control(ventana, "FNWND3170", TITULO_CONTROL_TAREAS, ESPERA_SGA_S):   # SGA puede tardar
            print("Control de Tareas abierto (por menú).")
            _activar(ventana, _buscar_control(ventana, "FNWND3170", TITULO_CONTROL_TAREAS).handle)   # maximizada
            return
        # La orden se envió pero no se detectó la ventana: NO se hace clic en el ícono para no abrirla dos veces
        raise RuntimeError(f"Se envió la orden del menú, pero no detecté Control de Tareas en {ESPERA_SGA_S} segundos.")

    print("No se pudo usar el menú; se intentará con el clic en el ícono.")
    _abrir_por_icono(ventana)                # estrategia B
    if _esperar_control(ventana, "FNWND3170", TITULO_CONTROL_TAREAS, ESPERA_SGA_S):
        print("Control de Tareas abierto (por ícono).")
        _activar(ventana, _buscar_control(ventana, "FNWND3170", TITULO_CONTROL_TAREAS).handle)   # maximizada
        return
    raise RuntimeError("No se pudo abrir Control de Tareas (ni por menú ni por el ícono).")


def _nodo_hijo(nodo, inicio):
    """Devuelve el hijo del nodo del árbol cuyo texto empieza por 'inicio', o None."""
    for hijo in nodo.children():
        if hijo.text().upper().startswith(inicio.upper()):
            return hijo
    return None


def _arbol_contratistas(ventana):
    """Devuelve el árbol de contratistas de Control de Tareas (control PBTreeView32_...) o None."""
    for candidato in ventana.descendants():
        try:
            if candidato.class_name().startswith("PBTreeView32") and candidato.is_visible():
                return TreeViewWrapper(candidato.handle)   # permite leer los nodos y hacer clic en ellos
        except Exception:                        # controles que cambian mientras SGA carga
            continue
    return None


def _esperar_nodo(obtener, descripcion):
    """Repite 'obtener()' hasta que devuelva un nodo (SGA puede tardar en mostrarlo)."""
    limite = time.time() + ESPERA_SGA_S
    avisado = False
    while time.time() < limite:
        try:
            nodo = obtener()
            if nodo is not None:
                return nodo
        except Exception:                        # el árbol se está redibujando; se vuelve a intentar
            pass
        if not avisado:
            print(f"Esperando a que SGA muestre {descripcion}...")
            avisado = True
        time.sleep(1)
    raise RuntimeError(f"SGA no mostró {descripcion} en {ESPERA_SGA_S} segundos.")


def _clic_nodo(item, doble=False):
    """Clic (o doble clic) en el CENTRO REAL de un nodo del árbol de SGA.
    SGA informa la posición del nodo sin la escala de pantalla (125%); sin corregirla, en las filas de abajo
    el clic cae una fila más arriba (por ejemplo en GARANTÍA MANTENIMIENTO en vez de MANTO HFC)."""
    arbol = item.tree_ctrl.handle
    f = factor_escala(arbol)                 # escala de esta pantalla (1.0, 1.25, 1.5...)
    origen = PUNTO(0, 0)
    ctypes.windll.user32.ClientToScreen(ctypes.c_void_p(arbol), ctypes.byref(origen))
    r = item.client_rect()                   # posición del nodo SIN escala
    x = origen.x + int((r.left + r.right) / 2 * f)
    y = origen.y + int((r.top + r.bottom) / 2 * f)
    if doble:
        mouse.double_click(button="left", coords=(x, y))
    else:
        mouse.click(button="left", coords=(x, y))


def _doble_clic_seguro(ventana, item):
    """Doble clic en un nodo del árbol SIN equivocarse de fila: al elegir un contratista SGA puede cerrar otro
    (las filas se mueven). Por eso: clic simple, esperar a SGA, confirmar que quedó elegido ESE nodo, y recién
    doble clic en su posición actual. Si queda elegido otro nodo, se reintenta."""
    for intento in range(1, 4):
        _poner_al_frente(ventana)
        try:
            item.ensure_visible()
        except Exception:
            pass
        try:
            item.select()                    # 1) se elige el nodo por el árbol (sin mouse: no puede caer en otra fila)
        except Exception:
            _clic_nodo(item)                 #    si no se puede, clic simple
        time.sleep(1)
        _esperar_sga_libre(ventana)          # SGA puede cerrar otras ramas y mover las filas
        time.sleep(1)
        try:
            item.ensure_visible()            # la posición se vuelve a leer DESPUÉS de que el árbol se reacomodó
        except Exception:
            pass
        if not item.is_selected():
            print(f"   El clic no quedó en {item.text()} (intento {intento}); se reintenta...")
            continue
        _clic_nodo(item, doble=True)         # 2) doble clic en su posición ACTUAL (ya se confirmó que es el correcto)
        time.sleep(1)
        _esperar_sga_libre(ventana)          # SGA carga las tareas; después puede dejar de marcarlo como elegido
        print("Doble clic en", item.text())
        return
    raise RuntimeError(f"No logré elegir '{item.text()}' en el árbol de SGA.")


def abrir_witlink_en_arbol(ventana, nodo=None, exacto=False):
    """Doble clic en el contratista (por defecto CONTRATISTA ALTA BAJA HFC&TPI-TPE) y luego en WITLINK S.A.C.
    exacto=True exige el nombre completo: así 'CONTRATISTA MANTO HFC' no se confunde con '... MANTO HFC PEXT'."""
    nodo = nodo or NODO_CONTRATISTA
    _esperar_sga_libre(ventana)

    def coincide(texto):
        return _normalizar(texto) == _normalizar(nodo) if exacto else _normalizar(texto).startswith(_normalizar(nodo))

    # 1) Se espera a que aparezca el nodo del contratista
    contratista = _esperar_nodo(
        lambda: next((n for n in _arbol_contratistas(ventana).roots() if coincide(n.text())), None),
        f"'{nodo}' en el árbol")

    # Si el nodo ya está desplegado NO se hace doble clic, porque eso lo cerraría
    for intento in range(1, 4):              # hasta 3 intentos
        if _nodo_hijo(contratista, NODO_EMPRESA) is not None:
            break
        _doble_clic_seguro(ventana, contratista)
        limite = time.time() + 30            # hasta 30 s a que aparezca WITLINK S.A.C debajo
        while time.time() < limite and _nodo_hijo(contratista, NODO_EMPRESA) is None:
            time.sleep(1)
        if _nodo_hijo(contratista, NODO_EMPRESA) is None:
            print(f"   No se desplegó {contratista.text()} (intento {intento}); se vuelve a intentar...")

    # 2) WITLINK S.A.C debajo del contratista
    empresa = _esperar_nodo(lambda: _nodo_hijo(contratista, NODO_EMPRESA), f"'{NODO_EMPRESA}' en el árbol")
    _doble_clic_seguro(ventana, empresa)
    time.sleep(1)
    _esperar_sga_libre(ventana)                  # espera a que SGA termine de cargar las tareas de la empresa


def abrir_buscar(ventana):
    """Abre la ventana Filtros con la opción Aplicación -> Buscar (la misma del ícono de la barra)."""
    filtros = _buscar_control(ventana, "pbdw170", "Filtros")
    if filtros is not None:                      # si ya estaba abierta, no se vuelve a abrir
        print("La ventana Filtros ya estaba abierta.")
        return filtros
    _esperar_sga_libre(ventana)
    if not _ejecutar_menu(ventana, "Buscar"):
        raise RuntimeError("No encontré la opción Buscar en el menú de SGA.")
    filtros = _esperar_control(ventana, "pbdw170", "Filtros", ESPERA_SGA_S)
    if filtros is None:
        raise RuntimeError(f"Se pulsó Buscar, pero no apareció la ventana Filtros en {ESPERA_SGA_S} segundos.")
    print("Ventana Filtros abierta.")
    return filtros


def clic_puntos_sot(ventana, filtros):
    """Hace clic en el botón '...' que está al costado del campo SOT en la ventana Filtros."""
    elementos = Desktop(backend="uia").window(handle=filtros.handle).descendants()   # campos de Filtros
    campo = next((e for e in elementos if e.element_info.name == CAMPO_SOT), None)   # campo SOT (codsolot)
    if campo is None:
        raise RuntimeError(f"No encontré el campo SOT ('{CAMPO_SOT}') en la ventana Filtros.")
    r_campo = campo.element_info.rectangle
    centro_campo = (r_campo.top + r_campo.bottom) // 2
    # El botón '...' de SOT es el que está en la misma fila que el campo y a su derecha
    # El botón dice '...' o, si ya tiene SOTs cargadas de antes, '***' (en rojo)
    botones = [e for e in elementos if e.element_info.control_type == "Button" and e.element_info.name in ("...", "***")
               and e.element_info.rectangle.top <= centro_campo <= e.element_info.rectangle.bottom
               and e.element_info.rectangle.left >= r_campo.right - 5]
    if not botones:
        raise RuntimeError("No encontré el botón '...' al costado del campo SOT.")
    r_boton = botones[0].element_info.rectangle

    # SGA informa la posición de los campos sin contar la barra de título de Filtros; se suma ese desfase
    ventana_rect = RECTANGULO()
    ctypes.windll.user32.GetWindowRect(filtros.handle, ctypes.byref(ventana_rect))   # esquina de la ventana
    origen = PUNTO(0, 0)
    ctypes.windll.user32.ClientToScreen(filtros.handle, ctypes.byref(origen))        # esquina del área interior
    x = (r_boton.left + r_boton.right) // 2 + (origen.x - ventana_rect.left)
    y = (r_boton.top + r_boton.bottom) // 2 + (origen.y - ventana_rect.top)

    _poner_al_frente(ventana)
    antes = _ventanas_visibles(ventana)          # ventanas que había antes del clic
    print(f"Clic en '...' de SOT: x={x}, y={y}")    # sirve para comparar si el clic cae en otro lugar
    mouse.click(button="left", coords=(x, y))

    # La ventana de SOTs ("Total : 0") es la que aparece después del clic
    limite = time.time() + ESPERA_SGA_S
    while time.time() < limite:
        time.sleep(0.5)
        nuevas = [v for v in _ventanas_visibles(ventana) if v.handle not in antes]
        if nuevas:
            time.sleep(1)                        # pausa para que termine de dibujarse
            nuevas = [v for v in _ventanas_visibles(ventana) if v.handle not in antes] or nuevas
            # Se prefiere la ventana titulada "Selección Multiple para Listado"
            por_titulo = [v for v in nuevas if TITULO_SELECCION.lower() in v.window_text().lower()]
            # Si no, la más grande de las nuevas (las demás son partes internas de ella)
            dialogo = por_titulo[0] if por_titulo else max(
                nuevas, key=lambda v: v.rectangle().width() * v.rectangle().height())
            print("Ventana de SOTs abierta:", repr(dialogo.window_text()), dialogo.class_name())
            return dialogo
    raise RuntimeError(f"Se hizo clic en '...', pero no apareció la ventana de SOTs en {ESPERA_SGA_S} segundos.")


# ---------------- PASO 4: PEGAR SOTs, CONFIRMAR, PALOMITA Y ENTER ----------------
def _ventanas_visibles(ventana):
    """Ventanas visibles de SGA con barra de título (las internas y las sueltas del mismo proceso)."""
    todas = [c for c in ventana.descendants() if c.class_name() in ("FNWND3170", "pbdw170")]
    todas += [v for v in Desktop(backend="win32").windows(process=ventana.process_id(), visible_only=True)
              if v.handle != ventana.handle]
    visibles = []
    for v in todas:
        try:
            if v.is_visible() and v.rectangle().width() > 0:
                visibles.append(v)
        except Exception:                        # ventanas que se cierran mientras se leen
            continue
    return visibles


def _botones_inferiores(dialogo):
    """Devuelve los botones de la fila de abajo del diálogo, ordenados de izquierda a derecha."""
    botones = []
    for c in dialogo.descendants(class_name="Button"):   # en PowerBuilder los botones con ícono son 'Button'
        try:
            if c.is_visible() and c.rectangle().width() > 0:
                botones.append(c)
        except Exception:
            continue
    if not botones:
        return []
    abajo = max(b.rectangle().top for b in botones)            # altura de la fila más baja
    fila = [b for b in botones if abs(b.rectangle().top - abajo) <= 10]   # botones de esa fila
    return sorted(fila, key=lambda b: b.rectangle().left)      # de izquierda a derecha


def _total_dialogo(dialogo):
    """Lee el número que aparece en 'Total : N' dentro del diálogo (o None si no lo encuentra)."""
    for e in Desktop(backend="uia").window(handle=dialogo.handle).descendants():
        texto = (e.element_info.name or "").replace(" ", "")
        if texto.upper().startswith("TOTAL:"):               # por ejemplo "Total : 53"
            numero = texto.split(":", 1)[1]
            return int(numero) if numero.isdigit() else None
    return None


def _mostrar_dialogo(dialogo):
    """Muestra en consola los controles del diálogo, para saber qué ocurrió cuando algo falla."""
    print("Controles de la ventana de SOTs:")
    for c in dialogo.descendants():
        try:
            if c.is_visible():
                print("   ", c.class_name(), repr(c.window_text())[:40], c.rectangle())
        except Exception:
            continue


def _confirmar_mensaje(ventana):
    """Si SGA muestra un mensaje de confirmación (ventana pequeña de Windows), pulsa Sí / Aceptar."""
    for v in Desktop(backend="win32").windows(process=ventana.process_id(), visible_only=True):
        if v.class_name() != "#32770":           # #32770 = clase de los mensajes estándar de Windows
            continue
        print("SGA mostró un mensaje:", repr(v.window_text()))
        for boton in v.children(class_name="Button"):
            if boton.window_text().replace("&", "").strip().lower() in ("sí", "si", "aceptar", "ok", "yes"):
                boton.click()                    # pulsa el botón Sí / Aceptar
                print("   Se respondió:", boton.window_text().replace("&", ""))
                time.sleep(1)
                return


def pegar_y_cargar_sots(ventana, dialogo, cantidad):
    """Botón 1 (pegar), verifica el total, botón 2 (confirmar), botón 4 (palomita) y Enter en Filtros."""
    botones = _botones_inferiores(dialogo)
    if len(botones) < 4:                         # la fila de abajo tiene 6 botones; se necesitan al menos 4
        _mostrar_dialogo(dialogo)
        raise RuntimeError(f"Esperaba 6 botones abajo en 'Selección Multiple para Listado' y encontré {len(botones)}.")
    print(f"Botones encontrados abajo: {len(botones)}")

    _poner_al_frente(ventana, dialogo)           # la ventana Selección Multiple es la que debe estar al frente
    botones[BOTON_BORRAR].click_input()          # 0) tacho: se vacía la lista para no sumar SOTs de antes
    print("Clic en el botón 3 (tacho): lista vaciada.")
    time.sleep(1)
    _confirmar_mensaje(ventana)                  # si SGA pregunta "¿Desea borrar...?", se responde que sí
    total = _total_dialogo(dialogo)
    if total not in (None, 0):
        raise RuntimeError(f"Después del tacho la lista sigue con {total} SOTs. No se continúa.")
    _poner_al_frente(ventana, dialogo)
    botones[BOTON_PEGAR].click_input()                     # 1) pegar las SOTs del portapapeles
    print("Clic en el botón 1 (pegar).")
    total = None
    inicio = time.time()
    while time.time() < inicio + 60:             # hasta 60 segundos a que el total llegue a la cantidad copiada
        time.sleep(0.5)
        total = _total_dialogo(dialogo)
        if total == cantidad:
            break
        if total is None and time.time() > inicio + 5:   # si no se puede leer el total, no se sigue esperando
            time.sleep(2)                        # pausa extra para que termine de pegar
            break
    if total is None:
        print("AVISO: no pude leer el 'Total' de la ventana; se continúa sin verificarlo.")
    elif total != cantidad:
        raise RuntimeError(f"Se pegaron {total} SOTs, pero se copiaron {cantidad}. No se continúa.")
    else:
        print(f"OK: Total : {total} SOTs pegadas.")

    botones[BOTON_CONFIRMAR].click_input()                     # 2) confirmar
    print("Clic en el botón 2 (confirmar).")
    time.sleep(1)
    botones[BOTON_PALOMITA].click_input()                     # 4) palomita roja
    print("Clic en el botón 4 (palomita).")

    # Al pulsar la palomita se vuelve a la ventana Filtros
    limite = time.time() + ESPERA_SGA_S
    while time.time() < limite and dialogo.is_visible():
        time.sleep(0.5)
    filtros = _esperar_control(ventana, "pbdw170", "Filtros", 10)
    if filtros is None:
        raise RuntimeError("Después de la palomita no volvió la ventana Filtros.")

    _poner_al_frente(ventana)                    # el Enter solo se envía si SGA está al frente
    filtros.set_focus()
    keyboard.send_keys("{ENTER}")                # 5) Enter: carga las SOTs en Control de Tareas
    print("Enter enviado en Filtros.")
    limite = time.time() + ESPERA_SGA_S          # la carga puede tardar varios minutos
    while time.time() < limite and _buscar_control(ventana, "pbdw170", "Filtros") is not None:
        time.sleep(1)
    if _buscar_control(ventana, "pbdw170", "Filtros") is None:
        print("Filtros se cerró: las SOTs se están cargando en Control de Tareas.")
    else:
        print(f"AVISO: la ventana Filtros sigue abierta después de {ESPERA_SGA_S} segundos; revisa SGA.")


# ---------------- PASO 5: CARGAR TODA LA LISTA Y CLIC EN LA PRIMERA SOT ----------------
LISTA_TAREAS = "d_lis_tareawf"               # nombre interno de la lista de SOTs de Control de Tareas
WM_VSCROLL = 0x0115                          # constante de Windows: mensaje de la barra de desplazamiento vertical
SB_TOP = 6                                   # ir al inicio de la lista
SB_BOTTOM = 7                                # ir al final de la lista


class INFO_SCROLL(ctypes.Structure):         # estructura de Windows con el estado de una barra de desplazamiento
    _fields_ = [("cbSize", ctypes.c_uint), ("fMask", ctypes.c_uint), ("nMin", ctypes.c_int), ("nMax", ctypes.c_int),
                ("nPage", ctypes.c_uint), ("nPos", ctypes.c_int), ("nTrackPos", ctypes.c_int)]


def _barra_vertical(lista):
    """Devuelve la barra de desplazamiento vertical de la lista (la más alta y angosta), o None."""
    barras = [c for c in lista.children() if c.class_name() == "ScrollBar" and c.is_visible()]
    verticales = [b for b in barras if b.rectangle().height() > b.rectangle().width()]
    return verticales[0] if verticales else None


def _estado_barra(barra):
    """Devuelve (máximo, posición) de la barra; cambia cuando SGA agrega filas o se mueve la lista."""
    info = INFO_SCROLL()
    info.cbSize = ctypes.sizeof(INFO_SCROLL)
    info.fMask = 0x17                        # SIF_ALL: pedir todos los datos
    ctypes.windll.user32.GetScrollInfo(barra.handle, 2, ctypes.byref(info))   # 2 = SB_CTL (barra como control)
    return info.nMax, info.nPos


def _desplazar(lista, barra, orden):
    """Envía a la lista la orden de ir al inicio (SB_TOP) o al final (SB_BOTTOM), como si se usara la barra."""
    SendMessageW = ctypes.windll.user32.SendMessageW
    SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    SendMessageW(lista.handle, WM_VSCROLL, orden, barra.handle if barra else 0)


def cargar_lista_y_primera_sot(ventana):
    """Baja la lista hasta el final varias veces (SGA carga filas al bajar), vuelve arriba y hace clic en la primera SOT."""
    _esperar_sin_colgar(ventana)
    lista = None
    limite = time.time() + ESPERA_SGA_S          # la lista de SOTs puede tardar en aparecer
    while time.time() < limite and lista is None:
        lista = _lista_liviana(ventana)
        if lista is None:
            time.sleep(1)
    if lista is None:
        raise RuntimeError("No encontré la lista de SOTs de Control de Tareas.")

    barra = None
    limite = time.time() + 30                    # la barra aparece cuando la lista ya tiene filas
    while time.time() < limite and barra is None:
        barra = _barra_vertical(lista)
        if barra is None:
            time.sleep(1)
    if barra is None:
        print("La lista no tiene barra vertical (caben todas las SOTs en pantalla); no hace falta bajar.")
    else:
        anterior, iguales = None, 0
        for vuelta in range(1, 101):             # como máximo 100 bajadas
            _desplazar(lista, barra, SB_BOTTOM)  # baja hasta el final
            time.sleep(1.5)
            _esperar_sin_colgar(ventana)         # espera a que SGA termine de cargar las filas nuevas
            estado = _estado_barra(barra)
            iguales = iguales + 1 if estado == anterior else 0
            if iguales >= 2:                     # 2 bajadas seguidas sin cambios: se llegó al final real
                break
            anterior = estado
        print(f"Lista cargada hasta el final ({vuelta} bajadas).")
        _desplazar(lista, barra, SB_TOP)         # vuelve arriba
        time.sleep(1)
        _esperar_sga_libre(ventana)
        print("Lista de vuelta arriba.")

    clic_primera_fila(ventana, lista)        # primera SOT (posición escalada a esta pantalla)
    print("Primera SOT seleccionada.")


# ---------------- PASO 6: CAPTURAR SOT + TIPO DE TRABAJO DE LA LISTA ----------------
def _partir_exportacion(texto):
    """Convierte el texto exportado por SGA (columnas separadas por tabulador) en una lista de filas.
    Algunas observaciones traen saltos de línea y hasta tabuladores dentro, así que:
    - cada fila empieza donde un salto de línea va seguido de un número y un tabulador (el idagenda);
    - si una fila trae columnas de más, lo sobrante se devuelve a la columna 'observacion'."""
    texto = texto.replace("\r\n", "\n")
    cabecera = texto.split("\n", 1)[0].split("\t")
    total = len(cabecera)
    obs = cabecera.index("observacion") if "observacion" in cabecera else None
    registros = re.split(r"\n(?=\d+\t)", texto)[1:]   # sin la cabecera
    filas = []
    for registro in registros:
        campos = registro.rstrip("\n").split("\t")
        sobran = len(campos) - total
        if sobran > 0 and obs is not None:   # tabuladores dentro de la observación: se vuelven a unir
            campos = campos[:obs] + ["\t".join(campos[obs:obs + sobran + 1])] + campos[obs + sobran + 1:]
        campos += [""] * (total - len(campos))   # si faltan columnas, se completan vacías
        filas.append(campos[:total])
    return cabecera, filas


def exportar_lista(ventana):
    """Copia TODA la lista de Control de Tareas con Aplicación -> Exportar -> Clipboard y la devuelve como
    [{columna: valor}, ...] en el orden de la pantalla. (Leer la lista celda por celda no es confiable:
    a veces SGA solo entrega la primera fila.)"""
    lista = _lista_liviana(ventana)
    if lista is None:
        raise RuntimeError("No encontré la lista de SOTs de Control de Tareas.")
    texto = ""
    for intento in range(1, 4):              # hasta 3 intentos de exportar
        _esperar_sin_colgar(ventana)
        _poner_al_frente(ventana)
        lista.set_focus()                    # la lista debe estar activa para que se exporte ella
        time.sleep(0.5)
        subprocess.run("clip", input=b"", check=True, shell=True)   # se vacía el portapapeles
        if not _ejecutar_menu(ventana, "Clipboard"):
            raise RuntimeError("No encontré la opción Aplicación -> Exportar -> Clipboard en SGA.")
        limite = time.time() + 20
        while time.time() < limite and "codsolot" not in texto:
            time.sleep(1)
            _esperar_sin_colgar(ventana)
            try:
                texto = clipboard.GetData() or ""
            except Exception:                # portapapeles ocupado un instante
                texto = ""
        if "codsolot" in texto:
            break
        print(f"   SGA no copió la lista (intento {intento}); se vuelve a intentar...")
    if "codsolot" not in texto:
        # Sin datos después de 3 intentos: la búsqueda no trajo SOTs en este contratista (lista vacía)
        print("AVISO: la lista de Control de Tareas está VACÍA (ninguna SOT encontrada en este contratista).")
        return []
    cabecera, filas = _partir_exportacion(texto)
    for columna in ("codsolot", "tipotrabajo", "cid"):
        if columna not in cabecera:
            raise RuntimeError(f"La lista exportada no tiene la columna '{columna}'.")
    posicion = {c: cabecera.index(c) for c in cabecera}   # si un nombre se repite, vale el último
    tabla = [{c: fila[i] if i < len(fila) else "" for c, i in posicion.items()} for fila in filas]
    print(f"{len(tabla)} filas copiadas de Control de Tareas.")
    return tabla


def leer_sots_y_tipo_trabajo(ventana, sots_buscadas):
    """Lee de la lista de Control de Tareas cada SOT con su Tipo de trabajo y muestra solo las buscadas
    (las ATENDIDAS / EN ATENCION de la sede).
    Devuelve ([(sot, tipo), ...], {cid: (sot, tipo)}, total_de_filas); el CID sirve para reconocer
    cada fila en el paso 7 y el total dice cuántas filas hay que recorrer."""
    print("Leyendo la lista de Control de Tareas (Aplicación -> Exportar -> Clipboard)...")
    tabla = exportar_lista(ventana)          # todas las filas, en el orden de la pantalla
    sots = [f["codsolot"].strip() for f in tabla]
    tipos = [f["tipotrabajo"].strip() for f in tabla]
    cids = [f["cid"].strip() for f in tabla]
    filas = list(zip(sots, tipos))           # (SOT, Tipo de trabajo) de cada fila
    tipo_de = dict(filas)                    # SOT -> Tipo de trabajo
    por_cid = {}                             # CID -> (SOT, Tipo de trabajo)
    if len(cids) == len(sots):
        por_cid = {cid: fila for cid, fila in zip(cids, filas) if cid}
        repetidos = len([c for c in cids if c]) - len(por_cid)
        if repetidos:
            print(f"AVISO: {repetidos} filas comparten CID con otra; en esas no puedo distinguir la SOT.")
    resultado = [(sot, tipo_de[sot]) for sot in sots_buscadas if sot in tipo_de]
    faltan = [sot for sot in sots_buscadas if sot not in tipo_de]
    if sots_buscadas:
        print(f"SOTs de {nombre_sede()} con su Tipo de trabajo en SGA: {len(resultado)} de {len(sots_buscadas)}")
    for numero, (sot, tipo) in enumerate(resultado, start=1):
        print(f"   {numero:>3}. {sot}  |  {tipo}")
    if faltan:
        print(f"AVISO: {len(faltan)} SOTs no aparecen en Control de Tareas:", ", ".join(faltan))
    return resultado, por_cid, len(sots)


# ---------------- PASO 7: SERVICIO Y CANTIDAD DE CADA SOT DE INSTALACION ----------------
PESTANA_DETALLE = "Detalle Servicios"        # pestaña de Control de Tareas donde está la tabla de servicios
# Posición de la pestaña 'Detalle Servicios' medida desde la esquina superior izquierda de la barra de pestañas
# (solo se usa si la pestaña no está abierta; en píxeles con la pantalla al 125%)
PESTANA_DETALLE_X, PESTANA_DETALLE_Y = 118, 14   # pestaña 'Detalle Servicios' (al 100%, desde la izquierda)

# Lectura "liviana" de ventanas: solo se consulta a Windows, sin enviarle mensajes a SGA (así no se cuelga)
_ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _texto_liviano(h):
    """Título de una ventana leído directamente de Windows."""
    b = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.InternalGetWindowText(ctypes.c_void_p(h), b, 256)
    return b.value


def _clase_liviana(h):
    """Clase (tipo) de una ventana."""
    b = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(ctypes.c_void_p(h), b, 256)
    return b.value


def _hijos_livianos(h):
    """Todos los controles dentro de la ventana h (sus identificadores), sin molestar a SGA."""
    hijos = []
    ctypes.windll.user32.EnumChildWindows(ctypes.c_void_p(h), _ENUM(lambda x, _: hijos.append(x) or True), None)
    return hijos


def _padre(h):
    ctypes.windll.user32.GetParent.restype = ctypes.c_void_p
    return ctypes.windll.user32.GetParent(ctypes.c_void_p(h))


def _visible(h):
    return bool(ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(h)))


def _tamano(h):
    r = RECTANGULO()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))
    return (r.right - r.left) * (r.bottom - r.top)


def _lista_liviana(ventana):
    """La lista de SOTs (d_lis_tareawf) buscada sin enviarle mensajes a SGA; devuelve su control o None."""
    for h in _hijos_livianos(ventana.handle):
        if _clase_liviana(h) == "pbdw170" and _texto_liviano(h) == LISTA_TAREAS and _visible(h):
            return Desktop(backend="win32").window(handle=h).wrapper_object()
    return None


def _pagina_detalle(ventana):
    """Identificador de la página 'Detalle Servicios' (esté visible o no), o None."""
    for h in _hijos_livianos(ventana.handle):
        if _clase_liviana(h) == "FNUDO3170" and _texto_liviano(h) == PESTANA_DETALLE:
            return h
    return None


def _tiene_columna_servicio(tabla):
    """True si la tabla tiene la columna 'servicio' (así se distingue de la tabla 'Ficha Tecnica')."""
    try:
        return any(e.element_info.name == "servicio_t"
                   for e in Desktop(backend="uia").window(handle=tabla).descendants(control_type="Text"))
    except Exception:
        return False


def _tabla_detalle(pagina, segundos=30):
    """La tabla de servicios de la página Detalle Servicios: la que tiene la columna 'servicio'.
    Recién abierta la pestaña las tablas todavía se están armando, por eso se reintenta unos segundos."""
    limite = time.time() + segundos
    while True:
        tablas = [h for h in _hijos_livianos(pagina) if _clase_liviana(h) == "pbdw170" and _padre(h) == pagina]
        # primero las que no tienen título (la de servicios no tiene; la otra se llama 'Ficha Tecnica')
        tablas.sort(key=lambda h: (_texto_liviano(h) != "", -_tamano(h)))
        for h in tablas:
            if _tiene_columna_servicio(h):
                return h
        if time.time() > limite:
            return None
        time.sleep(1)


def _abrir_pestana_detalle(ventana):
    """Deja abierta la pestaña 'Detalle Servicios' (si ya lo está no hace nada)."""
    pagina = _pagina_detalle(ventana)
    if pagina is None:
        raise RuntimeError("No encontré la pestaña 'Detalle Servicios' en Control de Tareas.")
    if _visible(pagina):
        print(f"La pestaña '{PESTANA_DETALLE}' ya está abierta.")
        return pagina
    return clic_pestana(ventana, pagina, PESTANA_DETALLE_X, PESTANA_DETALLE_Y, nombre=PESTANA_DETALLE)


def _esperar_sin_colgar(ventana, segundos=ESPERA_SGA_S):
    """Espera a que SGA vuelva a responder, preguntándole solo a Windows (no le envía mensajes)."""
    limite = time.time() + segundos
    avisado = False
    while time.time() < limite:
        if not ctypes.windll.user32.IsHungAppWindow(ctypes.c_void_p(ventana.handle)):
            return True
        if not avisado:
            print("SGA está ocupado; esperando...")
            avisado = True
        time.sleep(1)
    return False


def _leer_detalle(tabla):
    """Lee la tabla de Detalle Servicios: devuelve (CIDs, [(servicio, cantidad), ...]).
    Una SOT con internet + TV + teléfono trae un CID distinto por servicio, por eso se devuelven todos."""
    columnas = {}
    for celda in Desktop(backend="uia").window(handle=tabla).descendants(control_type="Edit"):
        try:
            valor = (celda.iface_value.CurrentValue or "").strip()
        except Exception:                    # celda que no se deja leer
            valor = ""
        columnas.setdefault(celda.element_info.name, []).append(valor)
    cids = frozenset(c for c in columnas.get("cid", []) if c)
    return cids, list(zip(columnas.get("servicio", []), columnas.get("cantidad", [])))


# ---------------- REGLA DEL PAQUETE DE INSTALACIÓN ----------------
# Palabras que indican que un servicio es un EQUIPO (router, repetidor, deco...), no un plan
PALABRAS_EQUIPO = ["ALQUILER DE EQUIPOS", "COMODATO", "ROUTER", "REPETIDOR", "ONT", "DECO", "MESH"]


def es_equipo(servicio):
    """True si el servicio es un equipo (router, repetidor, deco...)."""
    texto = servicio.upper()
    # \b = palabra completa, así "ONT" no se confunde con "MONTO"
    return any(re.search(r"\b" + re.escape(palabra) + r"\b", texto) for palabra in PALABRAS_EQUIPO)


def _cantidad(texto):
    """Convierte la cantidad de SGA ('1', '2.00'...) en número entero; si está vacía cuenta 1."""
    try:
        return int(float(texto))
    except (TypeError, ValueError):
        return 1


def calcular_paquete(servicios, tipo=""):
    """Calcula el paquete a partir de [(servicio, cantidad), ...] del Detalle Servicios:
    - Internet siempre es 1 PLAY MB; un servicio Claro TV suma 1 PLAY; Telefonía suma 1 PLAY y agrega '+ TLF'.
    - Los equipos DECODIFICADOR / Deco dan '+ 1 DECO' o '+ N DECOS' (según su cantidad).
    - Los equipos REPETIDOR dan '+ MESH' (uno) o '+ N MESH' (varios).
    - ACCESORIO ADICIONAL (por su tipo de trabajo), o sin ningún plan y solo repetidores: se instala
      solo el mesh -> 'INSTALACION MESH' (uno) o 'INSTALACION N MESH' (varios), aunque aparezca el internet.
    Ejemplo: '3 PLAY MB + TLF + 2 DECOS + MESH'."""
    planes = [s.upper() for s, _ in servicios if not es_equipo(s)]
    equipos = [(s.upper(), _cantidad(c)) for s, c in servicios if es_equipo(s)]
    tiene_tv = any("CLARO TV" in s for s in planes)
    tiene_tlf = any("TELEFONIA" in s for s in planes)
    decos = sum(c for s, c in equipos if "DECO" in s)
    mesh = sum(c for s, c in equipos if "REPETIDOR" in s or "MESH" in s)

    es_accesorio = "ACCESORIO ADICIONAL" in _normalizar(tipo)
    if mesh and not decos and (es_accesorio or not planes):   # solo se instala el mesh, sin PLAY
        return "INSTALACION MESH" if mesh == 1 else f"INSTALACION {mesh} MESH"

    paquete = f"{1 + tiene_tv + tiene_tlf} PLAY MB"
    if tiene_tlf:
        paquete += " + TLF"
    if decos:
        paquete += " + 1 DECO" if decos == 1 else f" + {decos} DECOS"
    if mesh:
        paquete += " + MESH" if mesh == 1 else f" + {mesh} MESH"
    return paquete


# ---------------- POSTVENTA: CONSULTA HISTÓRICA Y REGLA (cambios de plan, servicios menores) ----------------
# Tipos de trabajo de postventa que se comparan con la SOT anterior
TIPOS_POSTVENTA = [
    "FTTH Cambio de plan",
    "FTTH/SIAC - CAMBIO DE PLAN",
    "HFC Cambio de Plan",
    "HFC/SIAC - CAMBIO DE PLAN",
    "CLARO EMPRESAS HFC - SERVICIOS MENORES",   # también se compara con la SOT anterior
]


_TIPOS_POSTVENTA = {_normalizar(t) for t in TIPOS_POSTVENTA}


TITULO_CONSULTA = "Consulta Historica"       # inicio del título de la ventana Consulta Histórica


MENU_CONSULTA = "Consulta Historica de Clientes"   # opción del menú Reportes


# Pestañas de Consulta Histórica (fila de abajo). Se miden desde la barra de pestañas (píxeles, pantalla al 125%):
PESTANA_BUSCAR = (40, 28)                    # 'Buscar': al 100%, desde la esquina IZQUIERDA


PESTANA_SOLICITUDES_DESDE_DERECHA = (92, 28)   # 'Solicitudes de OT': al 100%, desde la esquina DERECHA (es la última)


WM_MDIACTIVATE = 0x0222
WM_MDIMAXIMIZE = 0x0225                      # maximiza una ventana interna de SGA


# La SOT ANTERIOR solo puede ser de estos tipos (se busca la palabra en el tipo de trabajo)
PALABRAS_ANTERIOR = ["INSTALACION", "MIGRACION", "CAMBIO DE PLAN", "PORTABILIDAD"]


def sirve_como_anterior(tipo):
    """True si el tipo de trabajo es una instalación, una migración, un cambio de plan o una portabilidad.
    NO sirven traslados, accesorios, mantenimientos, etc."""
    texto = _normalizar(tipo)
    # 'FTTH - MANTENIMIENTOFTTH - SISACT INSTALACION...' es mantenimiento: no sirve
    if "MANTENIMIENTO" in texto:
        return False
    return any(palabra in texto for palabra in PALABRAS_ANTERIOR)


def es_postventa(tipo):
    """True si el tipo de trabajo es uno de los 10 de postventa que se comparan con la SOT anterior."""
    return _normalizar(tipo) in _TIPOS_POSTVENTA


# ---------------- VENTANAS DE SGA ----------------
def _mdi(sga):
    """Área interna de SGA donde viven sus ventanas (Control de Tareas, Consulta Histórica...)."""
    return next(h for h in _hijos_livianos(sga.handle) if _clase_liviana(h) == "MDIClient")


def _ventana_interna(sga, inicio_titulo):
    """Identificador de la ventana interna de SGA cuyo título empieza así (o None)."""
    for h in _hijos_livianos(sga.handle):
        if _clase_liviana(h) == "FNWND3170" and _texto_liviano(h).startswith(inicio_titulo):
            return h
    return None


def _activar(sga, h):
    """Pone al frente una ventana interna de SGA (lo mismo que elegirla en el menú Aplicación) y la MAXIMIZA,
    para que se vea igual en cualquier tamaño de monitor."""
    ctypes.windll.user32.SendMessageW(_mdi(sga), WM_MDIACTIVATE, h, 0)
    if not ctypes.windll.user32.IsZoomed(ctypes.c_void_p(h)):
        ctypes.windll.user32.SendMessageW(_mdi(sga), WM_MDIMAXIMIZE, h, 0)
    time.sleep(1)
    _esperar_sin_colgar(sga)


def _hijo(h_padre, clase, titulo):
    """Primer control dentro de h_padre con esa clase y ese título exacto (o None)."""
    for h in _hijos_livianos(h_padre):
        if _clase_liviana(h) == clase and _texto_liviano(h) == titulo:
            return h
    return None


def _rect(h):
    r = RECTANGULO()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))
    return r


def _desfase(h):
    """Diferencia entre la esquina de la ventana y su área interior (PowerBuilder informa sin contarla)."""
    r = _rect(h)
    origen = PUNTO(0, 0)
    ctypes.windll.user32.ClientToScreen(ctypes.c_void_p(h), ctypes.byref(origen))
    return origen.x - r.left, origen.y - r.top


def _clic_en(sga, x, y, doble=False):
    _poner_al_frente(sga)
    mouse.click(button="left", coords=(x, y))
    if doble:
        mouse.double_click(button="left", coords=(x, y))


def _abrir_pestana(sga, consulta, nombre, desde_izquierda=None, desde_derecha=None):
    """Hace clic en una pestaña de Consulta Histórica y confirma que su página quedó visible."""
    pagina = _hijo(consulta, "FNUDO3170", nombre)
    if pagina is None:
        raise RuntimeError(f"No encontré la pestaña '{nombre}' en Consulta Histórica.")
    if desde_izquierda:
        return clic_pestana(sga, pagina, desde_izquierda[0], desde_izquierda[1], nombre=nombre)
    return clic_pestana(sga, pagina, desde_derecha[0], desde_derecha[1], desde_derecha=True, nombre=nombre)


# ---------------- CONSULTA HISTÓRICA ----------------
def abrir_consulta(sga):
    """Deja al frente Consulta Histórica en la pestaña Buscar.
    La primera vez: Aplicación -> SGA Operaciones -> Reportes -> Consulta Historica de Clientes.
    Las siguientes: Aplicación -> Consulta Historica (ya abierta) -> pestaña Buscar."""
    consulta = _ventana_interna(sga, TITULO_CONSULTA)
    if consulta is None:
        principal = _ventana_interna(sga, "SGA Operaciones")
        if principal is None:
            raise RuntimeError("No encontré la ventana 'SGA Operaciones' dentro de SGA.")
        _activar(sga, principal)             # 1 SGA Operaciones
        if not _ejecutar_menu(sga, MENU_CONSULTA):   # Reportes -> Consulta Historica de Clientes
            raise RuntimeError("No encontré 'Consulta Historica de Clientes' en el menú Reportes.")
        limite = time.time() + ESPERA_SGA_S
        while time.time() < limite and consulta is None:
            time.sleep(1)
            consulta = _ventana_interna(sga, TITULO_CONSULTA)
        if consulta is None:
            raise RuntimeError("No se abrió Consulta Histórica.")
        print("Consulta Histórica abierta.")
    _activar(sga, consulta)
    _esperar_sin_colgar(sga)
    _abrir_pestana(sga, consulta, "Buscar", desde_izquierda=PESTANA_BUSCAR)
    return consulta


def _formulario_buscar(consulta):
    """La tabla del formulario de búsqueda (la que tiene el campo 'solicitud')."""
    pagina = _hijo(consulta, "FNUDO3170", "Buscar")
    for h in _hijos_livianos(pagina):
        if _clase_liviana(h) == "pbdw170" and _padre(h) == pagina:
            nombres = {e.element_info.name for e in Desktop(backend="uia").window(handle=h).descendants(control_type="Edit")}
            if "solicitud" in nombres:
                return h
    raise RuntimeError("No encontré el campo 'Solicitud OT' en la pestaña Buscar.")


def buscar_por_solicitud(sga, consulta, sot):
    """Marca 'Solicitud OT', escribe la SOT y da Enter."""
    formulario = _formulario_buscar(consulta)
    elementos = Desktop(backend="uia").window(handle=formulario).descendants()
    casilla = next(e for e in elementos if e.element_info.control_type == "CheckBox"
                   and e.element_info.name == "Solicitud OT" and e.element_info.rectangle.width() > 0)
    campo = next(e for e in elementos if e.element_info.control_type == "Edit" and e.element_info.name == "solicitud"
                 and e.element_info.rectangle.width() > 0)
    dx, dy = _desfase(formulario)

    marcada = bool(casilla.legacy_properties().get("State", 0) & 0x10)   # 0x10 = marcada
    if not marcada:
        r = casilla.element_info.rectangle
        _clic_en(sga, r.left + 8 + dx, (r.top + r.bottom) // 2 + dy)   # clic en el cuadrito
        time.sleep(0.5)
    r = campo.element_info.rectangle
    _clic_en(sga, (r.left + r.right) // 2 + dx, (r.top + r.bottom) // 2 + dy)
    time.sleep(0.3)
    keyboard.send_keys("{HOME}+{END}{DELETE}")   # borra la SOT anterior
    keyboard.send_keys(sot)
    keyboard.send_keys("{ENTER}")
    print(f"Buscando la SOT {sot} en Consulta Histórica...")
    time.sleep(2)
    _esperar_sin_colgar(sga)


def _tabla_solicitudes(consulta):
    """La tabla de arriba de 'Solicitudes de OT' (la que tiene la columna codsolot)."""
    pagina = _hijo(consulta, "FNUDO3170", "Solicitudes de OT")
    for h in _hijos_livianos(pagina):
        if _clase_liviana(h) == "pbdw170" and _padre(h) == pagina:
            return h
    return None


def _tabla_detalle_solicitud(consulta):
    """La tabla de 'Detalle Solicitud' que está DENTRO de 'Solicitudes de OT'.
    (Consulta Histórica tiene otra página con el mismo nombre en otra pestaña, siempre vacía.)"""
    solicitudes = _hijo(consulta, "FNUDO3170", "Solicitudes de OT")
    pagina = _hijo(solicitudes, "FNUDO3170", "Detalle Solicitud")
    for h in _hijos_livianos(pagina):
        if _clase_liviana(h) == "pbdw170" and _padre(h) == pagina:
            return h
    return None


def _con_reintentos(funcion, *argumentos, veces=5):
    """Repite una lectura si SGA responde con error (pasa cuando está ocupado cargando)."""
    for intento in range(1, veces + 1):
        try:
            return funcion(*argumentos)
        except Exception:
            if intento == veces:
                raise
            time.sleep(2)


def leer_solicitudes(tabla):
    return _con_reintentos(_leer_solicitudes, tabla)


def leer_detalle_solicitud(tabla):
    return _con_reintentos(_leer_detalle_solicitud, tabla)


def _leer_solicitudes(tabla):
    """Todas las solicitudes del cliente (de la más nueva a la más antigua) y cuál está en pantalla.
    Devuelve ([{codsolot, customer, estado, tipo, fecha, observacion}], índice_en_pantalla)."""
    columnas = {}
    posiciones = []
    for e in Desktop(backend="uia").window(handle=tabla).descendants(control_type="Edit"):
        nombre = e.element_info.name
        try:
            valor = (e.iface_value.CurrentValue or "").strip()
        except Exception:
            valor = ""
        columnas.setdefault(nombre, []).append(valor)
        if nombre == "codsolot":
            posiciones.append(e.element_info.rectangle.width() > 0)   # solo la que está en pantalla tiene tamaño
    filas = []
    for i, sot in enumerate(columnas.get("codsolot", [])):
        def col(n):
            valores = columnas.get(n, [])
            return valores[i] if i < len(valores) else ""
        filas.append({"sot": sot, "customer": col("solot_customer_id"), "estado": col("estsol_descripcion"),
                      "tipo": col("tiptrabajo_descripcion"), "fecha": col("fecapr"),
                      "observacion": col("observacion"), "motivo": col("motot_descripcion")})
    actual = next((i for i, visible in enumerate(posiciones) if visible), None)
    return filas, actual


def _leer_detalle_solicitud(tabla):
    """[(servicio, cantidad), ...] y los pid (identifican el detalle que está en pantalla)."""
    columnas = {}
    for e in Desktop(backend="uia").window(handle=tabla).descendants(control_type="Edit"):
        try:
            valor = (e.iface_value.CurrentValue or "").strip()
        except Exception:
            valor = ""
        columnas.setdefault(e.element_info.name, []).append(valor)
    servicios = list(zip(columnas.get("servicio", []), columnas.get("cantidad", [])))
    return servicios, tuple(columnas.get("pid", []))


def buscar_anterior(filas, sot):
    """La solicitud ANTERIOR más reciente: mismo Customer ID, Atendida, y de instalación, migración
    o cambio de plan (cualquier tipo de trabajo que diga CAMBIO DE PLAN)."""
    i = next((k for k, f in enumerate(filas) if f["sot"] == sot), None)
    if i is None:
        return None, None
    for k in range(i + 1, len(filas)):       # hacia abajo = más antiguas
        f = filas[k]
        if (f["customer"] == filas[i]["customer"] and f["estado"].strip().upper() == "ATENDIDA"
                and sirve_como_anterior(f["tipo"])):
            return i, k
    return i, None


def ir_a_solicitud(sga, tabla, destino):
    """Mueve 'Solicitudes de OT' hasta la solicitud número 'destino' con la rueda del mouse
    y hace clic en un espacio en blanco para que cargue su Detalle Solicitud."""
    r = _rect(tabla)
    x, y = r.right - 150, (r.top + r.bottom) // 2   # zona en blanco a la derecha de la tabla
    _poner_al_frente(sga)
    mouse.move(coords=(x, y))
    for _ in range(400):                     # como máximo 400 movimientos de rueda
        _, actual = leer_solicitudes(tabla)
        if actual == destino:
            break
        # Si falta mucho se mueve la rueda varias muescas de golpe (hasta 10); cerca del destino, de una en una
        falta = abs(destino - actual) if actual is not None else 1
        muescas = max(1, min(10, falta // 2))
        paso = -muescas if actual is None or actual < destino else muescas   # hacia abajo = más antiguas
        mouse.scroll(coords=(x, y), wheel_dist=paso)
        time.sleep(0.6)
        _esperar_sin_colgar(sga)
    _, actual = leer_solicitudes(tabla)
    if actual != destino:
        raise RuntimeError(f"No pude llegar a la solicitud {destino + 1} (quedé en la {None if actual is None else actual + 1}).")
    _clic_en(sga, x, y)                      # clic en blanco: carga el Detalle Solicitud
    time.sleep(1)
    _esperar_sin_colgar(sga)


def comparar_con_anterior(sga, sot):
    """Busca la SOT en Consulta Histórica y devuelve (datos_actual, datos_anterior) con su detalle."""
    consulta = abrir_consulta(sga)
    buscar_por_solicitud(sga, consulta, sot)
    consulta = _ventana_interna(sga, TITULO_CONSULTA)   # el título cambia al nombre del cliente
    _abrir_pestana(sga, consulta, "Solicitudes de OT", desde_derecha=PESTANA_SOLICITUDES_DESDE_DERECHA)
    tabla = _tabla_solicitudes(consulta)
    detalle = _tabla_detalle_solicitud(consulta)

    # Se espera a que la lista de solicitudes tenga la SOT buscada (SGA puede tardar)
    limite = time.time() + ESPERA_SGA_S
    filas, _ = leer_solicitudes(tabla)
    while time.time() < limite and not any(f["sot"] == sot for f in filas):
        time.sleep(2)
        _esperar_sin_colgar(sga)
        filas, _ = leer_solicitudes(tabla)
    i, k = buscar_anterior(filas, sot)
    if i is None:
        raise RuntimeError(f"La SOT {sot} no aparece en Solicitudes de OT.")
    actual = dict(filas[i])
    if k is None:
        print(f"   {sot}: no hay una solicitud anterior Atendida (instalación, migración o cambio de plan) del mismo cliente.")
        return actual, None
    anterior = dict(filas[k])
    print(f"   {sot}: SOT anterior = {anterior['sot']} ({anterior['tipo']}, {anterior['fecha']})")

    _, pid_antes = leer_detalle_solicitud(detalle)
    ir_a_solicitud(sga, tabla, k)
    # SGA primero VACÍA el Detalle Solicitud y luego lo llena: se espera a que tenga servicios nuevos
    # y a que dos lecturas seguidas sean iguales (ya terminó de cargar)
    limite = time.time() + ESPERA_SGA_S
    servicios, pid = leer_detalle_solicitud(detalle)
    anterior_lectura = None
    while time.time() < limite:
        listo = pid and pid != pid_antes and (servicios, pid) == anterior_lectura
        if listo:
            break
        anterior_lectura = (servicios, pid)
        time.sleep(1.5)
        _esperar_sin_colgar(sga)
        servicios, pid = leer_detalle_solicitud(detalle)
    if not pid or pid == pid_antes:
        print(f"   AVISO: el Detalle Solicitud de {anterior['sot']} no cargó; queda vacío.")
        servicios = []
    anterior["servicios"] = [{"servicio": s, "cantidad": c} for s, c in servicios]
    return actual, anterior


# ---------------- REGLA DE POSTVENTA (aprendida con tus respuestas) ----------------
def _contar_equipos(servicios, patron):
    """Suma la cantidad de los equipos cuyo nombre tiene el patrón (DECO, REPETIDOR...)."""
    return sum(_cantidad(s["cantidad"]) for s in servicios
               if es_equipo(s["servicio"]) and re.search(patron, s["servicio"].upper()))


PATRON_MESH = r"INT-REPETIDOR|MESH"          # repetidores que cuentan como MESH


def _tiene_telefonia(servicios):
    return any("TELEFONIA" in _normalizar(s["servicio"]) for s in servicios if not es_equipo(s["servicio"]))


def sugerir_paquete(anterior, actual):
    """Compara los equipos de la SOT anterior con los de la actual:
    - decos de más  -> 'INSTALACION N DECO(S)';  decos de menos -> 'RETIRO N DECO(S)'
    - repetidores alquilados (INT-REPETIDOR) de más -> '+ MESH' (o 'INSTALACION N MESH' si es lo único);
      de menos -> 'RETIRO N MESH'
    - telefonía nueva -> 'INSTALACION TLF'; quitar telefonía -> 'RETIRO TLF' solo si no cambió nada más
    - si se retiran decos Y mesh a la vez -> 'BAJA PARCIAL'
    Devuelve None si no hay diferencias (no sabe qué poner)."""
    if not anterior or not anterior.get("servicios"):
        return None
    a, c = anterior["servicios"], actual.get("servicios", [])
    decos = _contar_equipos(c, "DECO") - _contar_equipos(a, "DECO")
    # Solo cuenta como MESH el repetidor ALQUILADO (INT-REPETIDOR); el 'Comodato-REPETIDOR ZXHN...' no es mesh
    mesh = _contar_equipos(c, PATRON_MESH) - _contar_equipos(a, PATRON_MESH)
    tlf = int(_tiene_telefonia(c)) - int(_tiene_telefonia(a))

    if decos < 0 and mesh < 0:
        return "BAJA PARCIAL"
    instala, retira = [], []
    if decos > 0:
        instala.append("1 DECO" if decos == 1 else f"{decos} DECOS")
    if mesh > 0:
        if instala:                          # junto con decos: 'INSTALACION 1 DECO + MESH'
            instala.append("MESH" if mesh == 1 else f"{mesh} MESH")
        else:                                # solo mesh: 'INSTALACION MESH' (uno) / 'INSTALACION N MESH'
            instala.append("MESH" if mesh == 1 else f"{mesh} MESH")
    if tlf > 0:
        instala.append("TLF")
    if decos < 0:
        retira.append("1 DECO" if decos == -1 else f"{-decos} DECOS")
    if mesh < 0:
        retira.append(f"{-mesh} MESH")
    # Si hay un RETIRO (de decos o de mesh), PREVALECE el retiro aunque también se haya instalado algo
    # (por ejemplo 'INSTALACION TLF / RETIRO 1 MESH' no existe en Witlink -> 'RETIRO 1 MESH')
    if retira:
        return "RETIRO " + " + ".join(retira)
    if instala:
        return "INSTALACION " + " + ".join(instala)
    if tlf < 0:                              # solo se quitó la telefonía (sin cambios de decos ni mesh)
        return "RETIRO TLF"
    return None


def _equipos_en(texto):
    """De un trozo como ' 2 ENRUTADORES, 1 DECO' saca {'MESH': 2, 'DECO': 1, 'TLF': 0}.
    ENRUTADOR / REPETIDOR / MESH cuentan como MESH. Sin número delante se cuenta 1."""
    cuenta = {"DECO": 0, "MESH": 0, "TLF": 0}
    for numero, equipo in re.findall(r"(\d+)?\s*(ENRUTADOR\w*|REPETIDOR\w*|MESH|DECO\w*|TELEFONIA|TLF)", texto):
        clave = "DECO" if equipo.startswith("DECO") else "TLF" if equipo in ("TELEFONIA", "TLF") else "MESH"
        cuenta[clave] += int(numero) if numero else 1
    return cuenta


def _lista_equipos(cuenta, uno_mesh_sin_numero):
    """{'DECO': 2, 'MESH': 1, 'TLF': 1} -> ['2 DECOS', '1 MESH', 'TLF'] (en instalación, un solo mesh es 'MESH')."""
    partes = []
    if cuenta["DECO"]:
        partes.append("1 DECO" if cuenta["DECO"] == 1 else f"{cuenta['DECO']} DECOS")
    if cuenta["MESH"]:
        partes.append("MESH" if cuenta["MESH"] == 1 and uno_mesh_sin_numero else f"{cuenta['MESH']} MESH")
    if cuenta["TLF"]:
        partes.append("TLF")
    return partes


def paquete_por_observacion(observacion):
    """Cuando la SOT anterior es IGUAL a la actual, el paquete sale de la observación de la SOT actual,
    tal cual lo que dice que se retira o se instala:
      'RETIRAR: 2 ENRUTADORES' -> 'RETIRO 2 MESH'      'RETIRAR: 2 DECO_IP' -> 'RETIRO 2 DECOS'
      'RETIRO: TELEFONIA'      -> 'RETIRO TLF'         'INSTALAR: TELEFONIA' -> 'INSTALACION TLF'
    Si dice RETIRAR, el retiro prevalece. Devuelve None si no dice RETIRAR/RETIRO ni INSTALAR/INSTALACION."""
    texto = _normalizar(observacion or "")
    trozos = {"RETIRO": "", "INSTALACION": ""}
    clave = r"(?:RETIRAR|RETIRO|INSTALAR|INSTALACION)\s*:"
    for palabra, contenido in re.findall(r"\b(RETIRAR|RETIRO|INSTALAR|INSTALACION)\s*:(.*?)(?=\b" + clave + "|$)", texto):
        trozos["RETIRO" if palabra.startswith("RETIR") else "INSTALACION"] += " " + contenido
    retira = _lista_equipos(_equipos_en(trozos["RETIRO"]), uno_mesh_sin_numero=False)
    if retira:                               # el RETIRO siempre prevalece
        return "RETIRO " + " + ".join(retira)
    instala = _lista_equipos(_equipos_en(trozos["INSTALACION"]), uno_mesh_sin_numero=True)
    return "INSTALACION " + " + ".join(instala) if instala else None


# ---------------- PASO 7: INSTALACIONES + POSTVENTA EN UN SOLO RECORRIDO ----------------
ARCHIVO_PROGRESO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "progreso_paquetes.json")


def _guardar_progreso(fecha, resultados):
    """Guarda lo calculado hasta ahora (si SGA se cuelga, no se pierde lo avanzado)."""
    with open(ARCHIVO_PROGRESO, "w", encoding="utf-8") as f:
        json.dump({"fecha": f"{fecha:%Y-%m-%d}", "departamento": DEPARTAMENTO, "resultados": resultados},
                  f, ensure_ascii=False, indent=2)


def calcular_paquetes_sga(ventana, sots_buscadas, por_cid, total_filas, instalaciones, fecha):
    """Recorre Control de Tareas fila por fila. Para cada SOT buscada:
    - instalación -> paquete con su Detalle Servicios;
    - postventa   -> va a Consulta Histórica, compara con la SOT anterior y vuelve a Control de Tareas;
    - otra        -> se omite.
    Si la lista está vacía devuelve {} (no hay nada que recorrer).
    Devuelve {sot: {"tipo", "paquete" (o None), "motivo"}} y lo va guardando en progreso_paquetes.json."""
    if not total_filas:
        print("No hay SOTs en la lista de Control de Tareas; nada que recorrer en esta pasada.")
        return {}

    control = _ventana_interna(ventana, TITULO_CONTROL_TAREAS)
    lista = _lista_liviana(ventana)
    if control is None or lista is None:
        raise RuntimeError("No encontré Control de Tareas o su lista de SOTs.")
    tabla = _tabla_detalle(_abrir_pestana_detalle(ventana))
    if tabla is None:
        raise RuntimeError("No encontré la tabla de la pestaña Detalle Servicios.")

    # Se empieza desde la primera fila
    clic_primera_fila(ventana, lista)

    buscadas = set(sots_buscadas)
    resultados = {}
    cids_anterior = None
    print(f"Recorriendo {total_filas} filas...")
    for fila in range(1, total_filas + 1):
        limite = time.time() + 10            # si dos filas seguidas comparten CID, máximo 10 s
        cids, servicios = _leer_detalle(tabla)
        while cids == cids_anterior and time.time() < limite:
            time.sleep(0.7)
            _esperar_sin_colgar(ventana)
            cids, servicios = _leer_detalle(tabla)
        cids_anterior = cids
        cid = next((c for c in cids if c in por_cid), None)
        sot, tipo = por_cid.get(cid, ("?", "?"))

        if sot in buscadas and sot not in resultados:
            if es_instalacion(tipo) or sot in instalaciones:
                paquete = calcular_paquete(servicios, tipo)
                resultados[sot] = {"tipo": tipo, "paquete": paquete, "motivo": "instalación"}
                print(f"{sot} | {tipo}  ->  INSTALACION: {paquete}")
            elif paquete_fijo(tipo):            # traslado interno, plume, recojos: paquete directo
                paquete = paquete_fijo(tipo)
                resultados[sot] = {"tipo": tipo, "paquete": paquete, "motivo": "paquete fijo por tipo de trabajo"}
                print(f"{sot} | {tipo}  ->  PAQUETE FIJO: {paquete}")
            elif es_entrega_mesh(tipo):         # WA ENTREGA: mesh según 'Velocidad Total ... - Bono' y repetidores
                paquete = paquete_entrega_mesh(servicios)
                motivo = "entrega de mesh" if paquete else "no encontré 'Velocidad Total/Bono', 'Full Claro' ni repetidores en el detalle"
                resultados[sot] = {"tipo": tipo, "paquete": paquete, "motivo": motivo}
                print(f"{sot} | {tipo}  ->  " + (f"ENTREGA MESH: {paquete}" if paquete else f"SIN PAQUETE: {motivo}"))
                for servicio, cantidad in servicios:
                    print(f"      - {servicio}  x {cantidad}")
            elif es_accesorio_postventa(tipo):  # accesorio adicional: se cuentan los REPETIDORES
                paquete = paquete_accesorio_postventa(servicios)
                motivo = "accesorio adicional" if paquete else "no encontré repetidores en el Detalle Servicios"
                resultados[sot] = {"tipo": tipo, "paquete": paquete, "motivo": motivo}
                print(f"{sot} | {tipo}  ->  " + (f"ACCESORIO: {paquete}" if paquete else f"SIN PAQUETE: {motivo}"))
                for servicio, cantidad in servicios:
                    print(f"      - {servicio}  x {cantidad}")
            elif es_deco_adicional(tipo):       # deco adicional: se cuentan los DECODIFICADORES
                paquete = paquete_deco_adicional(servicios)
                motivo = "deco adicional" if paquete else "no encontré decodificadores en el Detalle Servicios"
                resultados[sot] = {"tipo": tipo, "paquete": paquete, "motivo": motivo}
                print(f"{sot} | {tipo}  ->  " + (f"DECO ADICIONAL: {paquete}" if paquete else f"SIN PAQUETE: {motivo}"))
                for servicio, cantidad in servicios:
                    print(f"      - {servicio}  x {cantidad}")
            elif es_postventa(tipo):
                print(f"{sot} | {tipo}  ->  POSTVENTA: buscando la SOT anterior en Consulta Histórica...")
                actual = {"servicios": [{"servicio": s_, "cantidad": c_} for s_, c_ in servicios]}
                try:
                    datos, anterior = comparar_con_anterior(ventana, sot)
                    actual.update({"observacion": datos.get("observacion", "")})
                except Exception as error:   # una SOT con problemas no detiene las demás
                    anterior = None
                    print(f"   ERROR en Consulta Histórica: {type(error).__name__}: {error}")
                paquete = sugerir_paquete(anterior, actual)
                if not paquete and anterior and anterior.get("servicios"):   # anterior IGUAL a la actual
                    paquete = paquete_por_observacion(actual.get("observacion"))
                    if paquete:
                        print(f"   La SOT anterior es igual; uso la observación: '{actual.get('observacion', '').strip()}'")
                if paquete:
                    motivo = f"postventa (comparada con la SOT anterior {anterior['sot']})"
                    print(f"   -> PAQUETE: {paquete}")
                elif not anterior:
                    motivo = "no encontré una SOT anterior Atendida (instalación, migración o cambio de plan)"
                    print(f"   -> SIN PAQUETE: {motivo}")
                else:
                    motivo = (f"no veo diferencias de equipos con la SOT anterior {anterior['sot']} "
                              f"y la observación no dice INSTALAR ni RETIRAR")
                    print(f"   -> SIN PAQUETE: {motivo}")
                resultados[sot] = {"tipo": tipo, "paquete": paquete, "motivo": motivo}
                _activar(ventana, control)   # de vuelta a Control de Tareas (la SOT sigue seleccionada)
                if not _visible(_pagina_detalle(ventana)):
                    _abrir_pestana_detalle(ventana)
            else:
                print(f"{sot} | {tipo}  -> omitida (todavía no sé llenar este tipo)")
            if sot in resultados:
                _guardar_progreso(fecha, resultados)   # se guarda al instante
        if fila % 10 == 0:
            print(f"   ... fila {fila} de {total_filas}")

        if fila == total_filas:
            break
        _poner_al_frente(ventana)            # siguiente fila: flecha abajo
        lista.set_focus()
        keyboard.send_keys("{DOWN}")
        time.sleep(0.5)
        _esperar_sin_colgar(ventana)
    return resultados


# ---------------- PASO 7B: MANTENIMIENTOS (CONTRATISTA MANTO HFC) ----------------
ARCHIVO_CODIGOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codigos_solucion.json")
PESTANA_AGENDAMIENTO = "Agendamiento"
# Posición de la pestaña 'Agendamiento' desde la esquina de la barra de pestañas (solo si no está abierta)
PESTANA_AGENDAMIENTO_X, PESTANA_AGENDAMIENTO_Y = 224, 14   # pestaña 'Agendamiento' (al 100%, desde la izquierda)


def es_mantenimiento(tipo):
    """Todo tipo de trabajo que diga MANTENIMIENTO, y también HFC - RETENCION y FTTH - AMPLIACION DE FAT."""
    texto = _normalizar(tipo)
    return "MANTENIMIENTO" in texto or "RETENCION" in texto or "AMPLIACION DE FAT" in texto


def _normalizar_motivo(texto):
    """Para comparar motivos: mayúsculas, sin tildes, y sin espacios alrededor de / - + ( ) . ,"""
    texto = _normalizar(texto)
    texto = re.sub(r"\s*([/\-+().,])\s*", r"\1", texto)
    return texto.rstrip(".")


def cargar_codigos():
    """{motivo normalizado: código} desde codigos_solucion.json."""
    with open(ARCHIVO_CODIGOS, encoding="utf-8") as f:
        return {_normalizar_motivo(c["motivo"]): c["codigo"] for c in json.load(f)}


def _pagina_agendamiento(ventana):
    """La página 'Agendamiento' de Control de Tareas (Consulta Histórica tiene otra con el mismo nombre)."""
    control = _ventana_control_tareas(ventana)
    for h in _hijos_livianos(control):
        if _clase_liviana(h) == "FNUDO3170" and _texto_liviano(h) == PESTANA_AGENDAMIENTO:
            return h
    return None


def _ventana_control_tareas(ventana):
    for h in _hijos_livianos(ventana.handle):
        if _clase_liviana(h) == "FNWND3170" and _texto_liviano(h) == TITULO_CONTROL_TAREAS:
            return h
    raise RuntimeError("No encontré Control de Tareas.")


def _abrir_pestana_agendamiento(ventana):
    """Deja abierta la pestaña Agendamiento de Control de Tareas (clic por posición si no lo está)."""
    pagina = _pagina_agendamiento(ventana)
    if pagina is None:
        raise RuntimeError("No encontré la pestaña 'Agendamiento' en Control de Tareas.")
    return clic_pestana(ventana, pagina, PESTANA_AGENDAMIENTO_X, PESTANA_AGENDAMIENTO_Y, nombre=PESTANA_AGENDAMIENTO)


def _tabla_motivos(pagina):
    """La tabla del historial de Agendamiento (Estado Agenda, Fecha Ejecutado, Motivo Solución)."""
    for h in _hijos_livianos(pagina):
        if _clase_liviana(h) != "pbdw170" or _padre(h) != pagina:
            continue
        nombres = {e.element_info.name for e in Desktop(backend="uia").window(handle=h).descendants()}
        if "mot_solucion_descripcion_t" in nombres:
            return h
    raise RuntimeError("No encontré la tabla con el Motivo Solución en la pestaña Agendamiento.")


def _leer_campos(tabla):
    """{nombre de columna: [valores]} de una tabla de SGA."""
    columnas = {}
    for e in Desktop(backend="uia").window(handle=tabla).descendants(control_type="Edit"):
        try:
            valor = (e.iface_value.CurrentValue or "").strip()
        except Exception:
            valor = ""
        columnas.setdefault(e.element_info.name, []).append(valor)
    return columnas


def _motivo_ejecutado(tabla):
    """Motivo de solución de la fila 'Ejecutado' más reciente (por Fecha Ejecutado), o ''."""
    c = _leer_campos(tabla)
    filas = zip(c.get("descripcion", []), c.get("fechaejecutado", []), c.get("mot_solucion_descripcion", []))
    ejecutados = []
    for estado, fecha, motivo in filas:
        if _normalizar(estado) == "EJECUTADO":
            try:
                cuando = datetime.datetime.strptime(fecha, "%d/%m/%Y %H:%M:%S")
            except ValueError:
                cuando = datetime.datetime.min
            ejecutados.append((cuando, motivo))
    return max(ejecutados)[1] if ejecutados else ""


def calcular_mantenimientos(ventana, mantenimientos, fecha, resultados):
    """Recorre Control de Tareas (ya cargado con los mantenimientos en MANTO HFC -> WITLINK). En cada fila lee la
    pestaña Agendamiento: el Motivo Solución de la fila Ejecutado más reciente -> su código -> 'XX00 - SIN SERVICIO'.
    La SOT es la de esa fila en la lista (mismo orden). Agrega lo calculado a 'resultados' y lo guarda en progreso_paquetes.json."""
    codigos = cargar_codigos()
    tabla_lista = exportar_lista(ventana)    # SOT y tipo de cada fila, en el orden de la pantalla
    total_filas = len(tabla_lista)
    if not total_filas:
        print("No hay SOTs de mantenimiento en la lista; nada que recorrer.")
        return resultados
    lista = _lista_liviana(ventana)
    tabla = _tabla_motivos(_abrir_pestana_agendamiento(ventana))   # tabla con el Motivo Solución

    clic_primera_fila(ventana, lista)        # se empieza desde la primera fila

    buscadas = set(mantenimientos)
    def firma():
        """Lo que muestra el historial de Agendamiento (sus fechas): cambia cuando se baja a otra SOT."""
        return tuple(_leer_campos(tabla).get("fecreg", []))

    firma_anterior = None
    print(f"Recorriendo {total_filas} filas de mantenimiento...")
    for fila in range(1, total_filas + 1):
        limite = time.time() + 10            # se espera a que Agendamiento muestre la SOT de esta fila
        actual = firma()
        while actual == firma_anterior and time.time() < limite:
            time.sleep(0.7)
            _esperar_sin_colgar(ventana)
            actual = firma()
        firma_anterior = actual
        # La fila N de la pantalla es la fila N de la lista copiada (mismo orden)
        sot, tipo = tabla_lista[fila - 1]["codsolot"].strip(), tabla_lista[fila - 1]["tipotrabajo"].strip()
        if sot not in buscadas:
            print(f"{sot} | {tipo}  -> fila {fila}: no es una SOT de mantenimiento buscada; se salta")
        elif sot in resultados:
            print(f"{sot} | {tipo}  -> fila {fila}: ya calculada")
        else:
            motivo = _motivo_ejecutado(tabla)
            codigo = codigos.get(_normalizar_motivo(motivo))
            if codigo:
                paquete = f"{codigo} - SIN SERVICIO"
                resultados[sot] = {"tipo": tipo, "paquete": paquete, "motivo": f"mantenimiento: {motivo}"}
                print(f"{sot} | {tipo}  ->  MANTENIMIENTO: '{motivo}' = {paquete}")
            else:
                razon = (f"el motivo '{motivo}' no está en codigos_solucion.json" if motivo
                         else "no tiene fila 'Ejecutado' con motivo de solución en Agendamiento")
                resultados[sot] = {"tipo": tipo, "paquete": None, "motivo": razon}
                print(f"{sot} | {tipo}  ->  SIN PAQUETE: {razon}")
            _guardar_progreso(fecha, resultados)
        if fila % 10 == 0:
            print(f"   ... fila {fila} de {total_filas}")
        if fila == total_filas:
            break
        _poner_al_frente(ventana)            # siguiente fila: flecha abajo
        lista.set_focus()
        keyboard.send_keys("{DOWN}")
        time.sleep(0.5)
        _esperar_sin_colgar(ventana)
    no_vistas = [m for m in mantenimientos if m not in resultados]
    if no_vistas:
        print("AVISO: estas SOTs de mantenimiento no aparecieron en CONTRATISTA MANTO HFC:", ", ".join(no_vistas))
    return resultados


# ---------------- PASO 8: LLENAR EL PAQUETE EN WITLINK ----------------
ESPERA_AUTOGUARDADO_MS = 10 * 1000           # Witlink guarda el paquete solo al elegirlo; hasta 10 s para confirmarlo
PAUSA_POR_SOT_MS = 700                       # pausa después de cada SOT para que puedas ver cómo se llena


def traer_chrome_al_frente(page):
    """Pone la ventana de Chrome delante de SGA (la pestaña sola no basta: la ventana puede estar detrás)."""
    page.bring_to_front()                    # pestaña activa dentro de Chrome
    titulo = page.title()                    # por ejemplo "Programación · SOTs | Witlink"
    for ventana in Desktop(backend="win32").windows(class_name="Chrome_WidgetWin_1", visible_only=True):
        if ventana.window_text().startswith(titulo):
            if ventana.is_minimized():
                ventana.restore()
            ventana.set_focus()              # ventana de Chrome al primer plano
            time.sleep(1)
            print("Chrome en primer plano.")
            return
    print("AVISO: no pude traer Chrome al frente; haz clic en su ícono de la barra de tareas.")


def _resaltar(page, fila):
    """Marca la fila que se está llenando con un borde naranja (solo visual, no cambia nada en Witlink)."""
    page.evaluate("""fila => {
        document.querySelectorAll('tr[data-bot-actual]').forEach(f => {
            f.style.outline = ''; f.removeAttribute('data-bot-actual'); });
        fila.style.outline = '3px solid #f97316'; fila.setAttribute('data-bot-actual', '1');
        fila.scrollIntoView({block: 'center'});
    }""", fila.element_handle())


def _llenar_una_sot(page, sot, paquete):
    """Llena el paquete de la fila ATENDIDA de la SOT. Devuelve (estado, detalle):
    estado = 'llenada', 'ya_tenia', 'distinto', 'sin_fila', 'sin_atendida', 'sin_opcion' o 'no_guardo'."""
    filas = page.locator(f'#prog-tbody tr[data-sot="{sot}"]')          # la SOT puede estar varias veces en el día
    if filas.count() == 0:
        return "sin_fila", "no está en la programación de hoy"
    # Se prefiere la fila ATENDIDA; si no hay, la EN ATENCION
    atendidas = None
    for estado in ESTADOS_BUSCADOS:
        candidatas = filas.filter(has=page.locator(f'td.col-econt[data-val="{estado}"]'))
        if candidatas.count():
            atendidas = candidatas
            break
    if atendidas is None:
        return "sin_atendida", f"aparece {filas.count()} vez/veces pero ninguna está ATENDIDA ni EN ATENCION"
    _resaltar(page, atendidas.first)         # se ve en pantalla qué fila se está llenando
    celda = atendidas.first.locator('td.col-paquete')

    actual = (celda.get_attribute("data-val") or "").strip()
    if actual:                               # ya tenía paquete: no se toca
        if actual.upper() == paquete.upper():
            return "ya_tenia", actual
        return "distinto", f"tiene '{actual}' y yo calculé '{paquete}' (no se cambió)"

    campo = celda.locator("input.wl-tsearch-input")
    campo.scroll_into_view_if_needed()
    campo.click()                            # abre la lista de paquetes
    campo.fill("")
    campo.press_sequentially(paquete, delay=20)   # escribir filtra la lista
    # Opción visible cuyo texto es EXACTAMENTE el paquete (así '1 PLAY MB' no se confunde con '1 PLAY MB + MESH')
    opcion = page.get_by_text(paquete, exact=True).filter(visible=True)
    try:
        opcion.first.wait_for(state="visible", timeout=5000)
    except Exception:
        campo.fill("")                       # se deja el campo vacío como estaba
        campo.press("Escape")
        return "sin_opcion", f"'{paquete}' no aparece en la lista de paquetes de Witlink"
    opcion.first.click()

    # Verificación: la celda (o su campo oculto) queda con el paquete elegido
    try:
        page.wait_for_function(
            """([td, valor]) => (td.getAttribute('data-val') || '').trim().toUpperCase() === valor
                || (td.querySelector('input[type=hidden]')?.value || '').trim().toUpperCase() === valor""",
            arg=[celda.element_handle(), paquete.upper()], timeout=ESPERA_AUTOGUARDADO_MS)
    except Exception:
        return "no_guardo", f"elegí '{paquete}' pero la celda no cambió"
    return "llenada", paquete


def llenar_paquetes_witlink(page, paquetes, fecha):
    """Para cada SOT de instalación ({sot: paquete}) llena su Paquete en la fila ATENDIDA y al final pulsa Guardar.
    Devuelve un resumen {estado: [(sot, detalle), ...]}."""
    traer_chrome_al_frente(page)             # Chrome delante de SGA para que veas el llenado
    # Se abre la programación de ESE día (un simple recargar podría volver a HOY) y se confirma la fecha
    page.goto(url_del_dia(fecha))            # el día y la sede que se están trabajando
    page.locator(f'#btn-prog-export[href*="fecha={fecha:%Y-%m-%d}"]').wait_for(state="attached",
                                                                               timeout=ESPERA_CORTA_MS)
    if SEDE_ACTUAL:                          # se confirma que quedó en la sede correcta
        page.locator(f'a.prog-sede-btn.is-active[href*="sede={SEDE_ACTUAL}"]').wait_for(
            state="visible", timeout=ESPERA_CORTA_MS)
    print(f"Programación del {fecha:%d/%m/%Y} ({nombre_sede()}) abierta para llenar paquetes.")
    page.locator("#prog-tbody").wait_for(state="attached", timeout=ESPERA_CORTA_MS)
    page.locator("#prog-tbody tr[data-sot]").first.wait_for(state="attached", timeout=ESPERA_CORTA_MS)
    print(f"Llenando paquetes en Witlink ({len(paquetes)} SOTs de instalación)...")

    resumen = {}
    for numero, (sot, paquete) in enumerate(paquetes.items(), start=1):
        try:
            estado, detalle = _llenar_una_sot(page, sot, paquete)
        except Exception as error:           # un problema en una SOT no detiene las demás
            estado, detalle = "error", f"{type(error).__name__}: {error}"
        resumen.setdefault(estado, []).append((sot, detalle))
        print(f"   {numero:>3}. {sot}  {paquete:<35} -> {estado}: {detalle}")
        page.wait_for_timeout(PAUSA_POR_SOT_MS)   # pausa para que se vea cada SOT llenada

    # Guardar: se sube al inicio de la página para que se vea el botón y se pulsa
    page.evaluate("""() => {
        document.querySelectorAll('tr[data-bot-actual]').forEach(f => { f.style.outline = ''; });
        window.scrollTo({top: 0, behavior: 'smooth'});
    }""")
    page.wait_for_timeout(1000)
    guardar = page.locator("#btn-guardar")
    guardar.scroll_into_view_if_needed()
    guardar.click()                          # Guardar (confirma lo llenado)
    page.wait_for_timeout(2000)
    print("Botón Guardar pulsado.")
    return resumen


def texto_resumen(resumen):
    """'SOTs con paquetes: N' y, debajo, las que no se llenaron y por qué."""
    llenadas = len(resumen.get("llenada", [])) + len(resumen.get("ya_tenia", []))
    motivos = {"distinto": "Ya tenían otro paquete", "sin_opcion": "Paquete no está en la lista de Witlink",
               "sin_atendida": "Sin fila ATENDIDA ni EN ATENCION", "sin_fila": "No están en la programación de hoy",
               "no_guardo": "No se confirmó el guardado", "no_sga": "Sin detalle en SGA",
               "sin_regla": "SOTs que no supe calcular (en blanco)",
               "error": "Error"}
    texto = f"SOTs con paquetes: {llenadas}"
    if resumen.get("ya_tenia"):
        texto += f"  (de ellas {len(resumen['ya_tenia'])} ya lo tenían)"
    for estado, titulo in motivos.items():
        if resumen.get(estado):
            texto += f"\n\n{titulo} ({len(resumen[estado])}):"
            for sot, detalle in resumen[estado]:
                texto += f"\n  • {sot}: {detalle}"
    return texto


def mostrar_aviso_total(resultados_sedes):
    """Un solo aviso al final con el resumen de cada sede (o el error si una sede falló)."""
    import tkinter as tk
    from tkinter import messagebox
    partes = []
    for nombre, resumen in resultados_sedes:
        if isinstance(resumen, str):         # la sede falló
            partes.append(f"===== {nombre} =====\nERROR: {resumen}")
        else:
            partes.append(f"===== {nombre} =====\n{texto_resumen(resumen)}")
    texto = "\n\n".join(partes)
    print("\n" + texto)
    raiz = tk.Tk()
    raiz.withdraw()                          # sin ventana principal, solo el aviso
    raiz.attributes("-topmost", True)        # el aviso aparece delante de todo
    messagebox.showinfo("Paquetes en Witlink - todas las sedes", texto, parent=raiz)
    raiz.destroy()


# ---------------- PROGRAMA PRINCIPAL ----------------
class _Registro:
    """Hace que todo lo que se imprime salga en la consola Y se guarde en bot_log.txt."""

    def __init__(self, consola, *archivos):
        self.consola, self.archivos = consola, archivos

    def write(self, texto):
        self.consola.write(texto)
        for archivo in self.archivos:
            archivo.write(texto)
            archivo.flush()                  # se guarda al instante, aunque el bot se cierre

    def flush(self):
        self.consola.flush()


def procesar_sede(page, fecha):
    """Todo el proceso de UNA sede (Witlink ya está en ella): tabla de Witlink -> SGA (ALTA BAJA y MANTO HFC) -> llenar
    paquetes en Witlink -> Guardar. Devuelve el resumen del llenado."""
    if DIAS_ATRAS:                                 # 3) otro día: se abre la programación de ESE día y ESA sede
        page.goto(url_del_dia(fecha))
        page.locator(f'#btn-prog-export[href*="fecha={fecha:%Y-%m-%d}"]').wait_for(
            state="attached", timeout=ESPERA_CORTA_MS)
        if SEDE_ACTUAL:
            page.locator(f'a.prog-sede-btn.is-active[href*="sede={SEDE_ACTUAL}"]').wait_for(
                state="visible", timeout=ESPERA_CORTA_MS)
        print(f"Programación del {fecha:%d/%m/%Y} ({nombre_sede()}) abierta.")
    else:                                          # hoy: la página ya está en HOY
        page.locator(f'#btn-prog-export[href*="fecha={fecha:%Y-%m-%d}"]').wait_for(
            state="attached", timeout=ESPERA_CORTA_MS)
        print(f"Programación de hoy ({fecha:%d/%m/%Y}) abierta.")
    todas, instalaciones = leer_sots_witlink(page)   # 4) ATENDIDAS / EN ATENCION de la tabla de Witlink
    print("PASO 1 COMPLETADO.")
    if not todas:
        print(f"No hay SOTs ATENDIDAS ni EN ATENCION en {nombre_sede()}; no hay nada que llenar.")
        return {}
    mantenimientos = list(MANTENIMIENTOS_WITLINK)
    sots = [s for s in todas if s not in mantenimientos]   # 1ra pasada (ALTA BAJA): sin mantenimientos
    copiar_al_portapapeles(sots)                   # 7) copiarlas al portapapeles y verificar
    sga = abrir_sga()                              # 8) traer SGA al frente
    print(f"PASO 2 COMPLETADO. {len(sots)} SOTs atendidas en el portapapeles y SGA al frente.")
    abrir_control_tareas(sga)                      # 9) abrir Control de Tareas
    abrir_witlink_en_arbol(sga)                    # 10) doble clic en CONTRATISTA ALTA BAJA HFC y en WITLINK
    filtros = abrir_buscar(sga)                    # 11) Buscar -> ventana Filtros
    dialogo = clic_puntos_sot(sga, filtros)        # 12) clic en '...' al costado de SOT
    print("PASO 3 COMPLETADO.")
    pegar_y_cargar_sots(sga, dialogo, len(sots))   # 13) pegar, confirmar, palomita y Enter
    print("PASO 4 COMPLETADO.")
    cargar_lista_y_primera_sot(sga)                # 14) bajar hasta el final, subir y clic en la 1ra SOT
    print("PASO 5 COMPLETADO.")
    filas_sga, por_cid, total_filas = leer_sots_y_tipo_trabajo(sga, sots)   # 15) SOT + Tipo de trabajo
    print(f"PASO 6 COMPLETADO. {len(filas_sga)} SOTs con su Tipo de trabajo capturadas.")
    resultados = calcular_paquetes_sga(sga, sots, por_cid, total_filas, instalaciones, fecha)   # 16)
    # Las 'Fuera de TOA' que no aparecieron en ALTA BAJA se buscan también en MANTO HFC
    fuera_toa = [s for s in FUERA_TOA_WITLINK if s not in resultados]
    if fuera_toa:
        print(f"'Fuera de TOA' no encontradas en ALTA BAJA; se buscan en MANTO HFC: {', '.join(fuera_toa)}")
    mantenimientos = mantenimientos + [s for s in fuera_toa if s not in mantenimientos]
    if mantenimientos:                             # 2da pasada: CONTRATISTA MANTO HFC -> WITLINK
        print(f"\nMANTENIMIENTOS: {len(mantenimientos)} SOTs -> CONTRATISTA MANTO HFC")
        copiar_al_portapapeles(mantenimientos)
        abrir_witlink_en_arbol(sga, NODO_MANTENIMIENTO, exacto=True)
        filtros = abrir_buscar(sga)
        dialogo = clic_puntos_sot(sga, filtros)
        pegar_y_cargar_sots(sga, dialogo, len(mantenimientos))
        cargar_lista_y_primera_sot(sga)
        calcular_mantenimientos(sga, mantenimientos, fecha, resultados)
        print("PASO 7B COMPLETADO (mantenimientos).")
    paquetes = {sot: d["paquete"] for sot, d in resultados.items() if d["paquete"]}
    sin_regla = [(sot, f"{d['tipo']}: {d['motivo']}") for sot, d in resultados.items() if not d["paquete"]]
    print(f"PASO 7 COMPLETADO. {len(paquetes)} SOTs con paquete calculado; {len(sin_regla)} sin paquete.")
    for sot, motivo in sin_regla:
        print(f"   SIN PAQUETE {sot}: {motivo}")
    resumen = llenar_paquetes_witlink(page, paquetes, fecha)   # 17) llenar Paquete y Guardar
    if sin_regla:
        resumen["sin_regla"] = sin_regla
    sin_detalle = [s for s in instalaciones if s not in resultados]   # instalaciones de Witlink no leídas en SGA
    if sin_detalle:
        resumen["no_sga"] = [(s, "es instalación en Witlink pero no la encontré en SGA") for s in sin_detalle]
    print("PASO 8 COMPLETADO.")
    return resumen


def main():
    global DEPARTAMENTO, SEDE_ACTUAL         # cambian en cada vuelta del recorrido por sedes
    carpeta = os.path.dirname(os.path.abspath(__file__))
    registro = os.path.join(carpeta, "bot_log.txt")          # el de la última corrida
    historial = os.path.join(carpeta, "registros")            # y una copia por corrida, con fecha y hora
    os.makedirs(historial, exist_ok=True)
    copia = os.path.join(historial, f"bot_log_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    sys.stdout = _Registro(sys.stdout, open(registro, "w", encoding="utf-8"), open(copia, "w", encoding="utf-8"))
    print(f"Corrida del {datetime.datetime.now():%d/%m/%Y %H:%M:%S}")
    fecha = datetime.date.today() - datetime.timedelta(days=DIAS_ATRAS)   # por ejemplo ayer, 23/09/2026
    with sync_playwright() as p:             # inicia Playwright y lo cierra correctamente al terminar
        try:
            navegador, page = conectar_chrome(p)          # tu Chrome, en la pestaña de Programación
        except RuntimeError as error:
            print("ERROR:", error)
            input("Presiona Enter para salir...")
            return

        abrir_programacion(page)                           # confirmar que la pestaña está en Programación
        resultados_sedes = []
        departamento_anterior = None
        for numero, (departamento, sede) in enumerate(SEDES):
            DEPARTAMENTO, SEDE_ACTUAL = departamento, sede
            print(f"\n################ SEDE {numero + 1} de {len(SEDES)}: {nombre_sede()} ################")
            try:
                ir_a_sede(page, departamento, sede, fecha, departamento_anterior)
                resumen = procesar_sede(page, fecha)
                resultados_sedes.append((nombre_sede(), resumen))
                print(f"SEDE {nombre_sede()} TERMINADA.")
                departamento_anterior = departamento
            except Exception as error:       # si una sede falla, se anota y se sigue con la siguiente
                print(f"ERROR en {nombre_sede()}: {type(error).__name__}: {error}")
                resultados_sedes.append((nombre_sede(), f"{type(error).__name__}: {error}"))
                departamento_anterior = None     # tras un error se entra de nuevo por 'Cambiar dep' -> SEDES
                try:
                    captura = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           f"error_{nombre_sede().replace(' ', '_')}.png")
                    page.screenshot(path=captura)
                    print("Captura del error guardada en:", captura)
                except Exception:
                    pass
        mostrar_aviso_total(resultados_sedes)          # un solo aviso con todas las sedes

        input("Presiona Enter para terminar (tu Chrome queda abierto)...")


# Solo ejecuta main() si el archivo se corre directamente (no si se importa desde otro archivo)
if __name__ == "__main__":
    main()
