"""
Go2Facto - Automation App v2
Backend Flask multi-tipo: Factura, Anticipo, Aplicación de Anticipo, Cancelación.
Cada tipo modifica campos XML específicos dentro del campo 'xml' codificado en Base64.
"""

import base64
import copy
import json
import os
import re

import requests
from flask import Flask, jsonify, render_template, request, Response

# ─── Configuración global ─────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(24)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENDPOINT = "https://opera-go2facto.suite-nt.com/api/v1/operamx-generico/ticket40"

# ─── Mapeo centralizado por tipo de request ───────────────────────────────────
# Cada entrada define:
#   label        → nombre legible para la UI
#   campos       → lista de campos editables con su config
#     campo      → nombre del nodo XML (tag)
#     label      → etiqueta para mostrar al usuario
#     placeholder→ ejemplo de valor
#     prefill    → si True, pre-llenar con el valor actual del XML
#     valor_fijo → si presente, pre-llenar con este valor (editable)
#     requerido  → si True, validar que no esté vacío antes de procesar
REQUEST_TYPES = {
    "factura": {
        "label": "Factura",
        "campos": [
            {
                "campo": "BILLNUMBER",
                "label": "BillNumber",
                "placeholder": "Ej: 5567",
                "prefill": True,
                "requerido": True,
            },
            {
                "campo": "CONFIRMATIONNO",
                "label": "Confirmation No",
                "placeholder": "Ej: 55150",
                "prefill": True,
                "requerido": False,
            },
        ],
    },
    "anticipo": {
        "label": "Anticipo",
        "campos": [
            {
                "campo": "BILLNUMBER",
                "label": "BillNumber",
                "placeholder": "Ej: 5567",
                "prefill": True,
                "requerido": True,
            },
            {
                "campo": "CONFIRMATIONNO",
                "label": "Confirmation No",
                "placeholder": "Ej: 55150",
                "prefill": True,
                "requerido": False,
            },
        ],
    },
    "aplicacion_anticipo": {
        "label": "Aplicación de Anticipo",
        "campos": [
            {
                "campo": "BILLNUMBER",
                "label": "BillNumber (mismo que el Anticipo)",
                "placeholder": "Ej: 5567",
                "prefill": True,
                "requerido": True,
            },
            {
                "campo": "CONFIRMATIONNO",
                "label": "Confirmation No (mismo que el Anticipo)",
                "placeholder": "Ej: 55150",
                "prefill": True,
                "requerido": False,
            },
            {
                "campo": "SUPPLEMENT",
                "label": "Supplement",
                "placeholder": "del deposit transfer at check-in",
                "valor_fijo": "del deposit transfer at check-in",
                "requerido": True,
            },
        ],
    },
    "cancelacion": {
        "label": "Cancelación",
        "campos": [
            {
                "campo": "ASSOCIATED_BILL_NO",
                "label": "Associated Bill No (folio a cancelar)",
                "placeholder": "Ej: FA_C_RFC5518",
                "prefill": True,
                "requerido": True,
            },
        ],
    },
}


# ─── Funciones de I/O ─────────────────────────────────────────────────────────

def listar_json_files(carpeta: str) -> list[str]:
    """Lista todos los archivos .json en la carpeta dada."""
    if not os.path.isdir(carpeta):
        raise FileNotFoundError(f"La carpeta no existe: {carpeta}")
    return sorted(
        f for f in os.listdir(carpeta) if f.lower().endswith(".json")
    )


def leer_json_request(carpeta: str, nombre_archivo: str) -> dict:
    """
    Lee un archivo JSON de la carpeta indicada.
    Usa raw_decode para tolerar texto adicional al final del archivo.
    """
    ruta = os.path.join(carpeta, nombre_archivo)
    if not os.path.exists(ruta):
        raise FileNotFoundError(f"No se encontró el archivo: {nombre_archivo}")
    with open(ruta, "r", encoding="utf-8") as f:
        contenido = f.read()
    start = contenido.index("{")
    data, _ = json.JSONDecoder().raw_decode(contenido, start)
    return data


# ─── Funciones XML / Base64 ───────────────────────────────────────────────────

def extraer_campo_xml(data: dict) -> str:
    """
    Extrae el campo 'xml' del JSON.
    Soporta estructura plana y un nivel de anidamiento.
    """
    if "xml" in data:
        return data["xml"]
    for val in data.values():
        if isinstance(val, dict) and "xml" in val:
            return val["xml"]
    raise KeyError("El campo 'xml' no existe en el JSON.")


def decodificar_base64(b64_string: str) -> str:
    """Decodifica Base64 a texto UTF-8."""
    try:
        return base64.b64decode(b64_string).decode("utf-8")
    except Exception as e:
        raise ValueError(f"Error al decodificar Base64: {e}")


def codificar_base64(xml_text: str) -> str:
    """Codifica texto a Base64 ASCII."""
    return base64.b64encode(xml_text.encode("utf-8")).decode("ascii")


def extraer_valor_nodo(xml_text: str, tag: str) -> str | None:
    """Extrae el valor actual de un nodo XML por su tag. Devuelve None si no existe."""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", xml_text, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def nodo_existe(xml_text: str, tag: str) -> bool:
    """Verifica si un nodo XML existe (self-closing o con contenido)."""
    return bool(re.search(rf"<{tag}[^>]*/?>", xml_text, re.IGNORECASE))


def reemplazar_nodo(xml_text: str, tag: str, nuevo_valor: str) -> str:
    """
    Reemplaza el contenido de <TAG>...</TAG> con nuevo_valor.
    También maneja tags self-closing <TAG/> convirtiéndolos a <TAG>valor</TAG>.
    Lanza ValueError si el nodo no existe.
    """
    # Verificar existencia
    if not nodo_existe(xml_text, tag):
        raise ValueError(f"El nodo <{tag}> no existe en el XML.")

    # Reemplazar tag con contenido: <TAG>...</TAG>
    pattern_pair = rf"(<{tag}>)(.*?)(</{tag}>)"
    result, n = re.subn(
        pattern_pair,
        lambda m: f"{m.group(1)}{nuevo_valor}{m.group(3)}",
        xml_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if n > 0:
        return result

    # Reemplazar tag self-closing: <TAG/> → <TAG>valor</TAG>
    pattern_self = rf"<{tag}\s*/>"
    result, n = re.subn(
        pattern_self,
        f"<{tag}>{nuevo_valor}</{tag}>",
        xml_text,
        flags=re.IGNORECASE,
    )
    if n > 0:
        return result

    raise ValueError(f"No se pudo reemplazar el nodo <{tag}>.")


def reconstruir_request(data: dict, nuevo_b64: str) -> dict:
    """Devuelve una copia profunda del JSON con el campo 'xml' actualizado."""
    nuevo_data = copy.deepcopy(data)
    if "xml" in nuevo_data:
        nuevo_data["xml"] = nuevo_b64
        return nuevo_data
    for key, val in nuevo_data.items():
        if isinstance(val, dict) and "xml" in val:
            nuevo_data[key]["xml"] = nuevo_b64
            return nuevo_data
    raise KeyError("No se pudo ubicar el campo 'xml' para actualizarlo.")


# ─── Detección automática del tipo de request ─────────────────────────────────

def detectar_tipo(xml_text: str) -> str:
    """
    Intenta inferir el tipo de request a partir del XML:
      - NC_C_RFC en QUEUENAME → cancelacion
      - ARRANGEMENT_DESCRIPTION contiene 'anticipo' → anticipo
      - Por defecto → factura
    """
    queuename = extraer_valor_nodo(xml_text, "QUEUENAME") or ""
    if "NC_" in queuename.upper():
        return "cancelacion"

    arr_desc = extraer_valor_nodo(xml_text, "ARRANGEMENT_DESCRIPTION") or ""
    if "anticipo" in arr_desc.lower():
        return "anticipo"

    return "factura"


# ─── Envío HTTP ───────────────────────────────────────────────────────────────

def enviar_request(payload: dict, endpoint: str) -> tuple[int, str]:
    """Envía el JSON al endpoint por POST y devuelve (status_code, body)."""
    resp = requests.post(
        endpoint,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    return resp.status_code, resp.text


# ─── Rutas API ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", default_endpoint=DEFAULT_ENDPOINT)


@app.route("/api/tipos", methods=["GET"])
def api_tipos():
    """Devuelve la configuración de todos los tipos de request para la UI."""
    return jsonify({
        k: {"label": v["label"], "campos": v["campos"]}
        for k, v in REQUEST_TYPES.items()
    })


@app.route("/api/files", methods=["POST"])
def api_files():
    """Lista los archivos .json en la carpeta indicada."""
    try:
        body = request.get_json(force=True) or {}
        carpeta = body.get("carpeta", BASE_DIR)
        archivos = listar_json_files(carpeta)
        return jsonify({"ok": True, "archivos": archivos, "carpeta": carpeta})
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error al listar archivos: {e}"}), 500


@app.route("/api/load", methods=["POST"])
def api_load():
    """
    Carga un archivo JSON, decodifica el campo xml y extrae los valores actuales
    de todos los campos relevantes. También intenta detectar el tipo de request.
    """
    try:
        body = request.get_json(force=True) or {}
        carpeta  = body.get("carpeta", BASE_DIR)
        filename = body.get("filename", "")

        if not filename:
            return jsonify({"ok": False, "error": "Se requiere el nombre del archivo."}), 400

        data       = leer_json_request(carpeta, filename)
        b64_xml    = extraer_campo_xml(data)
        xml_decoded = decodificar_base64(b64_xml)
        tipo_detectado = detectar_tipo(xml_decoded)

        # Extraer valores actuales de TODOS los posibles campos modificables
        todos_los_campos = {
            tag
            for tipo in REQUEST_TYPES.values()
            for campo in tipo["campos"]
            for tag in [campo["campo"]]
        }
        valores_actuales = {
            tag: extraer_valor_nodo(xml_decoded, tag)
            for tag in todos_los_campos
        }

        return jsonify({
            "ok":              True,
            "xmlDecoded":      xml_decoded,
            "tipoDetectado":   tipo_detectado,
            "valoresActuales": valores_actuales,
            "message":         f"Archivo '{filename}' cargado. Tipo detectado: {REQUEST_TYPES[tipo_detectado]['label']}",
        })

    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except KeyError as e:
        return jsonify({"ok": False, "error": str(e)}), 422
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 422
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error inesperado: {e}"}), 500


@app.route("/api/process", methods=["POST"])
def api_process():
    """
    Aplica los reemplazos de campos XML según el tipo de request.
    Body: { carpeta, filename, tipo, campos: {TAG: valor} }
    """
    try:
        body     = request.get_json(force=True) or {}
        carpeta  = body.get("carpeta", BASE_DIR)
        filename = body.get("filename", "")
        tipo     = body.get("tipo", "")
        campos_ui = body.get("campos", {})

        if not filename:
            return jsonify({"ok": False, "error": "Se requiere el nombre del archivo."}), 400
        if tipo not in REQUEST_TYPES:
            return jsonify({"ok": False, "error": f"Tipo de request desconocido: '{tipo}'."}), 400

        # Validar que los campos requeridos estén completos
        config_tipo = REQUEST_TYPES[tipo]
        errores = []
        for campo_cfg in config_tipo["campos"]:
            tag = campo_cfg["campo"]
            if campo_cfg.get("requerido") and not str(campos_ui.get(tag, "")).strip():
                errores.append(f"El campo '{campo_cfg['label']}' es requerido.")
        if errores:
            return jsonify({"ok": False, "error": " | ".join(errores)}), 400

        # Leer y decodificar
        data        = leer_json_request(carpeta, filename)
        b64_xml     = extraer_campo_xml(data)
        xml_actual  = decodificar_base64(b64_xml)

        # Aplicar reemplazos en orden
        xml_nuevo = xml_actual
        reemplazos_realizados = []
        for campo_cfg in config_tipo["campos"]:
            tag   = campo_cfg["campo"]
            valor = str(campos_ui.get(tag, campo_cfg.get("valor_fijo", ""))).strip()
            if not valor:
                continue  # campo opcional vacío → no tocar
            xml_nuevo = reemplazar_nodo(xml_nuevo, tag, valor)
            reemplazos_realizados.append(f"<{tag}> → '{valor}'")

        # Re-codificar y reconstruir
        nuevo_b64  = codificar_base64(xml_nuevo)
        json_final = reconstruir_request(data, nuevo_b64)

        return jsonify({
            "ok":                  True,
            "xmlUpdated":          xml_nuevo,
            "requestJson":         json_final,
            "reemplazosRealizados": reemplazos_realizados,
            "message": (
                f"[{config_tipo['label']}] {len(reemplazos_realizados)} campo(s) reemplazado(s): "
                + ", ".join(reemplazos_realizados)
            ),
        })

    except (KeyError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 422
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error inesperado: {e}"}), 500


@app.route("/api/send", methods=["POST"])
def api_send():
    """Envía el JSON modificado al endpoint y devuelve el resultado."""
    try:
        body         = request.get_json(force=True) or {}
        endpoint     = str(body.get("endpoint", "")).strip()
        request_json = body.get("requestJson")

        if not endpoint:
            return jsonify({"ok": False, "error": "El endpoint es requerido."}), 400
        if not request_json:
            return jsonify({"ok": False, "error": "El requestJson es requerido."}), 400

        status_code, response_body = enviar_request(request_json, endpoint)

        return jsonify({
            "ok":           True,
            "statusCode":   status_code,
            "responseBody": response_body,
            "endpointUsed": endpoint,
            "message":      f"Request enviado. Status: {status_code}",
        })

    except requests.exceptions.ConnectionError:
        return jsonify({"ok": False, "error": "No se pudo conectar al endpoint. Verifique la URL y la red."}), 502
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "error": "El endpoint no respondió en el tiempo límite (30s)."}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error al enviar: {e}"}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    """Devuelve el JSON modificado como descarga de archivo."""
    try:
        body         = request.get_json(force=True) or {}
        request_json = body.get("requestJson")
        filename     = body.get("filename", "request-modificado.json")

        if not request_json:
            return jsonify({"ok": False, "error": "El requestJson es requerido."}), 400

        json_str = json.dumps(request_json, ensure_ascii=False, indent=4)
        dl_name  = f"mod_{filename}" if not filename.startswith("mod_") else filename

        return Response(
            json_str,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={dl_name}"},
        )

    except Exception as e:
        return jsonify({"ok": False, "error": f"Error al generar descarga: {e}"}), 500


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Go2Facto Automation App v2")
    print("  Servidor iniciado en: http://127.0.0.1:5000")
    print("  Presiona Ctrl+C para detener")
    print("=" * 60)
    app.run(debug=True, host="127.0.0.1", port=5000)
