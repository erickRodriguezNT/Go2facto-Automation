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
                "archivo": "factura.txt",
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

        return jsonify({
            "ok":         True,
            "runId":      run_id,
            "timestamp":  run_ts,
            "resultados": todos_resultados,
            "resumen": {
                "total":   totales,
                "pass":    pas,
                "fail":    fail,
                "error":   err,
                "omitido": omit,
                "pct_exito": round((pas / totales * 100) if totales else 0, 1),
            },
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


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 62)
    print("  Go2Facto QA Runner — Flow Automation App")
    print(f"  Carpeta de requests: {RQST_DIR}")
    print("  Servidor en:         http://127.0.0.1:5001")
    print("  Abrir en navegador:  http://127.0.0.1:5001/runner")
    print("=" * 62)
    app.run(debug=True, host="127.0.0.1", port=5001)


import base64
import copy
import json
import os
import re

import requests as req_lib
from flask import Flask, Response, jsonify, render_template, request
from pathlib import Path


# ─── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(24)

DEFAULT_ENDPOINT = (
    "https://opera-go2facto.suite-nt.com/api/v1/operamx-generico/ticket40"
)

# Extensiones de archivo permitidas para el explorador
EXTENSIONES_VALIDAS = {".json", ".txt"}


# ─── Mapeo centralizado de tipos de request ───────────────────────────────────
# Cada tipo define:
#   - label:   nombre visible en UI
#   - campos:  lista de campos a editar (ordenados)
#   - notas:   ayuda contextual para el usuario
#
# Campos especiales:
#   auto=True          → Se pre-genera automáticamente (editable por el usuario)
#   helper             → Texto de ayuda debajo del input
#   companion_of       → Este campo ayuda a generar el campo 'auto'

TIPOS_REQUEST = {
    "factura": {
        "label": "Factura",
        "campos": [
            {
                "id": "BILLNUMBER",
                "label": "Bill Number",
                "required": True,
                "placeholder": "Ej: 12345",
            },
            {
                "id": "CONFIRMATIONNO",
                "label": "Confirmation No",
                "required": False,
                "placeholder": "Ej: 99999",
                "helper": "Opcional",
            },
        ],
        "notas": "Modifica BILLNUMBER en el XML. CONFIRMATIONNO es opcional.",
    },
    "anticipo": {
        "label": "Anticipo",
        "campos": [
            {
                "id": "BILLNUMBER",
                "label": "Bill Number del Anticipo",
                "required": True,
                "placeholder": "Ej: 12345",
            },
            {
                "id": "CONFIRMATIONNO",
                "label": "Confirmation No",
                "required": True,
                "placeholder": "Ej: 99999",
            },
        ],
        "notas": "Modifica BILLNUMBER y CONFIRMATIONNO en el XML.",
    },
    "aplicacion_anticipo": {
        "label": "Aplicación Anticipo",
        "campos": [
            {
                "id": "BILLNUMBER",
                "label": "Bill Number (de la Aplicación)",
                "required": True,
                "placeholder": "Ej: 12346",
            },
            {
                "id": "CONFIRMATIONNO",
                "label": "Confirmation No (= mismo del Anticipo)",
                "required": True,
                "placeholder": "Ej: 99999",
            },
            {
                "id": "_anticipo_billno",
                "label": "Bill Number del Anticipo (para SUPPLEMENT)",
                "required": True,
                "placeholder": "Ej: 12345",
                "helper": "Se usará para construir SUPPLEMENT automáticamente",
                "companion_of": "SUPPLEMENT",
            },
            {
                "id": "SUPPLEMENT",
                "label": "Supplement (auto-generado, editable)",
                "required": True,
                "placeholder": "Folio No:{BillNoAnticipo}",
                "auto": True,
                "helper": "⚡ Se auto-completa como 'Folio No:{BillNo Anticipo}'",
            },
        ],
        "notas": (
            "CONFIRMATIONNO debe ser el mismo del Anticipo. "
            "SUPPLEMENT = 'Folio No:{BillNo Anticipo}'. "
            "Reemplaza TODAS las ocurrencias de &lt;SUPPLEMENT&gt; en el XML."
        ),
    },
    "cancelacion": {
        "label": "Cancelación",
        "campos": [
            {
                "id": "ASSOCIATED_BILL_NO",
                "label": "ASSOCIATED_BILL_NO (factura a cancelar)",
                "required": True,
                "placeholder": "Ej: 12345",
                "helper": "BillNumber de la factura que se va a cancelar",
            },
        ],
        "notas": "ASSOCIATED_BILL_NO = BillNumber de la factura que se va a cancelar.",
    },
}


# ─── Funciones de lógica de negocio ───────────────────────────────────────────


def leer_archivo_request(file_path: str) -> dict:
    """
    Lee un archivo JSON o TXT y extrae el primer objeto JSON que encuentre.
    Usa raw_decode para tolerar texto adicional después del JSON (ej. URLs al pie).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"No se encontró el archivo: {file_path}")
    if path.stat().st_size == 0:
        raise ValueError("El archivo está vacío.")

    content = path.read_text(encoding="utf-8", errors="replace")
    # Encontrar el inicio del primer objeto JSON
    start_brace = content.find("{")
    if start_brace == -1:
        raise ValueError("El archivo no contiene un objeto JSON válido.")

    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(content, start_brace)
    return data


def buscar_campo_xml(data: dict) -> str:
    """
    Busca recursivamente el campo 'xml' dentro del JSON.
    Soporta estructura plana y anidada a cualquier profundidad.
    """
    if isinstance(data, dict):
        if "xml" in data:
            return data["xml"]
        for value in data.values():
            if isinstance(value, dict):
                try:
                    return buscar_campo_xml(value)
                except KeyError:
                    continue
    raise KeyError(
        "El campo 'xml' no fue encontrado en el JSON. "
        "Verifica que el archivo tenga un campo 'xml' con contenido Base64."
    )


def decodificar_base64(b64_string: str) -> str:
    """Decodifica una cadena Base64 y la devuelve como texto UTF-8."""
    if not b64_string or not b64_string.strip():
        raise ValueError("El campo 'xml' está vacío o es nulo.")
    try:
        decoded_bytes = base64.b64decode(b64_string)
        return decoded_bytes.decode("utf-8")
    except Exception as exc:
        raise ValueError(f"Error al decodificar Base64: {exc}") from exc


def reemplazar_campo_xml(xml_text: str, campo: str, nuevo_valor: str) -> tuple[str, int]:
    """
    Reemplaza el valor interno de <CAMPO>...</CAMPO> o <CAMPO/> en el XML.
    - Reemplaza TODAS las ocurrencias del campo.
    - Maneja etiquetas auto-cerradas (<CAMPO/>).
    Retorna (xml_modificado, num_reemplazos).
    """
    campo_esc = re.escape(campo)

    # Patrón para <CAMPO>cualquier_cosa</CAMPO>
    pattern_full = rf"(<{campo_esc}>)(.*?)(</{campo_esc}>)"
    nuevo_xml, count = re.subn(
        pattern_full,
        lambda m: f"{m.group(1)}{nuevo_valor}{m.group(3)}",
        xml_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if count > 0:
        return nuevo_xml, count

    # Patrón para <CAMPO/> (auto-cerrado) → expandir a <CAMPO>valor</CAMPO>
    pattern_self = rf"<{campo_esc}\s*/>"
    nuevo_xml2, count2 = re.subn(
        pattern_self,
        f"<{campo}>{nuevo_valor}</{campo}>",
        xml_text,
        flags=re.IGNORECASE,
    )

    if count2 > 0:
        return nuevo_xml2, count2

    raise ValueError(
        f"La etiqueta <{campo}> no existe en el XML. "
        f"Verifica que el request sea del tipo correcto."
    )


def codificar_base64(xml_text: str) -> str:
    """Codifica texto XML de vuelta a Base64."""
    encoded_bytes = base64.b64encode(xml_text.encode("utf-8"))
    return encoded_bytes.decode("ascii")


def reconstruir_json(data: dict, nuevo_b64: str) -> dict:
    """
    Sustituye recursivamente el campo 'xml' en el JSON con el nuevo Base64.
    Soporta estructura plana y anidada.
    """
    nuevo_data = copy.deepcopy(data)

    def _reemplazar(obj) -> bool:
        """Retorna True si encontró y reemplazó el campo 'xml'."""
        if isinstance(obj, dict):
            if "xml" in obj:
                obj["xml"] = nuevo_b64
                return True
            for key in list(obj.keys()):
                if isinstance(obj[key], (dict, list)):
                    if _reemplazar(obj[key]):
                        return True
        elif isinstance(obj, list):
            for item in obj:
                if _reemplazar(item):
                    return True
        return False

    if not _reemplazar(nuevo_data):
        raise KeyError("No se pudo localizar el campo 'xml' para reemplazar en el JSON.")

    return nuevo_data


def extraer_valores_actuales(xml_text: str) -> dict:
    """Extrae los valores actuales de los campos de interés del XML."""
    campos = ["BILLNUMBER", "CONFIRMATIONNO", "SUPPLEMENT", "ASSOCIATED_BILL_NO"]
    valores = {}
    for campo in campos:
        match = re.search(
            rf"<{re.escape(campo)}>(.*?)</{re.escape(campo)}>",
            xml_text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            valores[campo] = match.group(1).strip()
    return valores


def aplicar_modificaciones(
    xml_text: str, tipo: str, valores: dict
) -> tuple[str, list]:
    """
    Aplica todas las modificaciones XML según el tipo de request y los valores dados.
    Retorna (xml_modificado, lista_de_cambios).
    """
    xml_actual = xml_text
    cambios = []
    errores = []

    # Determinar qué campos modificar según el tipo
    if tipo == "factura":
        orden = ["BILLNUMBER", "CONFIRMATIONNO"]
    elif tipo == "anticipo":
        orden = ["BILLNUMBER", "CONFIRMATIONNO"]
    elif tipo == "aplicacion_anticipo":
        orden = ["BILLNUMBER", "CONFIRMATIONNO", "SUPPLEMENT"]
    elif tipo == "cancelacion":
        orden = ["ASSOCIATED_BILL_NO"]
    else:
        raise ValueError(f"Tipo de request desconocido: '{tipo}'")

    for campo in orden:
        valor = valores.get(campo, "").strip()

        # Para campos opcionales en "factura", omitir si vacío
        if not valor:
            if tipo == "factura" and campo == "CONFIRMATIONNO":
                continue
            # Para otros tipos, un campo vacío en la lista de orden es un error
            errores.append(f"El campo '{campo}' está vacío.")
            continue

        try:
            xml_actual, num = reemplazar_campo_xml(xml_actual, campo, valor)
            cambios.append(
                {"campo": campo, "valor": valor, "ocurrencias": num}
            )
        except ValueError as exc:
            errores.append(str(exc))

    if errores:
        raise ValueError(" | ".join(errores))

    return xml_actual, cambios


def construir_supplement(anticipo_billno: str) -> str:
    """Construye el valor de SUPPLEMENT para Aplicación Anticipo."""
    return f"Folio No:{anticipo_billno}"


def enviar_request(request_json: dict, endpoint: str) -> tuple[int, str]:
    """Envía el JSON modificado al endpoint por POST."""
    headers = {"Content-Type": "application/json"}
    resp = req_lib.post(endpoint, json=request_json, headers=headers, timeout=30)
    return resp.status_code, resp.text


# ─── Helpers de seguridad ─────────────────────────────────────────────────────


def validar_ruta(ruta: str) -> Path:
    """Valida y resuelve una ruta de carpeta, evitando path traversal."""
    resolved = Path(ruta).resolve()
    # No permitir rutas relativas que escapan con '..'
    # (resolve() ya maneja esto en Windows)
    return resolved


# ─── Rutas Flask ──────────────────────────────────────────────────────────────


@app.route("/runner")
def runner_index():
    """Sirve la interfaz HTML del runner."""
    return render_template(
        "runner.html",
        tipos_request=TIPOS_REQUEST,
        default_endpoint=DEFAULT_ENDPOINT,
    )


@app.route("/runner/api/tipos", methods=["GET"])
def api_tipos():
    """Devuelve la configuración de todos los tipos de request."""
    return jsonify({"ok": True, "tipos": TIPOS_REQUEST})


@app.route("/runner/api/folder", methods=["POST"])
def api_folder():
    """Lista el contenido de una carpeta (subfolders y archivos JSON/TXT)."""
    try:
        body = request.get_json(force=True)
        folder_path = str(body.get("path", "")).strip()

        if not folder_path:
            return jsonify({"ok": False, "error": "La ruta de carpeta es requerida."}), 400

        resolved = validar_ruta(folder_path)

        if not resolved.exists():
            return jsonify({"ok": False, "error": f"La carpeta no existe: {resolved}"}), 404
        if not resolved.is_dir():
            return jsonify({"ok": False, "error": f"La ruta no es una carpeta: {resolved}"}), 400

        items = []
        for item in sorted(resolved.iterdir()):
            if item.is_dir():
                # Contar archivos válidos dentro del subfolder
                file_count = sum(
                    1 for f in item.iterdir() if f.suffix.lower() in EXTENSIONES_VALIDAS
                )
                items.append(
                    {
                        "name": item.name,
                        "type": "folder",
                        "path": str(item),
                        "count": file_count,
                    }
                )
            elif item.suffix.lower() in EXTENSIONES_VALIDAS:
                items.append(
                    {
                        "name": item.name,
                        "type": "file",
                        "path": str(item),
                        "size": item.stat().st_size,
                    }
                )

        parent = str(resolved.parent) if resolved != resolved.parent else None

        return jsonify(
            {
                "ok": True,
                "path": str(resolved),
                "name": resolved.name,
                "parent": parent,
                "items": items,
                "total": len(items),
            }
        )

    except Exception as exc:
        return jsonify({"ok": False, "error": f"Error al listar carpeta: {exc}"}), 500


@app.route("/runner/api/load", methods=["POST"])
def api_load():
    """Carga un archivo de request: extrae y decodifica el XML."""
    try:
        body = request.get_json(force=True)
        file_path = str(body.get("filePath", "")).strip()

        if not file_path:
            return jsonify({"ok": False, "error": "La ruta del archivo es requerida."}), 400

        # Validar que el archivo esté dentro de una ruta resuelta
        resolved = Path(file_path).resolve()
        if not resolved.exists():
            return jsonify({"ok": False, "error": f"El archivo no existe: {resolved}"}), 404

        data = leer_archivo_request(str(resolved))
        b64_xml = buscar_campo_xml(data)
        xml_decoded = decodificar_base64(b64_xml)
        valores_actuales = extraer_valores_actuales(xml_decoded)

        return jsonify(
            {
                "ok": True,
                "fileName": resolved.name,
                "filePath": str(resolved),
                "xmlDecoded": xml_decoded,
                "currentValues": valores_actuales,
                "message": f"'{resolved.name}' cargado. Valores actuales en XML: {valores_actuales}",
            }
        )

    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except (KeyError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 422
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Error inesperado: {exc}"}), 500


@app.route("/runner/api/process", methods=["POST"])
def api_process():
    """
    Procesa un request:
      1. Lee el archivo
      2. Decodifica el XML del Base64
      3. Aplica las modificaciones según tipo + valores
      4. Re-codifica a Base64
      5. Reconstituye el JSON
    """
    try:
        body = request.get_json(force=True)
        file_path = str(body.get("filePath", "")).strip()
        tipo = str(body.get("tipo", "")).strip()
        valores = body.get("valores", {})

        if not file_path:
            return jsonify({"ok": False, "error": "La ruta del archivo es requerida."}), 400
        if not tipo or tipo not in TIPOS_REQUEST:
            return jsonify({"ok": False, "error": f"Tipo inválido: '{tipo}'."}), 400

        # Construir SUPPLEMENT automáticamente para aplicacion_anticipo
        if tipo == "aplicacion_anticipo":
            anticipo_billno = str(valores.get("_anticipo_billno", "")).strip()
            if anticipo_billno and not str(valores.get("SUPPLEMENT", "")).strip():
                valores["SUPPLEMENT"] = construir_supplement(anticipo_billno)

        # Validar campos requeridos
        tipo_config = TIPOS_REQUEST[tipo]
        for campo_cfg in tipo_config["campos"]:
            if campo_cfg["required"] and campo_cfg["id"] not in ("_anticipo_billno",):
                val = str(valores.get(campo_cfg["id"], "")).strip()
                if not val:
                    return jsonify(
                        {
                            "ok": False,
                            "error": f"El campo '{campo_cfg['label']}' es requerido.",
                        }
                    ), 400

        # Procesar
        resolved = Path(file_path).resolve()
        data = leer_archivo_request(str(resolved))
        b64_xml = buscar_campo_xml(data)
        xml_decoded = decodificar_base64(b64_xml)

        xml_actualizado, cambios = aplicar_modificaciones(xml_decoded, tipo, valores)

        nuevo_b64 = codificar_base64(xml_actualizado)
        json_final = reconstruir_json(data, nuevo_b64)

        return jsonify(
            {
                "ok": True,
                "xmlDecoded": xml_decoded,
                "xmlUpdated": xml_actualizado,
                "requestJson": json_final,
                "cambios": cambios,
                "message": (
                    f"Request procesado correctamente. "
                    f"{len(cambios)} campo(s) modificado(s)."
                ),
            }
        )

    except (FileNotFoundError, KeyError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 422
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Error inesperado: {exc}"}), 500


@app.route("/runner/api/send", methods=["POST"])
def api_send():
    """Envía el JSON modificado al endpoint y retorna status + respuesta."""
    try:
        body = request.get_json(force=True)
        endpoint = str(body.get("endpoint", "")).strip()
        request_json = body.get("requestJson")

        if not endpoint:
            return jsonify({"ok": False, "error": "El endpoint es requerido."}), 400
        if not request_json:
            return jsonify({"ok": False, "error": "El requestJson es requerido."}), 400

        status_code, response_body = enviar_request(request_json, endpoint)

        return jsonify(
            {
                "ok": True,
                "statusCode": status_code,
                "responseBody": response_body,
                "endpointUsed": endpoint,
                "message": f"Request enviado. Status: {status_code}",
            }
        )

    except req_lib.exceptions.ConnectionError:
        return jsonify(
            {
                "ok": False,
                "error": "No se pudo conectar al endpoint. Verifica la URL y la red.",
            }
        ), 502
    except req_lib.exceptions.Timeout:
        return jsonify(
            {"ok": False, "error": "El endpoint no respondió en 30 segundos."}
        ), 504
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Error al enviar: {exc}"}), 500


@app.route("/runner/api/download", methods=["POST"])
def api_download():
    """Devuelve el JSON modificado como descarga de archivo."""
    try:
        body = request.get_json(force=True)
        request_json = body.get("requestJson")
        filename_raw = str(body.get("filename", "request-modificado.json"))

        if not request_json:
            return jsonify({"ok": False, "error": "El requestJson es requerido."}), 400

        json_str = json.dumps(request_json, ensure_ascii=False, indent=2)
        # Sanitizar nombre de archivo
        safe_filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename_raw)

        return Response(
            json_str,
            mimetype="application/json",
            headers={
                "Content-Disposition": f"attachment; filename={safe_filename}"
            },
        )

    except Exception as exc:
        return jsonify({"ok": False, "error": f"Error al generar descarga: {exc}"}), 500


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 62)
    print("  Melia QA Runner — Multi-Request Automation App")
    print("  Servidor iniciado en: http://127.0.0.1:5001")
    print("  Abrir en navegador:   http://127.0.0.1:5001/runner")
    print("  Presiona Ctrl+C para detener")
    print("=" * 62)
    app.run(debug=True, host="127.0.0.1", port=5001)
