"""
Go2Facto QA Runner — Flow Automation App
========================================================
Backend Flask para ejecutar flujos de prueba:
  Flujo 1 - Factura Ingreso
  Flujo 2 - Factura PPD
  Flujo 3 - Anticipo + Aplicación Anticipo (2 pasos)
  Flujo 4 - Factura Ingreso + Cancelación (2 pasos)
  Flujo Completo - Todos los flujos en secuencia

Lee requests desde la carpeta ./rqst/
Puerto: 5001   URL: http://127.0.0.1:5001/runner
"""

import base64
import copy
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests as req_lib
from flask import Flask, Response, jsonify, render_template, request

# ─── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(24)

BASE_DIR = Path(__file__).parent
RQST_DIR = BASE_DIR / "rqst"          # única carpeta de requests permitida

DEFAULT_ENDPOINT = (
    "https://opera-go2facto.suite-nt.com/api/v1/operamx-generico/ticket40"
)

# Extensiones válidas de requests
EXTENSIONES_VALIDAS = {".json", ".txt", ".log"}

# Historial en memoria (persistido mientras el server esté vivo)
_historial: list[dict] = []

# ─── Directorios de reportes ──────────────────────────────────────────────────
REPORTS_DIR        = BASE_DIR / "reports"
REPORTS_JSON_DIR   = REPORTS_DIR / "json"
REPORTS_HTML_DIR   = REPORTS_DIR / "html"
REPORTS_MATRIX_DIR = REPORTS_DIR / "matrix"
for _d in [REPORTS_DIR, REPORTS_JSON_DIR, REPORTS_HTML_DIR, REPORTS_MATRIX_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


def limpiar_reportes_viejos(dias: int = 3) -> int:
    """
    Elimina reportes (json + html + csv) con más de `dias` días de antigüedad.
    Devuelve el número de runs eliminados.
    """
    from datetime import timezone
    limite = datetime.now(tz=timezone.utc).timestamp() - dias * 86400
    eliminados = 0
    if not REPORTS_JSON_DIR.exists():
        return 0
    for f in list(REPORTS_JSON_DIR.iterdir()):
        if f.suffix != ".json" or not f.name.startswith("run-"):
            continue
        if f.stat().st_mtime < limite:
            run_id = f.stem[4:]  # quitar "run-"
            for carpeta, ext in [
                (REPORTS_JSON_DIR,   ".json"),
                (REPORTS_HTML_DIR,   ".html"),
                (REPORTS_MATRIX_DIR, ".csv"),
            ]:
                victim = carpeta / f"run-{run_id}{ext}"
                if victim.exists():
                    victim.unlink(missing_ok=True)
            eliminados += 1
    return eliminados


# Limpiar al arrancar el servidor
_eliminated_on_start = limpiar_reportes_viejos()


# ─── Definición de Flujos ─────────────────────────────────────────────────────
#
# Cada flujo tiene uno o más pasos.
# Cada paso define:
#   nombre      → label del paso
#   tipo        → clave interna (usada para lógica de modificación)
#   campos      → lista de campos que el usuario debe ingresar
#                 - id        : nombre del nodo XML o variable interna
#                 - label     : texto para la UI
#                 - required  : si es obligatorio
#                 - auto      : si se calcula automáticamente (no editar)
#                 - auto_rule : descripción de cómo se calcula
#                 - from_step : tomar el valor del paso N (0-indexed)
#                 - from_field: campo del paso N del que copiar el valor
#
# "from_step" / "from_field" permiten reutilizar valores entre pasos

FLUJOS = {
    "flujo1": {
        "label": "Flujo 1 · Factura Ingreso",
        "color": "#4f6ef7",
        "pasos": [
            {
                "nombre": "Factura Ingreso",
                "tipo": "factura",
                "archivo": "Factura.txt",
                "campos": [
                    {
                        "id": "BILLNUMBER",
                        "label": "Bill Number",
                        "required": True,
                        "placeholder": "Ej: 12345",
                    },
                ],
            }
        ],
    },
    "flujo2": {
        "label": "Flujo 2 · Factura PPD",
        "color": "#a78bfa",
        "pasos": [
            {
                "nombre": "Factura PPD",
                "tipo": "factura_ppd",
                "archivo": "RQ PPDa.txt",
                "campos": [
                    {
                        "id": "BILLNUMBER",
                        "label": "Bill Number",
                        "required": True,
                        "placeholder": "Ej: 12345",
                    },
                ],
            }
        ],
    },
    "flujo3": {
        "label": "Flujo 3 · Anticipo + Aplicación Anticipo",
        "color": "#22c55e",
        "pasos": [
            {
                "nombre": "Anticipo",
                "tipo": "anticipo",
                "archivo": "Anticipo 1.txt",
                "campos": [
                    {
                        "id": "BILLNUMBER",
                        "label": "Bill Number del Anticipo",
                        "required": True,
                        "placeholder": "Ej: 5567",
                    },
                    {
                        "id": "CONFIRMATIONNO",
                        "label": "Confirmation No",
                        "required": True,
                        "placeholder": "Ej: 55150",
                    },
                ],
            },
            {
                "nombre": "Aplicación Anticipo",
                "tipo": "aplicacion_anticipo",
                "archivo": "Aplicacion de anticipo 1.txt",
                "campos": [
                    {
                        # BILLNUMBER propio — el contador global lo asigna (NO copia del Anticipo)
                        "id": "BILLNUMBER",
                        "label": "Bill Number Aplicación Anticipo",
                        "required": True,
                        "placeholder": "Asignado automáticamente por el contador",
                    },
                    {
                        "id": "CONFIRMATIONNO",
                        "label": "Confirmation No (mismo del Anticipo)",
                        "required": True,
                        "auto": True,
                        "auto_rule": "Mismo CONFIRMATIONNO del Paso 1",
                        "from_step": 0,
                        "from_field": "CONFIRMATIONNO",
                    },
                    {
                        "id": "SUPPLEMENT",
                        "label": "Supplement (Folio No:{BILLNUMBER Anticipo})",
                        "required": True,
                        "auto": True,
                        "auto_rule": "Folio No:{BILLNUMBER del Anticipo — Paso 1}",
                        "from_step": 0,
                        "from_field": "BILLNUMBER",   # BILLNUMBER que se usó en el Anticipo (paso 0)
                        "template": "Folio No:{value}",
                    },
                ],
            },
        ],
    },
    "flujo4": {
        "label": "Flujo 4 · Factura Ingreso + Cancelación",
        "color": "#ef4444",
        "pasos": [
            {
                "nombre": "Factura Ingreso",
                "tipo": "factura",
                "archivo": "Factura.txt",
                "campos": [
                    {
                        "id": "BILLNUMBER",
                        "label": "Bill Number de la Factura",
                        "required": True,
                        "placeholder": "Ej: 12345",
                    },
                ],
            },
            {
                "nombre": "Cancelación",
                "tipo": "cancelacion",
                "archivo": "RQ CANCEL G2F 1.txt",
                "campos": [
                    {
                        # BILLNUMBER propio de la Cancelación (contador global lo asigna)
                        "id": "BILLNUMBER",
                        "label": "Bill Number Cancelación",
                        "required": True,
                        "placeholder": "Asignado automáticamente por el contador",
                    },
                    {
                        # ASSOCIATED_BILL_NO = BILLNUMBER de la Factura Ingreso (paso 0)
                        "id": "ASSOCIATED_BILL_NO",
                        "label": "ASSOCIATED_BILL_NO (= BillNumber de la Factura Ingreso)",
                        "required": True,
                        "auto": True,
                        "auto_rule": "Copia el BILLNUMBER del Paso 1 (Factura Ingreso)",
                        "from_step": 0,
                        "from_field": "BILLNUMBER",
                    },
                ],
            },
        ],
    },
}


# ─── Funciones de I/O ─────────────────────────────────────────────────────────

def listar_rqst() -> list[dict]:
    """
    Lista únicamente los archivos válidos dentro de ./rqst/.
    Devuelve lista de {name, path, size}.
    """
    if not RQST_DIR.exists():
        raise FileNotFoundError(
            f"La carpeta 'rqst' no existe en: {RQST_DIR}. "
            "Créala y coloca los archivos de request."
        )
    archivos = []
    for f in sorted(RQST_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in EXTENSIONES_VALIDAS:
            archivos.append({
                "name": f.name,
                "path": str(f),
                "size": f.stat().st_size,
            })
    return archivos


def leer_archivo_request(file_path: str) -> dict:
    """
    Lee un archivo de request.
    - Si el contenido es XML crudo (empieza con '<'), devuelve {"_raw_xml": contenido}.
    - Si es JSON con campo 'xml' en Base64, lo parsea normalmente.
    """
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"No se encontró: {path.name}")
    if path.stat().st_size == 0:
        raise ValueError("El archivo está vacío.")

    # Asegurar que el archivo esté dentro de RQST_DIR (seguridad path traversal)
    try:
        path.relative_to(RQST_DIR.resolve())
    except ValueError:
        raise PermissionError("Solo se permiten archivos de la carpeta rqst/.")

    content = path.read_text(encoding="utf-8", errors="replace")

    # Detectar XML crudo (archivos .txt con XML directo)
    if content.lstrip().startswith("<"):
        return {"_raw_xml": content}

    start = content.find("{")
    if start == -1:
        raise ValueError("El archivo no contiene un objeto JSON ni XML válido.")

    data, _ = json.JSONDecoder().raw_decode(content, start)
    return data


def cargar_plantilla_json() -> dict:
    """
    Carga request-postman.json como plantilla para construir el JSON de envío
    cuando el archivo fuente es XML crudo.
    """
    template_path = BASE_DIR / "request-postman.json"
    if not template_path.exists():
        raise FileNotFoundError("No se encontró request-postman.json en la carpeta raíz.")
    content = template_path.read_text(encoding="utf-8")
    # Extraer solo el primer objeto JSON válido (ignora texto extra que pueda quedar)
    start = content.find("{")
    if start == -1:
        raise ValueError("request-postman.json no contiene un objeto JSON válido.")
    data, _ = json.JSONDecoder().raw_decode(content, start)
    return data


def buscar_campo_xml(data: dict) -> str:
    """Busca recursivamente el campo 'xml' en el JSON."""
    if isinstance(data, dict):
        if "xml" in data:
            return data["xml"]
        for v in data.values():
            if isinstance(v, dict):
                try:
                    return buscar_campo_xml(v)
                except KeyError:
                    pass
    raise KeyError("Campo 'xml' no encontrado en el JSON.")


# ─── Funciones XML / Base64 ───────────────────────────────────────────────────

def decodificar_b64(b64: str) -> str:
    if not b64 or not b64.strip():
        raise ValueError("El campo 'xml' está vacío.")
    try:
        return base64.b64decode(b64).decode("utf-8")
    except Exception as e:
        raise ValueError(f"Error al decodificar Base64: {e}")


def codificar_b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def reemplazar_nodo(xml: str, campo: str, valor: str) -> tuple[str, int]:
    """
    Reemplaza <CAMPO>...</CAMPO> o <CAMPO/> con el nuevo valor.
    Retorna (xml_nuevo, num_reemplazos). Lanza ValueError si el nodo no existe.
    """
    esc = re.escape(campo)
    # Etiqueta con contenido
    nuevo, n = re.subn(
        rf"(<{esc}>)(.*?)(</{esc}>)",
        lambda m: f"{m.group(1)}{valor}{m.group(3)}",
        xml,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if n > 0:
        return nuevo, n
    # Etiqueta self-closing
    nuevo2, n2 = re.subn(
        rf"<{esc}\s*/>",
        f"<{campo}>{valor}</{campo}>",
        xml,
        flags=re.IGNORECASE,
    )
    if n2 > 0:
        return nuevo2, n2
    raise ValueError(f"Nodo <{campo}> no encontrado en el XML.")


def reconstruir_json(data: dict, nuevo_b64: str) -> dict:
    """Copia profunda del JSON reemplazando el campo 'xml'."""
    nuevo = copy.deepcopy(data)

    def _set(obj) -> bool:
        if isinstance(obj, dict):
            if "xml" in obj:
                obj["xml"] = nuevo_b64
                return True
            for v in obj.values():
                if _set(v):
                    return True
        elif isinstance(obj, list):
            for item in obj:
                if _set(item):
                    return True
        return False

    if not _set(nuevo):
        raise KeyError("No se pudo localizar 'xml' para reemplazar.")
    return nuevo


# ─── Lógica de ejecución de un paso ──────────────────────────────────────────

def ejecutar_paso(
    file_path: str,
    tipo: str,
    valores: dict,
    endpoint: str,
) -> dict:
    """
    Ejecuta un paso completo:
      Lee archivo → decodifica XML → aplica reemplazos → re-codifica → envía.
    Retorna dict con toda la info de la ejecución.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    resultado = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": ts,
        "file": Path(file_path).name,
        "tipo": tipo,
        "valores": valores,
        "xmlOriginal": None,
        "xmlActualizado": None,
        "requestJson": None,
        "cambios": [],
        "statusCode": None,
        "responseBody": None,
        "uuidGenerado": None,
        "resultado": "ERROR",
        "mensaje": "",
    }

    try:
        # 1. Leer y decodificar
        data = leer_archivo_request(file_path)
        es_xml_puro = "_raw_xml" in data
        if es_xml_puro:
            xml_orig = data["_raw_xml"]
        else:
            b64 = buscar_campo_xml(data)
            xml_orig = decodificar_b64(b64)
        resultado["xmlOriginal"] = xml_orig

        # 2. Aplicar reemplazos según tipo
        xml_new = xml_orig
        cambios = []

        # Determinar orden de campos a modificar
        campos_orden = {
            "factura":             ["BILLNUMBER"],
            "factura_ppd":         ["BILLNUMBER"],
            "anticipo":            ["BILLNUMBER", "CONFIRMATIONNO"],
            "aplicacion_anticipo": ["BILLNUMBER", "CONFIRMATIONNO", "SUPPLEMENT"],
            "cancelacion":         ["BILLNUMBER", "ASSOCIATED_BILL_NO"],
        }.get(tipo, [])

        for campo in campos_orden:
            valor = str(valores.get(campo, "")).strip()
            if not valor:
                raise ValueError(f"El campo '{campo}' es requerido y está vacío.")

            # SUPPLEMENT: solo cambiar el número dentro de etiquetas con contenido "Folio No:XXXX"
            # No tocar las etiquetas <SUPPLEMENT></SUPPLEMENT> vacías de otros cargos
            if campo == "SUPPLEMENT":
                # Extraer solo el número (CONFIRMATIONNO) del valor recibido
                num_match = re.search(r'\d+', valor)
                if not num_match:
                    raise ValueError(f"SUPPLEMENT: no se encontró un número válido en '{valor}'.")
                num = num_match.group()
                nuevo_xml, n = re.subn(
                    r'(<SUPPLEMENT>Folio No:)\d+(</SUPPLEMENT>)',
                    lambda m: f"{m.group(1)}{num}{m.group(2)}",
                    xml_new,
                    flags=re.IGNORECASE,
                )
                if n == 0:
                    raise ValueError(
                        "No se encontró el patrón '<SUPPLEMENT>Folio No:XXXX</SUPPLEMENT>' en el XML. "
                        "Verifica que el archivo correcto esté seleccionado."
                    )
                xml_new = nuevo_xml
                cambios.append({"campo": "SUPPLEMENT", "valor": f"Folio No:{num}", "ocurrencias": n})
                continue

            xml_new, n = reemplazar_nodo(xml_new, campo, valor)
            cambios.append({"campo": campo, "valor": valor, "ocurrencias": n})

        resultado["xmlActualizado"] = xml_new
        resultado["cambios"] = cambios

        # 3. Re-codificar y reconstruir JSON
        nuevo_b64 = codificar_b64(xml_new)
        if es_xml_puro:
            # Usar request-postman.json como plantilla e inyectar solo el Base64 en "xml"
            json_final = cargar_plantilla_json()
            json_final["xml"] = nuevo_b64
        else:
            json_final = reconstruir_json(data, nuevo_b64)
        resultado["requestJson"] = json_final

        # 4. Enviar
        status, body = enviar_http(json_final, endpoint)
        resultado["statusCode"]   = status
        resultado["responseBody"] = body

        # 5. Extraer UUID de la respuesta (si existe)
        uuid_generado = None
        try:
            resp_json = json.loads(body)
            # Buscar campos comunes que contengan UUID: uuid, id, folio, ticketId, etc.
            for clave in ("uuid", "UUID", "id", "folio", "ticketId", "ticket_id",
                          "documentId", "document_id", "cfdi_uuid", "cfdiUUID"):
                if clave in resp_json and resp_json[clave]:
                    uuid_generado = str(resp_json[clave])
                    break
            # Si no encontró en primer nivel, buscar recursivamente
            if not uuid_generado:
                def _buscar_uuid(obj, depth=0):
                    if depth > 4:
                        return None
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(v, str) and re.match(
                                r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', v
                            ):
                                return v
                            found = _buscar_uuid(v, depth + 1)
                            if found:
                                return found
                    elif isinstance(obj, list):
                        for item in obj:
                            found = _buscar_uuid(item, depth + 1)
                            if found:
                                return found
                    return None
                uuid_generado = _buscar_uuid(resp_json)
        except Exception:
            pass
        resultado["uuidGenerado"] = uuid_generado

        # 6. Evaluar resultado
        if 200 <= status < 300:
            resultado["resultado"] = "PASS"
            resultado["mensaje"]   = f"HTTP {status} · OK"
        else:
            resultado["resultado"] = "FAIL"
            resultado["mensaje"]   = f"HTTP {status} · El servidor respondió con error"

    except PermissionError as e:
        resultado["mensaje"] = str(e)
    except FileNotFoundError as e:
        resultado["mensaje"] = str(e)
    except (KeyError, ValueError) as e:
        resultado["mensaje"] = str(e)
    except req_lib.exceptions.ConnectionError:
        resultado["mensaje"] = "No se pudo conectar al endpoint."
    except req_lib.exceptions.Timeout:
        resultado["mensaje"] = "El endpoint no respondió en 30 segundos."
    except Exception as e:
        resultado["mensaje"] = f"Error inesperado: {e}"

    return resultado


def ejecutar_flujo(
    flujo_key: str,
    file_path: str,
    valores_por_paso: list[dict],
    endpoint: str,
    bill_counter: list | None = None,
) -> list[dict]:
    """
    Ejecuta todos los pasos de un flujo en orden.
    Propaga automáticamente valores entre pasos según 'from_step' / 'from_field'.
    bill_counter: lista de un elemento [int] que se incrementa por cada paso
                  que usa BILLNUMBER. Compartido entre flujos para asegurar
                  consecutivos únicos al correr 'todos'.
    Retorna lista de resultados de cada paso.
    """
    flujo = FLUJOS[flujo_key]
    resultados = []
    valores_ejecutados: list[dict] = []   # acumula los valores reales de cada paso
    valores_base = valores_por_paso[0] if valores_por_paso else {}

    for i, paso in enumerate(flujo["pasos"]):
        # Construir valores para este paso (base global o específico del paso)
        if i < len(valores_por_paso):
            valores_paso = dict(valores_por_paso[i])
        else:
            valores_paso = dict(valores_base)  # reutilizar base cuando hay un solo dict

        # Propagar CONFIRMATIONNO desde valores_base si este paso lo necesita y no lo tiene
        if "CONFIRMATIONNO" not in valores_paso and "CONFIRMATIONNO" in valores_base:
            valores_paso["CONFIRMATIONNO"] = valores_base["CONFIRMATIONNO"]

        # Asignar BILLNUMBER desde el contador global (para campos NO auto)
        if bill_counter is not None:
            for campo_cfg in paso["campos"]:
                if campo_cfg["id"] == "BILLNUMBER" and not campo_cfg.get("auto"):
                    valores_paso["BILLNUMBER"] = str(bill_counter[0])
                    bill_counter[0] += 1
                    break

        # Resolver campos automáticos (from_step/from_field/template)
        for campo_cfg in paso["campos"]:
            if campo_cfg.get("auto"):
                from_step  = campo_cfg.get("from_step", 0)
                from_field = campo_cfg.get("from_field", "")
                template   = campo_cfg.get("template", "{value}")
                # Obtener el valor del paso anterior ya ejecutado
                if from_step < len(valores_ejecutados):
                    base_val = str(valores_ejecutados[from_step].get(from_field, "")).strip()
                    computed = template.replace("{value}", base_val)
                    valores_paso[campo_cfg["id"]] = computed

        # Usar archivo definido en el paso; si no, usar el file_path recibido
        archivo_paso = paso.get("archivo")
        file_paso = str(RQST_DIR / archivo_paso) if archivo_paso else file_path

        # Esperar entre pasos para dar tiempo al API de procesar el paso anterior
        if i > 0:
            time.sleep(3)

        resultado = ejecutar_paso(
            file_path = file_paso,
            tipo      = paso["tipo"],
            valores   = valores_paso,
            endpoint  = endpoint,
        )
        resultado["flujo"]      = flujo_key
        resultado["flujoLabel"] = flujo["label"]
        resultado["paso"]       = paso["nombre"]
        resultado["pasoIndex"]  = i

        resultados.append(resultado)
        valores_ejecutados.append(valores_paso)

        # Si un paso falla con ERROR (no FAIL), detener los siguientes pasos del flujo
        if resultado["resultado"] == "ERROR":
            # Agregar pasos omitidos
            for j, paso_omitido in enumerate(flujo["pasos"][i+1:], start=i+1):
                resultados.append({
                    "id": str(uuid.uuid4())[:8],
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "file": Path(file_path).name,
                    "tipo": paso_omitido["tipo"],
                    "flujo": flujo_key,
                    "flujoLabel": flujo["label"],
                    "paso": paso_omitido["nombre"],
                    "pasoIndex": j,
                    "resultado": "OMITIDO",
                    "mensaje": f"Omitido porque el Paso {i+1} dio ERROR",
                    "statusCode": None,
                    "cambios": [],
                })
            break

    return resultados


def enviar_http(payload: dict, endpoint: str) -> tuple[int, str]:
    resp = req_lib.post(
        endpoint,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    return resp.status_code, resp.text


# ─── Rutas Flask ──────────────────────────────────────────────────────────────

# ─── Generación de reportes ───────────────────────────────────────────────────

def _html_escape(s) -> str:
    """Escapa caracteres HTML para uso seguro en templates."""
    return (str(s) if s is not None else "").replace(
        "&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _csv_cell(v) -> str:
    """Escapa un valor para celda CSV (RFC 4180)."""
    s = "" if v is None else str(v)
    if any(c in s for c in [",", '"', "\n", "\r"]):
        return '"' + s.replace('"', '""') + '"'
    return s


def generar_csv_matriz(resultados: list[dict], run_ts: str = "") -> str:
    """Genera CSV con la matriz de pruebas completa (UTF-8, separador coma)."""
    COLS = [
        "Caso de Prueba", "Fecha", "Archivo", "Flujo", "Paso",
        "Campos Modificados", "UUID Generado",
        "Request Enviado", "Response del API",
        "Resultado Esperado", "Resultado Obtenido", "Estatus",
        "Código HTTP", "Mensaje", "Observaciones",
    ]
    rows = [",".join(_csv_cell(c) for c in COLS)]
    for i, r in enumerate(resultados, 1):
        campos_mod = " | ".join(
            f"{c['campo']}={c['valor']}" for c in (r.get("cambios") or [])
        )
        req_str = (
            json.dumps(r.get("requestJson"), ensure_ascii=False)
            if r.get("requestJson") else ""
        )
        rows.append(",".join(_csv_cell(v) for v in [
            str(i),
            r.get("timestamp") or run_ts,
            r.get("file", ""),
            r.get("flujoLabel") or r.get("flujo", ""),
            r.get("paso") or r.get("tipo", ""),
            campos_mod,
            r.get("uuidGenerado") or "",
            req_str,
            r.get("responseBody") or "",
            "PASS",
            r.get("resultado", ""),
            r.get("resultado", ""),
            str(r.get("statusCode")) if r.get("statusCode") is not None else "",
            r.get("mensaje", ""),
            "",
        ]))
    return "\r\n".join(rows)


def generar_html_reporte(run_id: str, run_data: dict, resumen: dict) -> str:
    """Genera HTML esquemático y autocontenido del reporte de ejecución."""
    resultados = run_data.get("resultados", [])
    flujo_key  = run_data.get("flujo", "")
    ts         = run_data.get("timestamp", "")
    endpoint   = run_data.get("endpoint", "")
    archivo    = run_data.get("file", "")
    pas   = resumen.get("pass",    0)
    fail  = resumen.get("fail",    0)
    err   = resumen.get("error",   0)
    omit  = resumen.get("omitido", 0)
    total = resumen.get("total",   0)
    pct   = resumen.get("pct_exito", 0)

    def badge_html(resultado: str) -> str:
        clr = {
            "PASS":    "#22c55e", "FAIL": "#ef4444",
            "ERROR":   "#f59e0b", "OMITIDO": "#94a3b8",
        }.get(resultado, "#94a3b8")
        return (
            f'<span style="padding:3px 9px;border-radius:4px;font-size:.75rem;'
            f'font-weight:700;color:{clr};background:{clr}22;'
            f'border:1px solid {clr}55">{resultado}</span>'
        )

    def pre_box(label: str, content) -> str:
        safe = _html_escape(content or "—")
        return (
            '<div>'
            f'<p style="font-size:.72rem;color:#64748b;text-transform:uppercase;'
            f'font-weight:600;margin-bottom:5px">{label}</p>'
            f'<pre style="background:#0f172a;color:#e2e8f0;padding:10px;'
            f'border-radius:6px;font-size:.7rem;max-height:200px;overflow:auto;'
            f'white-space:pre-wrap;word-break:break-all">{safe}</pre>'
            '</div>'
        )

    tbody_rows = []
    for i, r in enumerate(resultados, 1):
        campos = " | ".join(
            f"{c['campo']}={c['valor']}" for c in (r.get("cambios") or [])
        ) or "—"
        req_str  = json.dumps(r.get("requestJson") or {}, ensure_ascii=False, indent=2) if r.get("requestJson") else "—"
        resp_str = r.get("responseBody") or "—"
        xml_orig = r.get("xmlOriginal") or "—"
        xml_new  = r.get("xmlActualizado") or "—"
        uuid_val = _html_escape(r.get("uuidGenerado") or "—")
        sc       = r.get("statusCode")
        sc_html  = (
            f'<b style="color:{"#22c55e" if sc and 200 <= sc < 300 else "#ef4444"}">{sc}</b>'
            if sc is not None else "—"
        )
        res = r.get("resultado", "—")
        msg = _html_escape(r.get("mensaje") or "")
        detail = (
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:14px 16px">'
            + pre_box("XML Original",     xml_orig)
            + pre_box("XML Modificado",   xml_new)
            + pre_box("Request Enviado",  req_str)
            + pre_box("Response del API", resp_str)
            + '</div>'
        )
        tbody_rows.append(
            f'<tr style="border-bottom:1px solid #e8ecf5">'
            f'<td style="padding:8px 10px;color:#94a3b8">{i}</td>'
            f'<td style="padding:8px 10px">{_html_escape(r.get("flujoLabel") or r.get("flujo","—"))}</td>'
            f'<td style="padding:8px 10px">{_html_escape(r.get("paso") or r.get("tipo","—"))}</td>'
            f'<td style="padding:8px 10px">{_html_escape(r.get("file","—"))}</td>'
            f'<td style="padding:8px 10px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{_html_escape(campos)}">{_html_escape(campos)}</td>'
            f'<td style="padding:8px 10px;font-family:monospace;font-size:.78rem;color:#0ea5e9">{uuid_val}</td>'
            f'<td style="padding:8px 10px">{sc_html}</td>'
            f'<td style="padding:8px 10px">{badge_html(res)}</td>'
            f'<td style="padding:8px 10px;color:#64748b;font-size:.78rem">{msg}</td>'
            f'<td style="padding:8px 10px;color:#94a3b8;font-size:.75rem;white-space:nowrap">{_html_escape(r.get("timestamp","—"))}</td>'
            f'</tr>'
            f'<tr style="background:#f8fafc;border-bottom:2px solid #e8ecf5">'
            f'<td colspan="10" style="padding:0">'
            f'<details><summary style="cursor:pointer;padding:8px 16px;font-size:.8rem;color:#4f6ef7;list-style:none">▶ Ver detalle (Request / Response / XML)</summary>'
            f'{detail}</details>'
            f'</td></tr>'
        )

    tbody_html = "\n".join(tbody_rows)
    gen_ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    css = (
        "*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }\n"
        "body { font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; background: #f0f2f8; color: #1e2235; }\n"
        ".page { max-width: 1200px; margin: 0 auto; padding: 30px 20px; }\n"
        ".hdr { background: #1e2235; color: #fff; border-radius: 12px; padding: 24px 28px; margin-bottom: 24px; }\n"
        ".hdr h1 { font-size: 1.4rem; font-weight: 700; color: #4f6ef7; margin-bottom: 12px; }\n"
        ".hdr-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; }\n"
        ".hdr-meta span { font-size: .82rem; color: #94a3b8; }\n"
        ".hdr-meta strong { color: #e2e8f0; }\n"
        ".summary { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 24px; }\n"
        ".sc { background: #fff; border-radius: 10px; padding: 16px; text-align: center; border: 1px solid #dde1f0; box-shadow: 0 1px 3px rgba(0,0,0,.06); }\n"
        ".sc .val { font-size: 2rem; font-weight: 700; }\n"
        ".sc .lbl { font-size: .7rem; color: #64748b; margin-top: 4px; text-transform: uppercase; letter-spacing: .04em; }\n"
        ".sc-p .val { color: #22c55e; } .sc-f .val { color: #ef4444; }\n"
        ".sc-e .val { color: #f59e0b; } .sc-pct .val { color: #4f6ef7; }\n"
        ".section { background: #fff; border-radius: 10px; border: 1px solid #dde1f0; margin-bottom: 20px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); }\n"
        ".sec-hdr { background: #f5f7fc; padding: 12px 18px; border-bottom: 1px solid #dde1f0; font-weight: 600; font-size: .9rem; }\n"
        "table { width: 100%; border-collapse: collapse; font-size: .82rem; }\n"
        "th { background: #f5f7fc; color: #64748b; font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; padding: 9px 10px; text-align: left; border-bottom: 1px solid #dde1f0; }\n"
        "details > summary { list-style: none; } details > summary::-webkit-details-marker { display: none; }\n"
        ".footer { text-align: center; color: #94a3b8; font-size: .78rem; margin-top: 28px; padding-top: 18px; border-top: 1px solid #dde1f0; }\n"
        "@media (max-width: 600px) { .summary { grid-template-columns: repeat(2,1fr); } }\n"
        "@media print { body { background: #fff; } .page { padding: 10px; } }\n"
    )

    return (
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n'
        '  <meta charset="UTF-8"/>\n'
        '  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>\n'
        f'  <title>Go2Facto · Reporte {_html_escape(run_id)}</title>\n'
        f'  <style>{css}</style>\n'
        '</head>\n<body>\n<div class="page">\n'
        '  <div class="hdr">\n'
        '    <h1>&#128203; Go2Facto · Reporte de Ejecución</h1>\n'
        '    <div class="hdr-meta">\n'
        f'      <span><strong>Run ID:</strong> {_html_escape(run_id)}</span>\n'
        f'      <span><strong>Fecha:</strong> {_html_escape(ts)}</span>\n'
        f'      <span><strong>Flujo:</strong> {_html_escape(flujo_key)}</span>\n'
        f'      <span><strong>Archivo:</strong> {_html_escape(archivo)}</span>\n'
        f'      <span><strong>Endpoint:</strong> {_html_escape(endpoint)}</span>\n'
        '    </div>\n  </div>\n'
        '  <div class="summary">\n'
        f'    <div class="sc"><div class="val">{total}</div><div class="lbl">Total Pasos</div></div>\n'
        f'    <div class="sc sc-p"><div class="val">{pas}</div><div class="lbl">PASS</div></div>\n'
        f'    <div class="sc sc-f"><div class="val">{fail}</div><div class="lbl">FAIL</div></div>\n'
        f'    <div class="sc sc-e"><div class="val">{err + omit}</div><div class="lbl">ERROR/OMIT</div></div>\n'
        f'    <div class="sc sc-pct"><div class="val">{pct}%</div><div class="lbl">% Éxito</div></div>\n'
        '  </div>\n'
        '  <div class="section">\n'
        '    <div class="sec-hdr">Resultados por Paso</div>\n'
        '    <div style="overflow-x:auto">\n'
        '      <table><thead><tr>\n'
        '        <th>#</th><th>Flujo</th><th>Paso</th><th>Archivo</th>'
        '<th>Campos Modificados</th><th>UUID</th><th>HTTP</th>'
        '<th>Resultado</th><th>Mensaje</th><th>Timestamp</th>\n'
        '      </tr></thead>\n'
        f'      <tbody>{tbody_html}</tbody></table>\n'
        '    </div>\n  </div>\n'
        f'  <div class="footer">Generado por Go2Facto QA Runner &middot; {gen_ts}</div>\n'
        '</div>\n</body>\n</html>'
    )


# ─── Rutas Flask ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/runner")
def runner_index():
    return render_template("runner.html", default_endpoint=DEFAULT_ENDPOINT)


@app.route("/runner/api/flujos", methods=["GET"])
def api_flujos():
    """Devuelve la definición de todos los flujos para la UI."""
    return jsonify({"ok": True, "flujos": FLUJOS})


@app.route("/runner/api/files", methods=["GET"])
def api_files():
    """Lista los archivos de la carpeta rqst/."""
    try:
        archivos = listar_rqst()
        return jsonify({
            "ok": True,
            "archivos": archivos,
            "carpeta": str(RQST_DIR),
            "total": len(archivos),
        })
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error al listar archivos: {e}"}), 500


@app.route("/runner/api/load", methods=["POST"])
def api_load():
    """Carga un archivo, decodifica Base64, devuelve XML y valores actuales."""
    try:
        body      = request.get_json(force=True) or {}
        file_path = str(body.get("filePath", "")).strip()
        if not file_path:
            return jsonify({"ok": False, "error": "filePath es requerido."}), 400

        data = leer_archivo_request(file_path)
        if "_raw_xml" in data:
            xml = data["_raw_xml"]
        else:
            b64 = buscar_campo_xml(data)
            xml = decodificar_b64(b64)

        # Extraer valores actuales de campos de interés
        campos_interes = ["BILLNUMBER", "CONFIRMATIONNO", "SUPPLEMENT", "ASSOCIATED_BILL_NO"]
        valores = {}
        for c in campos_interes:
            m = re.search(rf"<{re.escape(c)}>(.*?)</{re.escape(c)}>", xml, re.IGNORECASE | re.DOTALL)
            valores[c] = m.group(1).strip() if m else None

        return jsonify({
            "ok": True,
            "fileName": Path(file_path).name,
            "xmlDecoded": xml,
            "valoresActuales": valores,
        })

    except (FileNotFoundError, PermissionError, KeyError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 422
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error: {e}"}), 500


@app.route("/runner/api/run", methods=["POST"])
def api_run():
    """
    Ejecuta un flujo (o todos) con los datos recibidos.
    Body:
      {
        flujoKey:       "flujo1" | "flujo2" | "flujo3" | "flujo4" | "todos",
        filePath:       "/ruta/al/archivo.json",
        valorasPorPaso: [ {campo: valor}, ... ],   // un dict por paso
        endpoint:       "https://...",
      }
    """
    try:
        body        = request.get_json(force=True) or {}
        flujo_key   = str(body.get("flujoKey", "")).strip()
        file_path   = str(body.get("filePath", "")).strip()
        valores_raw = body.get("valoresPorPaso", [])
        endpoint    = str(body.get("endpoint", DEFAULT_ENDPOINT)).strip()
        test_name   = str(body.get("testName", "")).strip()[:120]

        if not flujo_key:
            return jsonify({"ok": False, "error": "flujoKey es requerido."}), 400
        if not file_path:
            return jsonify({"ok": False, "error": "filePath es requerido."}), 400

        # Asegurarnos de que es lista de dicts
        if not isinstance(valores_raw, list):
            valores_raw = [valores_raw]

        todos_resultados: list[dict] = []

        flujos_a_correr = list(FLUJOS.keys()) if flujo_key == "todos" else [flujo_key]

        if flujo_key not in FLUJOS and flujo_key != "todos":
            return jsonify({"ok": False, "error": f"Flujo desconocido: '{flujo_key}'."}), 400

        # Crear contador global de BILLNUMBER (se comparte entre todos los flujos)
        base_bn = None
        for vals in valores_raw:
            if isinstance(vals, dict) and "BILLNUMBER" in vals:
                try:
                    base_bn = int(vals["BILLNUMBER"])
                except (ValueError, TypeError):
                    pass
                break
        bill_counter = [base_bn] if base_bn is not None else None

        for fk in flujos_a_correr:
            res = ejecutar_flujo(fk, file_path, valores_raw, endpoint, bill_counter=bill_counter)
            todos_resultados.extend(res)

        # Guardar en historial
        run_id = str(uuid.uuid4())[:8]
        run_ts = datetime.now().isoformat(timespec="seconds")
        _historial.append({
            "runId":      run_id,
            "timestamp":  run_ts,
            "flujoKey":   flujo_key,
            "testName":   test_name,
            "file":       Path(file_path).name if file_path else "múltiples",
            "endpoint":   endpoint,
            "resultados": todos_resultados,
        })
        # Mantener solo los últimos 100 runs en memoria
        if len(_historial) > 100:
            _historial.pop(0)

        # Estadísticas resumen
        totales = len(todos_resultados)
        pas  = sum(1 for r in todos_resultados if r["resultado"] == "PASS")
        fail = sum(1 for r in todos_resultados if r["resultado"] == "FAIL")
        err  = sum(1 for r in todos_resultados if r["resultado"] == "ERROR")
        omit = sum(1 for r in todos_resultados if r["resultado"] == "OMITIDO")

        resumen = {
            "total":     totales,
            "pass":      pas,
            "fail":      fail,
            "error":     err,
            "omitido":   omit,
            "pct_exito": round((pas / totales * 100) if totales else 0, 1),
        }

        # ── Persistir reportes en disco ───────────────────────────────────────
        try:
            run_export = {
                "execution_id": run_id,
                "timestamp":    run_ts,
                "testName":     test_name,
                "endpoint":     endpoint,
                "file":         Path(file_path).name if file_path else "múltiples",
                "flujo":        flujo_key,
                "resultados":   todos_resultados,
                "resumen":      resumen,
            }
            # JSON
            (REPORTS_JSON_DIR / f"run-{run_id}.json").write_text(
                json.dumps(run_export, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # CSV (con BOM para Excel)
            csv_str = "\ufeff" + generar_csv_matriz(todos_resultados, run_ts)
            (REPORTS_MATRIX_DIR / f"run-{run_id}.csv").write_text(
                csv_str, encoding="utf-8"
            )
            # HTML
            (REPORTS_HTML_DIR / f"run-{run_id}.html").write_text(
                generar_html_reporte(run_id, run_export, resumen), encoding="utf-8"
            )
        except Exception as _save_err:
            app.logger.warning("Error guardando reportes en disco: %s", _save_err)

        return jsonify({
            "ok":         True,
            "runId":      run_id,
            "timestamp":  run_ts,
            "resultados": todos_resultados,
            "resumen":    resumen,
        })

    except Exception as e:
        return jsonify({"ok": False, "error": f"Error en ejecución: {e}"}), 500


@app.route("/runner/api/historial", methods=["GET"])
def api_historial():
    """Devuelve el historial de ejecuciones (sin el detalle XML para reducir peso)."""
    resumen = []
    for run in reversed(_historial):
        rs = run["resultados"]
        pas  = sum(1 for r in rs if r["resultado"] == "PASS")
        fail = sum(1 for r in rs if r["resultado"] == "FAIL")
        err  = sum(1 for r in rs if r["resultado"] in ("ERROR", "OMITIDO"))
        resumen.append({
            "runId":     run["runId"],
            "timestamp": run["timestamp"],
            "flujoKey":  run["flujoKey"],
            "file":      run["file"],
            "total":     len(rs),
            "pass":      pas,
            "fail":      fail,
            "error":     err,
        })
    return jsonify({"ok": True, "historial": resumen})


@app.route("/runner/api/download-run", methods=["POST"])
def api_download_run():
    """Descarga el resultado de una corrida como JSON."""
    try:
        body   = request.get_json(force=True) or {}
        run_id = str(body.get("runId", "")).strip()

        run = next((r for r in _historial if r["runId"] == run_id), None)
        if not run:
            return jsonify({"ok": False, "error": f"Run '{run_id}' no encontrado."}), 404

        # Sanear XMLs para no exponer datos innecesarios en descarga
        export = copy.deepcopy(run)
        json_str = json.dumps(export, ensure_ascii=False, indent=2)

        return Response(
            json_str,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename=run-{run_id}.json"},
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─── Endpoints de Reportes ────────────────────────────────────────────────────

@app.route("/runner/api/reportes", methods=["GET"])
def api_reportes():
    """Lista todos los reportes guardados en disco (orden descendente). Limpia los viejos al consultar."""
    try:
        limpiar_reportes_viejos()   # auto-limpieza en cada consulta
        reportes = []
        if REPORTS_JSON_DIR.exists():
            for f in sorted(REPORTS_JSON_DIR.iterdir(), reverse=True):
                if f.suffix == ".json" and f.name.startswith("run-"):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        res  = data.get("resumen", {})
                        reportes.append({
                            "runId":     data.get("execution_id", f.stem[4:]),
                            "timestamp": data.get("timestamp", ""),
                            "testName":  data.get("testName", ""),
                            "flujo":     data.get("flujo", ""),
                            "archivo":   data.get("file", ""),
                            "endpoint":  data.get("endpoint", ""),
                            "total":     res.get("total",    0),
                            "pass":      res.get("pass",     0),
                            "fail":      res.get("fail",     0),
                            "error":     res.get("error", 0) + res.get("omitido", 0),
                            "pct":       res.get("pct_exito", 0),
                        })
                    except Exception:
                        pass
        return jsonify({"ok": True, "reportes": reportes, "total": len(reportes)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/runner/api/reportes/<run_id>", methods=["DELETE"])
def api_borrar_reporte(run_id):
    """Elimina todos los archivos de un reporte (json, html, csv)."""
    try:
        safe_id = re.sub(r"[^a-zA-Z0-9\-]", "", run_id)[:20]
        eliminado = False
        for carpeta, ext in [
            (REPORTS_JSON_DIR,   ".json"),
            (REPORTS_HTML_DIR,   ".html"),
            (REPORTS_MATRIX_DIR, ".csv"),
        ]:
            victim = carpeta / f"run-{safe_id}{ext}"
            if victim.exists():
                victim.unlink()
                eliminado = True
        if not eliminado:
            return jsonify({"ok": False, "error": f"Reporte '{safe_id}' no encontrado."}), 404
        # Quitar del historial en memoria si está
        global _historial
        _historial = [r for r in _historial if r.get("runId") != safe_id]
        return jsonify({"ok": True, "deleted": safe_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/runner/api/reportes", methods=["DELETE"])
def api_borrar_todos_reportes():
    """Elimina TODOS los reportes guardados en disco."""
    try:
        total = 0
        for carpeta in [REPORTS_JSON_DIR, REPORTS_HTML_DIR, REPORTS_MATRIX_DIR]:
            if carpeta.exists():
                for f in carpeta.iterdir():
                    if f.is_file() and f.name.startswith("run-"):
                        f.unlink(missing_ok=True)
                        if carpeta == REPORTS_JSON_DIR:
                            total += 1
        global _historial
        _historial = []
        return jsonify({"ok": True, "deleted": total})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/runner/api/reportes/<run_id>", methods=["GET"])
def api_reporte_detalle(run_id):
    """Devuelve el detalle completo de un reporte guardado."""
    try:
        safe_id = re.sub(r"[^a-zA-Z0-9\-]", "", run_id)[:20]
        f = REPORTS_JSON_DIR / f"run-{safe_id}.json"
        if not f.exists():
            return jsonify({"ok": False, "error": f"Reporte '{safe_id}' no encontrado."}), 404
        data = json.loads(f.read_text(encoding="utf-8"))
        return jsonify({"ok": True, "reporte": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/runner/api/download/json/<run_id>", methods=["GET"])
def api_dl_json(run_id):
    """Descarga el reporte JSON de una corrida."""
    safe_id = re.sub(r"[^a-zA-Z0-9\-]", "", run_id)[:20]
    f = REPORTS_JSON_DIR / f"run-{safe_id}.json"
    if not f.exists():
        return jsonify({"ok": False, "error": "Reporte JSON no encontrado."}), 404
    return Response(
        f.read_bytes(),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=run-{safe_id}.json"},
    )


@app.route("/runner/api/download/html/<run_id>", methods=["GET"])
def api_dl_html(run_id):
    """Descarga el reporte HTML esquemático de una corrida."""
    safe_id = re.sub(r"[^a-zA-Z0-9\-]", "", run_id)[:20]
    f = REPORTS_HTML_DIR / f"run-{safe_id}.html"
    if not f.exists():
        return jsonify({"ok": False, "error": "Reporte HTML no encontrado."}), 404
    return Response(
        f.read_bytes(),
        mimetype="text/html",
        headers={"Content-Disposition": f"attachment; filename=reporte-{safe_id}.html"},
    )


@app.route("/runner/api/download/matrix/<run_id>", methods=["GET"])
def api_dl_matrix(run_id):
    """Descarga la matriz de pruebas CSV de una corrida."""
    safe_id = re.sub(r"[^a-zA-Z0-9\-]", "", run_id)[:20]
    f = REPORTS_MATRIX_DIR / f"run-{safe_id}.csv"
    if not f.exists():
        return jsonify({"ok": False, "error": "Matriz CSV no encontrada."}), 404
    return Response(
        f.read_bytes(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=matriz-{safe_id}.csv"},
    )


@app.route("/runner/api/download/matrix-all", methods=["GET"])
def api_dl_matrix_all():
    """Genera y descarga una matriz unificada con TODOS los reportes guardados."""
    try:
        todos: list[dict] = []
        if REPORTS_JSON_DIR.exists():
            for f in sorted(REPORTS_JSON_DIR.iterdir()):
                if f.suffix == ".json" and f.name.startswith("run-"):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        todos.extend(data.get("resultados", []))
                    except Exception:
                        pass
        if not todos:
            return jsonify({"ok": False, "error": "No hay reportes guardados aún."}), 404
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv = "\ufeff" + generar_csv_matriz(todos, ts)
        return Response(
            csv,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=matriz-completa-{ts}.csv"},
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 62)
    print("  Go2Facto QA Runner — Flow Automation App")
    print(f"  Carpeta de requests: {RQST_DIR}")
    print("  Servidor en:         http://127.0.0.1:5001")
    print("  Abrir en navegador:  http://127.0.0.1:5001/runner")
    print("=" * 62)
    app.run(debug=True, host="127.0.0.1", port=5001)
