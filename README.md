# Go2Facto QA Runner

Herramienta web para ejecutar pruebas de integración contra el API de **Go2Facto** (`opera-go2facto.suite-nt.com`). Permite lanzar flujos de facturación de forma individual o en secuencia completa, visualizar los resultados en tiempo real y descargar la matriz de pruebas en CSV.

---

## Requisitos previos

| Herramienta | Versión mínima |
|---|---|
| Python | 3.10+ |
| pip | — |

> Se recomienda usar un **entorno virtual** (venv o conda).

---

## Instalación

```bash
# 1. Clonar / descargar el repositorio
cd Go2Facto

# 2. Crear y activar un entorno virtual (opcional pero recomendado)
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Instalar dependencias
pip install flask requests
```

---

## Estructura del proyecto

```
Go2Facto/
├── runner.py          # Backend Flask (puerto 5001)
├── templates/
│   └── runner.html    # Dashboard web
└── rqst/              # Archivos de request (XML)
    ├── Factura.txt              → Flujo 1 y Flujo 4 paso 1
    ├── RQ PPDa.txt              → Flujo 2
    ├── Anticipo 1.txt           → Flujo 3 paso 1
    ├── Aplicacion de anticipo 1.txt  → Flujo 3 paso 2
    └── RQ CANCEL G2F 1.txt     → Flujo 4 paso 2
```

> Los archivos dentro de `rqst/` deben contener **XML crudo** (con extensión `.txt`, `.log` o `.json`).  
> No renombres ni elimines ninguno de estos archivos, ya que cada flujo los referencia por nombre.

---

## Ejecución del servidor

```bash
python runner.py
```

El servidor arranca en `http://127.0.0.1:5001/runner`.  
Abre esa URL en el navegador.

---

## Uso del dashboard

### 1. Configurar el endpoint

En la barra lateral izquierda, el campo **Endpoint** ya viene precargado con la URL de producción:

```
https://opera-go2facto.suite-nt.com/api/v1/operamx-generico/ticket40
```

Cámbialo si necesitas apuntar a un ambiente distinto (QA, staging, etc.).

---

### 2. Seleccionar archivo de request

El selector **Archivo de request** lista todos los archivos disponibles en la carpeta `rqst/`.  
Elige el archivo que corresponda al flujo que vas a probar (o déjalo en cualquiera si vas a usar "Todos los flujos").

---

### 3. Flujos disponibles

| Botón | Descripción | Campos requeridos |
|---|---|---|
| **Flujo 1 · Factura Ingreso** | Emite una factura de ingreso | `BILLNUMBER` |
| **Flujo 2 · Factura PPD** | Emite una factura PPD | `BILLNUMBER` |
| **Flujo 3 · Anticipo + Aplicación** | 2 pasos: registra anticipo y luego lo aplica | `BILLNUMBER`, `CONFIRMATIONNO` |
| **Flujo 4 · Factura + Cancelación** | 2 pasos: emite factura y luego la cancela | `BILLNUMBER` |
| **Todos los flujos** | Ejecuta Flujo 1 → 2 → 3 → 4 en secuencia | `BILLNUMBER` (base), `CONFIRMATIONNO` |

---

### 4. Ingresar los campos

Después de seleccionar un flujo, aparecerán los campos editables en la barra lateral:

- **BILLNUMBER** — número de folio de la cuenta en el PMS (debe existir en el sistema).
- **CONFIRMATIONNO** — número de confirmación de la reserva (requerido para Flujo 3 y modo "Todos").

#### Campos automáticos (no requieren entrada manual):

| Campo | Se calcula como |
|---|---|
| `CONFIRMATIONNO` (Aplicación Anticipo) | Se copia del paso 1 (Anticipo) |
| `SUPPLEMENT` (Aplicación Anticipo) | `Folio No:{BILLNUMBER del Anticipo}` |
| `BILLNUMBER` (pasos 2 en adelante) | Asignado por el contador global |
| `ASSOCIATED_BILL_NO` (Cancelación) | BILLNUMBER de la Factura Ingreso (paso 1) |

---

### 5. Contador global de BILLNUMBER ("Todos los flujos")

Cuando se ejecuta **Todos los flujos**, el runner asigna automáticamente BILLNUMBERs consecutivos a cada paso:

| Paso | Flujo | BILLNUMBER asignado |
|---|---|---|
| 1 | Flujo 1 — Factura Ingreso | Base (ingresado) |
| 2 | Flujo 2 — Factura PPD | Base + 1 |
| 3 | Flujo 3 — Anticipo | Base + 2 |
| 4 | Flujo 3 — Aplicación Anticipo | Base + 3 |
| 5 | Flujo 4 — Factura Ingreso | Base + 4 |
| 6 | Flujo 4 — Cancelación | Base + 5 |

> **Importante:** los 6 BILLNUMBERs deben estar registrados y activos en el PMS antes de ejecutar.

---

### 6. Ejecutar y revisar resultados

1. Haz clic en **▶ Ejecutar**.
2. El panel derecho mostrará el **XML original** y el **XML actualizado** con los valores sustituidos.
3. La tabla de resultados registra:
   - Flujo y paso
   - Archivo de request usado
   - Campos modificados con sus valores
   - UUID generado (click para copiar)
   - Código HTTP de respuesta
   - Resultado: `PASS` / `FAIL` / `ERROR` / `OMITIDO`
   - Mensaje de respuesta
   - Timestamp

4. Las tarjetas de estadísticas y las gráficas (dona + barras) se actualizan automáticamente.

---

### 7. Incremento automático de BILLNUMBER

Al finalizar cada ejecución, todos los campos `BILLNUMBER` del formulario se incrementan en **+1** automáticamente para facilitar la siguiente ronda de pruebas.

---

### 8. Descargar matriz de pruebas

Haz clic en **⬇ Descargar Matriz de Pruebas (.csv)** para exportar todos los resultados en un archivo CSV compatible con Excel (incluye BOM UTF-8).

Columnas exportadas: `Flujo`, `Paso`, `Archivo`, `Campos Modificados`, `UUID Generado`, `HTTP`, `Resultado`, `Mensaje`, `Timestamp`.

---

## Errores comunes

### Error de negocio del servidor (no es bug del runner)

```json
{
  "mensaje": "Ocurrio un error al tratar de comunicarse con el recurso...",
  "detalle": "No se encontraron anticipos previos para la integración JLO..."
}
```

**Causa:** El `CONFIRMATIONNO` ingresado no tiene un anticipo previo registrado en el PMS.  
**Solución:** Ejecuta primero el **Flujo 3 paso 1 (Anticipo)** con `PASS` y luego ejecuta la Aplicación.

---

### La carpeta `rqst/` no existe

```
FileNotFoundError: La carpeta 'rqst' no existe en: ...
```

**Solución:** Crea la carpeta `rqst/` en la raíz del proyecto y coloca los archivos XML de request.

---

### Puerto 5001 ocupado

```
OSError: [Errno 98] Address already in use
```

**Solución (Windows):**
```powershell
netstat -ano | findstr :5001
# Anota el PID y termínalo:
taskkill /PID <PID> /F
```

---

## Notas de seguridad

- El servidor solo acepta archivos de la carpeta `rqst/` (protegido contra path traversal).
- No exponer el servidor en redes públicas — está pensado para uso local o red interna.
