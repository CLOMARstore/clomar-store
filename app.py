import os
import io
import zipfile
import hashlib
import html
import urllib.parse
import urllib.request
import secrets
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

APP_VERSION = "V24.9 Catálogo Clientes + PDF"
APP_NAME = "Clomar Store"

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================
st.set_page_config(
    page_title="Clomar Store",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def get_secret(name: str, default=None):
    """Lee secrets de Streamlit Cloud o variables de entorno locales."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)


def database_url():
    # En Streamlit Cloud se configurará como secret: NEON_DATABASE_URL="postgresql://..."
    return get_secret("NEON_DATABASE_URL") or get_secret("DATABASE_URL") or "sqlite:///clomar_store_cloud_local.db"


@st.cache_resource(show_spinner=False)
def get_engine(url: str):
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 60}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


ENGINE = get_engine(database_url())
IS_POSTGRES = database_url().startswith("postgresql")
IS_LOCAL_SQLITE = database_url().startswith("sqlite")


def sql_id():
    return "BIGSERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def exec_sql(sql: str, params: dict | None = None):
    params = params or {}
    with ENGINE.begin() as conn:
        return conn.execute(text(sql), params)


def query_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    params = params or {}
    with ENGINE.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def scalar(sql: str, params: dict | None = None, default=0):
    params = params or {}
    with ENGINE.connect() as conn:
        value = conn.execute(text(sql), params).scalar()
    return default if value is None else value


# ============================================================
# SEGURIDAD
# ============================================================
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt.encode(), 120_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    stored_hash = str(stored_hash)
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, salt, digest = stored_hash.split("$", 2)
            candidate = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt.encode(), 120_000).hex()
            return secrets.compare_digest(candidate, digest)
        except Exception:
            return False
    # Soporte para hashes SHA256 antiguos.
    legacy = hashlib.sha256(str(password).encode("utf-8")).hexdigest()
    return secrets.compare_digest(legacy, stored_hash)


def money(x) -> str:
    try:
        return f"S/ {float(x or 0):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    except Exception:
        return "S/ 0,00"


def num(x) -> str:
    try:
        return f"{float(x or 0):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    except Exception:
        return "0,00"


# ============================================================
# ESQUEMA CLOUD
# ============================================================
def init_db():
    """Crea una estructura PostgreSQL/SQLite limpia para operación en nube."""
    ddl = [
        f"""
        CREATE TABLE IF NOT EXISTS usuarios (
            id_usuario {sql_id()},
            usuario VARCHAR(80) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            nombre VARCHAR(160) NOT NULL,
            rol VARCHAR(40) NOT NULL DEFAULT 'Vendedor',
            estado VARCHAR(30) NOT NULL DEFAULT 'Activo',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS categorias (
            id_categoria {sql_id()},
            nombre_categoria VARCHAR(160) UNIQUE NOT NULL,
            descripcion TEXT DEFAULT '',
            estado VARCHAR(30) DEFAULT 'Activo',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS clientes (
            id_cliente {sql_id()},
            nombre_cliente VARCHAR(200) NOT NULL,
            telefono VARCHAR(80) DEFAULT '',
            documento VARCHAR(80) DEFAULT '',
            direccion TEXT DEFAULT '',
            observacion TEXT DEFAULT '',
            limite_credito NUMERIC(12,2) DEFAULT 0,
            estado VARCHAR(30) DEFAULT 'Activo',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS proveedores (
            id_proveedor {sql_id()},
            nombre_proveedor VARCHAR(200) NOT NULL,
            telefono VARCHAR(80) DEFAULT '',
            documento VARCHAR(80) DEFAULT '',
            direccion TEXT DEFAULT '',
            estado VARCHAR(30) DEFAULT 'Activo',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS productos (
            id_producto {sql_id()},
            codigo VARCHAR(80) UNIQUE,
            nombre_producto VARCHAR(240) NOT NULL,
            id_categoria INTEGER,
            marca VARCHAR(120) DEFAULT '',
            unidad VARCHAR(60) DEFAULT 'Unidad',
            costo_unitario NUMERIC(12,2) DEFAULT 0,
            precio_venta NUMERIC(12,2) DEFAULT 0,
            stock_minimo NUMERIC(12,2) DEFAULT 0,
            imagen_url TEXT DEFAULT '',
            estado VARCHAR(30) DEFAULT 'Activo',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS ventas (
            id_venta {sql_id()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            comprobante VARCHAR(80) UNIQUE,
            id_cliente INTEGER,
            id_usuario INTEGER,
            vendedor_nombre VARCHAR(160),
            metodo_pago VARCHAR(60),
            total_venta NUMERIC(12,2) DEFAULT 0,
            monto_pagado NUMERIC(12,2) DEFAULT 0,
            saldo_pendiente NUMERIC(12,2) DEFAULT 0,
            estado_pago VARCHAR(30) DEFAULT 'Pagada',
            observacion TEXT DEFAULT '',
            anulada INTEGER DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS detalle_ventas (
            id_detalle {sql_id()},
            id_venta INTEGER NOT NULL,
            id_producto INTEGER,
            producto_nombre VARCHAR(240),
            cantidad NUMERIC(12,2) DEFAULT 0,
            precio_unitario NUMERIC(12,2) DEFAULT 0,
            costo_unitario NUMERIC(12,2) DEFAULT 0,
            subtotal NUMERIC(12,2) DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS movimientos_stock (
            id_movimiento {sql_id()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            id_producto INTEGER NOT NULL,
            tipo VARCHAR(50) NOT NULL,
            cantidad NUMERIC(12,2) DEFAULT 0,
            costo_unitario NUMERIC(12,2) DEFAULT 0,
            referencia VARCHAR(120) DEFAULT '',
            id_usuario INTEGER,
            observacion TEXT DEFAULT ''
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS compras (
            id_compra {sql_id()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            proveedor VARCHAR(200) DEFAULT '',
            metodo_pago VARCHAR(60) DEFAULT '',
            total_compra NUMERIC(12,2) DEFAULT 0,
            monto_pagado NUMERIC(12,2) DEFAULT 0,
            saldo_pendiente NUMERIC(12,2) DEFAULT 0,
            estado_pago VARCHAR(30) DEFAULT 'Pagada',
            id_usuario INTEGER,
            observacion TEXT DEFAULT ''
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS detalle_compras (
            id_detalle_compra {sql_id()},
            id_compra INTEGER NOT NULL,
            id_producto INTEGER,
            cantidad NUMERIC(12,2) DEFAULT 0,
            costo_unitario NUMERIC(12,2) DEFAULT 0,
            subtotal NUMERIC(12,2) DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS caja (
            id_caja {sql_id()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tipo VARCHAR(30) NOT NULL,
            concepto TEXT NOT NULL,
            metodo_pago VARCHAR(60) DEFAULT '',
            monto NUMERIC(12,2) DEFAULT 0,
            referencia VARCHAR(120) DEFAULT '',
            id_usuario INTEGER,
            observacion TEXT DEFAULT '',
            anulada INTEGER DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS cierres_caja (
            id_cierre {sql_id()},
            fecha DATE,
            hora VARCHAR(20),
            id_usuario INTEGER,
            monto_inicial NUMERIC(12,2) DEFAULT 0,
            efectivo_esperado NUMERIC(12,2) DEFAULT 0,
            efectivo_contado NUMERIC(12,2) DEFAULT 0,
            diferencia NUMERIC(12,2) DEFAULT 0,
            observacion TEXT DEFAULT '',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS egresos (
            id_egreso {sql_id()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            categoria_gasto VARCHAR(120),
            concepto TEXT,
            proveedor VARCHAR(200) DEFAULT '',
            monto NUMERIC(12,2) DEFAULT 0,
            metodo_pago VARCHAR(60) DEFAULT '',
            estado_pago VARCHAR(30) DEFAULT 'Pagada',
            monto_pagado NUMERIC(12,2) DEFAULT 0,
            saldo_pendiente NUMERIC(12,2) DEFAULT 0,
            id_usuario INTEGER,
            observacion TEXT DEFAULT ''
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS configuracion_tienda (
            clave VARCHAR(120) PRIMARY KEY,
            valor TEXT DEFAULT '',
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ]
    for statement in ddl:
        exec_sql(statement)

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_productos_nombre ON productos (nombre_producto)",
        "CREATE INDEX IF NOT EXISTS idx_productos_codigo ON productos (codigo)",
        "CREATE INDEX IF NOT EXISTS idx_ventas_fecha ON ventas (fecha)",
        "CREATE INDEX IF NOT EXISTS idx_detalle_ventas_producto ON detalle_ventas (id_producto)",
        "CREATE INDEX IF NOT EXISTS idx_mov_stock_producto ON movimientos_stock (id_producto)",
        "CREATE INDEX IF NOT EXISTS idx_caja_fecha ON caja (fecha)",
    ]
    for idx in indexes:
        try:
            exec_sql(idx)
        except Exception:
            pass

    # Migraciones suaves para bases ya creadas en versiones anteriores.
    migration_statements = []
    if IS_POSTGRES:
        migration_statements = [
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS descripcion TEXT DEFAULT ''",
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS imagen_url TEXT DEFAULT ''",
        ]
    else:
        migration_statements = [
            "ALTER TABLE productos ADD COLUMN descripcion TEXT DEFAULT ''",
            "ALTER TABLE productos ADD COLUMN imagen_url TEXT DEFAULT ''",
        ]
    for mig in migration_statements:
        try:
            exec_sql(mig)
        except Exception:
            pass

    ensure_default_data()


def ensure_default_data():
    # Usuarios base sin resetear claves si ya existen.
    defaults = [
        ("admin", "admin123", "Administrador", "Administrador"),
        ("vendedor", "venta123", "Vendedor tienda", "Vendedor"),
    ]
    for usuario, clave, nombre, rol in defaults:
        exists = scalar("SELECT COUNT(*) FROM usuarios WHERE usuario=:u", {"u": usuario})
        if int(exists or 0) == 0:
            exec_sql(
                """INSERT INTO usuarios (usuario, password_hash, nombre, rol, estado)
                   VALUES (:usuario, :password_hash, :nombre, :rol, 'Activo')""",
                {"usuario": usuario, "password_hash": hash_password(clave), "nombre": nombre, "rol": rol},
            )

    base_cats = ["Perfumes", "Cremas", "Ropa", "Accesorios", "Calzado", "Hogar", "Tecnología", "Regalos"]
    for cat in base_cats:
        exists = scalar("SELECT COUNT(*) FROM categorias WHERE lower(nombre_categoria)=lower(:c)", {"c": cat})
        if int(exists or 0) == 0:
            exec_sql(
                "INSERT INTO categorias (nombre_categoria, descripcion, estado) VALUES (:c, '', 'Activo')",
                {"c": cat},
            )


@st.cache_resource(show_spinner=False)
def bootstrap_database_once(db_url: str):
    """Inicializa la base una sola vez por arranque para reducir recargas lentas."""
    init_db()
    return True


def clear_app_cache():
    try:
        get_store_config.clear()
    except Exception:
        pass


try:
    bootstrap_database_once(database_url())
    DB_OK = True
    DB_ERROR = ""
except Exception as e:
    DB_OK = False
    DB_ERROR = str(e)


# ============================================================
# ESTILOS
# ============================================================
st.markdown(
    """
<style>
:root{
  --bg:#f6f7fb; --panel:#ffffff; --ink:#111827; --muted:#667085;
  --brand:#111827; --brand2:#1f2937; --accent:#ef4444; --green:#16a34a; --red:#dc2626; --line:#e5e7eb;
}
.stApp{background:var(--bg)!important;color:var(--ink)!important;}
.block-container{padding-top:.75rem!important;padding-bottom:2rem!important;max-width:1420px!important;}
[data-testid="stHeader"]{background:#0b0f17!important;}
[data-testid="stSidebar"]{background:#fff!important;border-right:1px solid var(--line)!important;}
[data-testid="stSidebar"] *{color:#1f2937!important;}
[data-testid="stSidebar"] button{background:#111827!important;color:#fff!important;border-radius:14px!important;border:1px solid #111827!important;font-weight:900!important;}

h1,h2,h3,h4{color:#0f172a!important;letter-spacing:-.025em;}
.clomar-hero{background:linear-gradient(135deg,#111827,#1f2937);color:#fff;border-radius:22px;padding:22px 26px;box-shadow:0 16px 38px rgba(15,23,42,.15);margin-bottom:14px;}
.clomar-hero h1{color:#fff!important;margin:0;font-size:32px;font-weight:950}.clomar-hero p{color:#e5e7eb!important;margin:.45rem 0 0;font-size:15px}

.kpi-card{background:#fff;border:1px solid var(--line);border-radius:20px;padding:17px;box-shadow:0 10px 25px rgba(15,23,42,.055);min-height:108px;}
.kpi-label{font-size:12px;color:#667085;font-weight:800;text-transform:uppercase;letter-spacing:.04em}.kpi-value{font-size:29px;font-weight:950;color:#0f172a;margin-top:5px}.kpi-sub{font-size:13px;color:#667085;margin-top:4px}
.card{background:#fff;border:1px solid var(--line);border-radius:20px;padding:18px;box-shadow:0 10px 26px rgba(15,23,42,.055);}
.product-card{background:#fff;border:1px solid #e7eaf0;border-radius:20px;padding:16px;box-shadow:0 8px 20px rgba(15,23,42,.055);height:100%;}
.catalog-card{background:#fff;border:1px solid #e7eaf0;border-radius:22px;padding:16px;box-shadow:0 10px 24px rgba(15,23,42,.06);min-height:320px;}
.catalog-img{width:100%;height:170px;object-fit:cover;border-radius:16px;border:1px solid #e5e7eb;background:#f8fafc;margin-bottom:12px;}
.catalog-empty-img{height:170px;border-radius:16px;background:linear-gradient(135deg,#fff7ed,#fdf2f8);display:flex;align-items:center;justify-content:center;font-size:54px;border:1px solid #f2e8e8;margin-bottom:12px;}
.public-hero{background:linear-gradient(135deg,#fff,#fff7fb);border:1px solid #f1e4e8;border-radius:26px;padding:24px;box-shadow:0 16px 35px rgba(15,23,42,.06);margin-bottom:16px;}
.public-hero h1{margin:0;color:#111827!important;font-size:34px;}
.public-hero p{color:#667085;margin:.35rem 0 0;}
.product-name{font-size:17px;font-weight:900;color:#101828;min-height:44px}.product-price{font-size:26px;font-weight:950;color:#111827;margin-top:8px}.product-meta{font-size:13px;color:#667085}.chip{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800;border:1px solid #e5e7eb;background:#f9fafb;color:#344054;margin:3px 4px 3px 0}.chip-ok{background:#ecfdf3;color:#027a48;border-color:#abefc6}.chip-warn{background:#fffaeb;color:#b54708;border-color:#fedf89}.chip-red{background:#fef3f2;color:#b42318;border-color:#fecdca}.chip-dark{background:#111827;color:#fff;border-color:#111827}

.clean-table{width:100%;border-collapse:separate;border-spacing:0;background:#fff;border:1px solid #e5e7eb;border-radius:18px;overflow:hidden;box-shadow:0 8px 22px rgba(15,23,42,.045)}.clean-table th{background:#f8fafc;color:#475467;text-align:left;padding:12px 14px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid #e5e7eb}.clean-table td{padding:12px 14px;border-bottom:1px solid #f0f2f5;color:#111827;font-size:14px}.clean-table tr:hover td{background:#f9fafb}.clean-table tr:last-child td{border-bottom:none}

/* Campos claros y legibles */
label,[data-testid="stWidgetLabel"],[data-testid="stWidgetLabel"] p{color:#111827!important;font-weight:850!important;opacity:1!important;}
[data-testid="stTextInput"] input,[data-testid="stNumberInput"] input,[data-testid="stTextArea"] textarea,[data-testid="stDateInput"] input{background:#fff!important;color:#111827!important;border:1px solid #cbd5e1!important;border-radius:12px!important;box-shadow:none!important;}
[data-testid="stTextInput"] input::placeholder,[data-testid="stTextArea"] textarea::placeholder{color:#98a2b3!important;opacity:1!important;}
[data-baseweb="select"] > div{background:#fff!important;color:#111827!important;border-color:#cbd5e1!important;}
[data-baseweb="select"] span{color:#111827!important;}
[data-testid="stForm"]{background:#fff!important;border:1px solid #e5e7eb!important;border-radius:20px!important;padding:18px!important;box-shadow:0 10px 24px rgba(15,23,42,.045)!important;}

.stButton > button,.stDownloadButton > button,button[kind="primary"]{border-radius:13px!important;font-weight:900!important;min-height:42px!important;}
button[kind="primary"],.stButton > button[kind="primary"]{background:#111827!important;color:#fff!important;border:1px solid #111827!important;}
.stButton > button:not([kind="primary"]){background:#fff!important;color:#111827!important;border:1px solid #d0d5dd!important;}
.stButton > button:disabled{background:#f2f4f7!important;color:#98a2b3!important;border:1px solid #e5e7eb!important;}
[data-testid="stAlert"]{border-radius:14px!important;}[data-testid="stAlert"] *{color:#111827!important;font-weight:700!important;}
button[data-baseweb="tab"]{color:#344054!important;font-weight:850!important;}button[data-baseweb="tab"][aria-selected="true"]{color:#ef4444!important;border-bottom-color:#ef4444!important;}
.js-plotly-plot,.plot-container{background:#fff!important;border-radius:18px!important;}
.success-panel{background:#fff;border:1px solid #d0d5dd;border-radius:26px;padding:26px;text-align:center;box-shadow:0 20px 42px rgba(15,23,42,.13);}.success-icon{font-size:50px;color:#16a34a}.success-title{font-size:28px;font-weight:950;color:#111827}.success-sub{color:#667085;font-size:15px;margin-top:6px}.receipt{background:white;border:1px solid #d0d5dd;border-radius:20px;padding:22px;max-width:440px;margin:auto;color:#111827}.receipt h2{text-align:center;margin:0}.receipt-line{display:flex;justify-content:space-between;border-bottom:1px dashed #d0d5dd;padding:8px 0;font-size:14px}.receipt-total{font-size:24px;font-weight:950;text-align:right;margin-top:12px}.no-print{}
@media print{body *{visibility:hidden!important;}#printable-receipt,#printable-receipt *{visibility:visible!important;}#printable-receipt{position:absolute;left:0;top:0;width:100%;}.no-print{display:none!important;}}
@media(max-width:900px){.block-container{padding-left:1rem!important;padding-right:1rem!important}.clomar-hero h1{font-size:26px}.kpi-value{font-size:24px}}
</style>
""",
    unsafe_allow_html=True,
)

def kpi(label, value, sub=""):
    st.markdown(f"""
    <div class="kpi-card"><div class="kpi-label">{label}</div><div class="kpi-value">{value}</div><div class="kpi-sub">{sub}</div></div>
    """, unsafe_allow_html=True)


def html_table(df: pd.DataFrame, columns=None, headers=None, max_rows=100):
    if df is None or df.empty:
        st.info("No hay datos para mostrar.")
        return
    columns = columns or list(df.columns)
    headers = headers or columns
    rows = []
    safe_df = df.copy().head(max_rows)
    for _, row in safe_df.iterrows():
        cells = "".join([f"<td>{html.escape(str(row.get(c, '')))}</td>" for c in columns])
        rows.append(f"<tr>{cells}</tr>")
    th = "".join([f"<th>{html.escape(str(h))}</th>" for h in headers])
    st.markdown(f"<table class='clean-table'><thead><tr>{th}</tr></thead><tbody>{''.join(rows)}</tbody></table>", unsafe_allow_html=True)


@st.cache_data(ttl=60, show_spinner=False)
def get_store_config() -> dict:
    """Devuelve configuración visual/comercial de la tienda."""
    defaults = {
        "nombre_tienda": APP_NAME,
        "logo_url": "",
        "telefono": "",
        "direccion": "",
        "mensaje_comprobante": "Gracias por su compra.",
        "color_principal": "#111827",
        "catalogo_url_base": get_secret("PUBLIC_APP_URL", "https://clomar-store.streamlit.app"),
    }
    try:
        df = query_df("SELECT clave, valor FROM configuracion_tienda")
        for _, r in df.iterrows():
            defaults[str(r["clave"])] = str(r["valor"] or "")
    except Exception:
        pass
    return defaults


def set_store_config(values: dict):
    with ENGINE.begin() as conn:
        for k, v in values.items():
            if IS_POSTGRES:
                conn.execute(text("""
                    INSERT INTO configuracion_tienda (clave, valor, actualizado_en)
                    VALUES (:k, :v, CURRENT_TIMESTAMP)
                    ON CONFLICT (clave) DO UPDATE SET valor=EXCLUDED.valor, actualizado_en=CURRENT_TIMESTAMP
                """), {"k": k, "v": str(v or "")})
            else:
                conn.execute(text("""
                    INSERT OR REPLACE INTO configuracion_tienda (clave, valor, actualizado_en)
                    VALUES (:k, :v, CURRENT_TIMESTAMP)
                """), {"k": k, "v": str(v or "")})


def logo_or_icon(size_px: int = 34) -> str:
    cfg = get_store_config()
    logo = str(cfg.get("logo_url", "")).strip()
    if logo:
        safe_logo = html.escape(logo, quote=True)
        return f"<img src='{safe_logo}' style='height:{size_px}px;width:{size_px}px;object-fit:contain;border-radius:10px;vertical-align:middle;margin-right:8px;'>"
    return "🛍️"


# ============================================================
# CONSULTAS DE NEGOCIO
# ============================================================
def productos_con_stock() -> pd.DataFrame:
    return query_df(
        """
        SELECT
            p.id_producto, p.codigo, p.nombre_producto, p.id_categoria,
            COALESCE(c.nombre_categoria, 'Sin categoría') AS categoria,
            p.marca, p.unidad, p.costo_unitario, p.precio_venta, p.stock_minimo,
            COALESCE(p.imagen_url,'') AS imagen_url, COALESCE(p.descripcion,'') AS descripcion,
            p.estado,
            COALESCE(SUM(CASE
                WHEN ms.tipo IN ('ENTRADA_COMPRA','AJUSTE_POSITIVO','MIGRACION_INICIAL') THEN ms.cantidad
                WHEN ms.tipo IN ('SALIDA_VENTA','AJUSTE_NEGATIVO','MERMA') THEN -ms.cantidad
                ELSE 0 END), 0) AS stock_actual
        FROM productos p
        LEFT JOIN categorias c ON c.id_categoria = p.id_categoria
        LEFT JOIN movimientos_stock ms ON ms.id_producto = p.id_producto
        WHERE COALESCE(p.estado,'Activo') = 'Activo'
        GROUP BY p.id_producto, p.codigo, p.nombre_producto, p.id_categoria, c.nombre_categoria,
                 p.marca, p.unidad, p.costo_unitario, p.precio_venta, p.stock_minimo, p.imagen_url, p.descripcion, p.estado
        ORDER BY p.nombre_producto ASC
        """
    )


def ventas_periodo(desde, hasta) -> pd.DataFrame:
    return query_df(
        """
        SELECT v.*, COALESCE(c.nombre_cliente, 'Cliente general') AS cliente
        FROM ventas v
        LEFT JOIN clientes c ON c.id_cliente = v.id_cliente
        WHERE date(v.fecha) BETWEEN :desde AND :hasta AND COALESCE(v.anulada,0)=0
        ORDER BY v.fecha DESC
        """,
        {"desde": str(desde), "hasta": str(hasta)},
    )


def detalle_productos_vendidos(desde, hasta) -> pd.DataFrame:
    return query_df(
        """
        SELECT
            COALESCE(v.vendedor_nombre, 'Sin vendedor') AS vendedor,
            dv.producto_nombre AS producto,
            SUM(dv.cantidad) AS cantidad,
            SUM(dv.subtotal) AS total_vendido,
            SUM((dv.precio_unitario - dv.costo_unitario) * dv.cantidad) AS utilidad
        FROM detalle_ventas dv
        JOIN ventas v ON v.id_venta = dv.id_venta
        WHERE date(v.fecha) BETWEEN :desde AND :hasta AND COALESCE(v.anulada,0)=0
        GROUP BY COALESCE(v.vendedor_nombre, 'Sin vendedor'), dv.producto_nombre
        ORDER BY total_vendido DESC
        """,
        {"desde": str(desde), "hasta": str(hasta)},
    )


def current_user():
    return st.session_state.get("user")


def is_admin():
    u = current_user()
    return bool(u and u.get("rol") in ["Administrador", "Dueño", "Supervisor"])


# ============================================================
# LOGIN
# ============================================================
def login_screen():
    cfg = get_store_config()
    st.markdown(f"""
    <div class="clomar-hero">
      <h1>{logo_or_icon(40)} {html.escape(cfg.get('nombre_tienda', APP_NAME))} Cloud</h1>
      <p>Sistema comercial propio: vendedor en tienda y dueño desde cualquier lugar.</p>
    </div>
    """, unsafe_allow_html=True)
    st.write("")
    if not DB_OK:
        st.error("No se pudo conectar con la base de datos.")
        st.code(DB_ERROR)
        st.info("Configura NEON_DATABASE_URL en Streamlit Secrets o prueba localmente.")
        return

    if IS_LOCAL_SQLITE:
        st.warning("Estás en modo local SQLite. Para usar nube, configura NEON_DATABASE_URL en Streamlit Cloud.")

    c1, c2, c3 = st.columns([1, 1.15, 1])
    with c2:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        st.subheader("Iniciar sesión")
        usuario = st.text_input("Usuario", placeholder="admin o vendedor")
        password = st.text_input("Contraseña", type="password", placeholder="Tu contraseña")
        if st.button("Entrar", type="primary", use_container_width=True):
            df = query_df("SELECT * FROM usuarios WHERE lower(usuario)=lower(:u) AND estado='Activo'", {"u": usuario.strip()})
            if not df.empty and verify_password(password, df.iloc[0]["password_hash"]):
                st.session_state.user = {
                    "id_usuario": int(df.iloc[0]["id_usuario"]),
                    "usuario": df.iloc[0]["usuario"],
                    "nombre": df.iloc[0]["nombre"],
                    "rol": df.iloc[0]["rol"],
                }
                st.session_state.cart = []
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")
        st.caption("Acceso seguro. Usa las credenciales definidas por el administrador.")
        st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# COMPONENTES: COMPROBANTE Y SONIDO
# ============================================================
def play_sale_sound():
    # Sonido breve mediante WebAudio. Algunos navegadores pueden bloquear autoplay.
    st.components.v1.html(
        """
        <script>
        try {
          const ctx = new (window.AudioContext || window.webkitAudioContext)();
          const o = ctx.createOscillator();
          const g = ctx.createGain();
          o.connect(g); g.connect(ctx.destination);
          o.frequency.value = 880; g.gain.value = 0.08; o.start();
          setTimeout(()=>{o.frequency.value=1175;}, 90);
          setTimeout(()=>{g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + .18); o.stop(ctx.currentTime + .19);}, 180);
        } catch(e) {}
        </script>
        """,
        height=0,
    )


def render_receipt(venta_id: int):
    venta = query_df(
        """SELECT v.*, COALESCE(c.nombre_cliente,'Cliente general') AS cliente
           FROM ventas v LEFT JOIN clientes c ON c.id_cliente=v.id_cliente WHERE v.id_venta=:id""",
        {"id": venta_id},
    )
    if venta.empty:
        st.warning("No se encontró el comprobante.")
        return
    v = venta.iloc[0]
    detalles = query_df("SELECT * FROM detalle_ventas WHERE id_venta=:id", {"id": venta_id})
    lines = "".join(
        [f"<div class='receipt-line'><span>{r['cantidad']} x {r['producto_nombre']}</span><b>{money(r['subtotal'])}</b></div>" for _, r in detalles.iterrows()]
    )
    cfg = get_store_config()
    logo_html = logo_or_icon(36)
    tienda = html.escape(cfg.get("nombre_tienda", APP_NAME))
    telefono = html.escape(cfg.get("telefono", ""))
    direccion = html.escape(cfg.get("direccion", ""))
    mensaje = html.escape(cfg.get("mensaje_comprobante", "Gracias por su compra."))
    html = f"""
    <div id="printable-receipt" class="receipt">
      <h2>{logo_html} {tienda}</h2>
      <div style="text-align:center;color:#667085;font-size:13px;">{direccion}</div>
      <div style="text-align:center;color:#667085;font-size:13px;">{telefono}</div>
      <div style="text-align:center;color:#667085;font-size:13px;margin-top:6px;">Comprobante de venta</div>
      <div class="receipt-line"><span>N°</span><b>{v['comprobante']}</b></div>
      <div class="receipt-line"><span>Fecha</span><b>{v['fecha']}</b></div>
      <div class="receipt-line"><span>Cliente</span><b>{v['cliente']}</b></div>
      <div class="receipt-line"><span>Vendedor</span><b>{v['vendedor_nombre']}</b></div>
      <div class="receipt-line"><span>Pago</span><b>{v['metodo_pago']}</b></div>
      {lines}
      <div class="receipt-total">TOTAL {money(v['total_venta'])}</div>
      <div style="text-align:center;color:#667085;margin-top:14px;font-size:12px;">{mensaje}</div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)
    st.markdown("<div class='no-print'>", unsafe_allow_html=True)
    if st.button("🖨️ Imprimir comprobante", use_container_width=True):
        st.components.v1.html("<script>window.print()</script>", height=0)
    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# PÁGINAS
# ============================================================
def page_panel_dueno():
    st.markdown("""
    <div class="clomar-hero"><h1>📊 Panel del dueño</h1><p>Vista ejecutiva en tiempo real para controlar ventas, inventario y caja.</p></div>
    """, unsafe_allow_html=True)
    hoy = date.today()
    desde, hasta = st.columns(2)
    with desde:
        fecha_desde = st.date_input("Desde", hoy, key="panel_desde")
    with hasta:
        fecha_hasta = st.date_input("Hasta", hoy, key="panel_hasta")

    ventas = ventas_periodo(fecha_desde, fecha_hasta)
    detalle = detalle_productos_vendidos(fecha_desde, fecha_hasta)
    productos = productos_con_stock()
    caja = query_df(
        "SELECT * FROM caja WHERE date(fecha) BETWEEN :d AND :h AND COALESCE(anulada,0)=0",
        {"d": str(fecha_desde), "h": str(fecha_hasta)},
    )
    total_ventas = ventas["total_venta"].sum() if not ventas.empty else 0
    utilidad = detalle["utilidad"].sum() if not detalle.empty else 0
    ticket = total_ventas / len(ventas) if len(ventas) else 0
    ingresos = caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum() if not caja.empty else 0
    egresos = caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum() if not caja.empty else 0

    a, b, c, d = st.columns(4)
    with a: kpi("Ventas", money(total_ventas), f"{len(ventas)} comprobantes")
    with b: kpi("Utilidad estimada", money(utilidad), "Según costo registrado")
    with c: kpi("Ticket promedio", money(ticket), "Promedio por venta")
    with d: kpi("Caja neta", money(ingresos - egresos), f"Ingresos {money(ingresos)}")

    st.write("")
    c1, c2 = st.columns([1.35, 1])
    with c1:
        st.subheader("Ventas recientes")
        if ventas.empty:
            st.info("Todavía no hay ventas en el período.")
        else:
            vtab = ventas.copy()
            vtab["total"] = vtab["total_venta"].apply(money)
            html_table(vtab, ["comprobante", "fecha", "cliente", "vendedor_nombre", "metodo_pago", "total"], ["Comprobante", "Fecha", "Cliente", "Vendedor", "Pago", "Total"], 12)
    with c2:
        st.subheader("Stock crítico")
        crit = productos[productos["stock_actual"] <= productos["stock_minimo"]].copy() if not productos.empty else pd.DataFrame()
        if crit.empty:
            st.success("Sin productos críticos.")
        else:
            for _, r in crit.head(8).iterrows():
                st.markdown(f"<span class='chip chip-red'>⚠️ {r['nombre_producto']} · Stock {num(r['stock_actual'])}</span>", unsafe_allow_html=True)

    st.subheader("Productos vendidos")
    if not detalle.empty:
        fig = px.bar(detalle.head(10), x="producto", y="total_vendido", color="vendedor", title="Top productos por venta", template="plotly_white")
        fig.update_layout(paper_bgcolor="white", plot_bgcolor="white", font_color="#111827")
        st.plotly_chart(fig, use_container_width=True)
        dtab = detalle.copy()
        dtab["total"] = dtab["total_vendido"].apply(money)
        dtab["utilidad_fmt"] = dtab["utilidad"].apply(money)
        html_table(dtab, ["vendedor", "producto", "cantidad", "total", "utilidad_fmt"], ["Vendedor", "Producto", "Cantidad", "Total", "Utilidad"], 20)
    else:
        st.info("No hay detalle de productos vendidos.")


def page_ventas():
    st.markdown("<div class='clomar-hero'><h1>🧾 Nueva venta</h1><p>Busca productos, agrégalos al carrito y registra el pago.</p></div>", unsafe_allow_html=True)
    if "cart" not in st.session_state:
        st.session_state.cart = []

    productos = productos_con_stock()
    categorias = query_df("SELECT * FROM categorias WHERE estado='Activo' ORDER BY nombre_categoria")
    col_main, col_cart = st.columns([1.8, 1])

    with col_main:
        s1, s2 = st.columns([2, 1])
        with s1:
            buscar = st.text_input("Buscar producto", placeholder="Nombre, código, marca...")
        with s2:
            cats = ["Todas"] + categorias["nombre_categoria"].tolist() if not categorias.empty else ["Todas"]
            cat = st.selectbox("Categoría", cats)
        fil = productos.copy()
        if buscar.strip():
            txt = buscar.lower().strip()
            fil = fil[
                fil["nombre_producto"].astype(str).str.lower().str.contains(txt, na=False) |
                fil["codigo"].astype(str).str.lower().str.contains(txt, na=False) |
                fil["marca"].astype(str).str.lower().str.contains(txt, na=False)
            ]
        if cat != "Todas":
            fil = fil[fil["categoria"] == cat]

        if fil.empty:
            st.info("No hay productos que coincidan.")
        else:
            cols = st.columns(3)
            for i, (_, r) in enumerate(fil.head(60).iterrows()):
                with cols[i % 3]:
                    stock = float(r["stock_actual"] or 0)
                    chip = "chip-ok" if stock > float(r["stock_minimo"] or 0) else "chip-red"
                    st.markdown(f"""
                    <div class="product-card">
                        <div class="product-name">{r['nombre_producto']}</div>
                        <div class="product-meta">{r.get('codigo','')} · {r.get('categoria','')}</div>
                        <div class="product-price">{money(r['precio_venta'])}</div>
                        <span class="chip {chip}">Stock {num(stock)}</span>
                    </div>
                    """, unsafe_allow_html=True)
                    if st.button("Agregar", key=f"add_{r['id_producto']}", use_container_width=True, disabled=stock <= 0):
                        item = {
                            "id_producto": int(r["id_producto"]), "nombre": r["nombre_producto"],
                            "cantidad": 1.0, "precio": float(r["precio_venta"] or 0),
                            "costo": float(r["costo_unitario"] or 0), "stock": stock
                        }
                        # Si ya está, aumenta cantidad.
                        found = False
                        for c in st.session_state.cart:
                            if c["id_producto"] == item["id_producto"]:
                                c["cantidad"] = min(float(c["cantidad"]) + 1, stock)
                                found = True
                        if not found:
                            st.session_state.cart.append(item)
                        st.rerun()

    with col_cart:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        st.subheader("🛒 Carrito")
        if not st.session_state.cart:
            st.info("Agrega productos para vender.")
        else:
            nuevo_cart = []
            total = 0
            for idx, item in enumerate(st.session_state.cart):
                st.markdown(f"**{item['nombre']}**")
                cc1, cc2, cc3 = st.columns([.8, .8, .4])
                with cc1:
                    cant = st.number_input("Cant.", min_value=0.0, max_value=float(item["stock"]), value=float(item["cantidad"]), step=1.0, key=f"cant_{idx}")
                with cc2:
                    precio = st.number_input("Precio", min_value=0.0, value=float(item["precio"]), step=1.0, key=f"precio_{idx}")
                with cc3:
                    if st.button("❌", key=f"del_{idx}"):
                        cant = 0
                if cant > 0:
                    item["cantidad"] = cant
                    item["precio"] = precio
                    total += cant * precio
                    nuevo_cart.append(item)
                st.divider()
            st.session_state.cart = nuevo_cart
            st.markdown(f"### Total: {money(total)}")

            clientes = query_df("SELECT id_cliente, nombre_cliente FROM clientes WHERE estado='Activo' ORDER BY nombre_cliente")
            opciones_cliente = {"Cliente general": None}
            if not clientes.empty:
                opciones_cliente.update({r["nombre_cliente"]: int(r["id_cliente"]) for _, r in clientes.iterrows()})
            cliente_nombre = st.selectbox("Cliente", list(opciones_cliente.keys()))
            metodo_pago = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Tarjeta", "Transferencia", "Crédito", "Mixto"])
            monto_pagado = st.number_input("Monto pagado", min_value=0.0, value=float(total if metodo_pago != "Crédito" else 0), step=1.0)
            observacion = st.text_area("Observación", placeholder="Nota interna, pedido, entrega...")
            saldo = max(total - monto_pagado, 0)
            st.markdown(f"<div class='kpi-card'><div class='kpi-label'>Saldo pendiente</div><div class='kpi-value'>{money(saldo)}</div></div>", unsafe_allow_html=True)

            if st.button("Crear venta", type="primary", use_container_width=True):
                if total <= 0:
                    st.error("El total debe ser mayor a cero.")
                else:
                    u = current_user()
                    comprobante = f"V{datetime.now().strftime('%Y%m%d%H%M%S')}"
                    estado_pago = "Pagada" if saldo <= 0 else ("Parcial" if monto_pagado > 0 else "Pendiente")
                    try:
                        with ENGINE.begin() as conn:
                            res = conn.execute(text("""
                                INSERT INTO ventas (comprobante, id_cliente, id_usuario, vendedor_nombre, metodo_pago, total_venta, monto_pagado, saldo_pendiente, estado_pago, observacion)
                                VALUES (:comp, :cli, :uid, :vend, :metodo, :total, :pagado, :saldo, :estado, :obs)
                            """), {"comp": comprobante, "cli": opciones_cliente[cliente_nombre], "uid": u["id_usuario"], "vend": u["nombre"], "metodo": metodo_pago, "total": total, "pagado": monto_pagado, "saldo": saldo, "estado": estado_pago, "obs": observacion})
                            venta_id = int(conn.execute(text("SELECT id_venta FROM ventas WHERE comprobante=:c"), {"c": comprobante}).scalar())
                            for item in st.session_state.cart:
                                subtotal = float(item["cantidad"]) * float(item["precio"])
                                conn.execute(text("""
                                    INSERT INTO detalle_ventas (id_venta, id_producto, producto_nombre, cantidad, precio_unitario, costo_unitario, subtotal)
                                    VALUES (:idv, :idp, :prod, :cant, :precio, :costo, :sub)
                                """), {"idv": venta_id, "idp": item["id_producto"], "prod": item["nombre"], "cant": item["cantidad"], "precio": item["precio"], "costo": item["costo"], "sub": subtotal})
                                conn.execute(text("""
                                    INSERT INTO movimientos_stock (id_producto, tipo, cantidad, costo_unitario, referencia, id_usuario, observacion)
                                    VALUES (:idp, 'SALIDA_VENTA', :cant, :costo, :ref, :uid, :obs)
                                """), {"idp": item["id_producto"], "cant": item["cantidad"], "costo": item["costo"], "ref": comprobante, "uid": u["id_usuario"], "obs": "Venta"})
                            if monto_pagado > 0:
                                conn.execute(text("""
                                    INSERT INTO caja (tipo, concepto, metodo_pago, monto, referencia, id_usuario, observacion)
                                    VALUES ('Ingreso', :concepto, :metodo, :monto, :ref, :uid, :obs)
                                """), {"concepto": f"Venta {comprobante}", "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": observacion})
                        st.session_state.cart = []
                        st.session_state.last_sale_id = venta_id
                        st.session_state.show_success_sale = True
                        st.rerun()
                    except Exception as e:
                        st.error("No se pudo registrar la venta.")
                        st.exception(e)
        st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state.get("show_success_sale"):
        play_sale_sound()
        st.markdown("<div class='success-panel'><div class='success-icon'>✅</div><div class='success-title'>¡Creaste una venta!</div><div class='success-sub'>Se registró correctamente en ventas, inventario y caja.</div></div>", unsafe_allow_html=True)
        render_receipt(int(st.session_state.last_sale_id))
        if st.button("Seguir vendiendo", type="primary", use_container_width=True):
            st.session_state.show_success_sale = False
            st.rerun()



def build_excel_template() -> bytes:
    ejemplo = pd.DataFrame([
        {
            "codigo": "0001",
            "nombre": "ZAPATILLAS NEGRAS OUTDOOR P",
            "categoria": "Calzado",
            "marca": "Outdoor",
            "descripcion": "Producto de ejemplo",
            "costo": 240,
            "precio": 380,
            "stock": 2,
            "stock_minimo": 1,
            "imagen_url": "",
            "estado": "Activo",
        },
        {
            "codigo": "0002",
            "nombre": "SOMBRERO",
            "categoria": "Accesorios",
            "marca": "",
            "descripcion": "Producto de ejemplo",
            "costo": 40,
            "precio": 80,
            "stock": 0,
            "stock_minimo": 1,
            "imagen_url": "",
            "estado": "Activo",
        },
    ])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        ejemplo.to_excel(writer, index=False, sheet_name="productos")
        instrucciones = pd.DataFrame([
            {"campo": "codigo", "uso": "Obligatorio y único. Si ya existe, se actualiza el producto."},
            {"campo": "nombre", "uso": "Obligatorio. Nombre comercial del producto."},
            {"campo": "categoria", "uso": "Opcional. Si no existe, la app la crea automáticamente."},
            {"campo": "costo", "uso": "Número sin S/. Solo administrador lo ve."},
            {"campo": "precio", "uso": "Número sin S/. Precio de venta."},
            {"campo": "stock", "uso": "Stock objetivo. La app ajusta entradas/salidas para igualar este valor."},
            {"campo": "imagen_url", "uso": "Opcional. Enlace directo a imagen JPG/PNG/WebP."},
            {"campo": "estado", "uso": "Activo o Inactivo."},
        ])
        instrucciones.to_excel(writer, index=False, sheet_name="instrucciones")
    buffer.seek(0)
    return buffer.getvalue()


def ensure_category(nombre: str) -> int | None:
    nombre = str(nombre or "").strip() or "Sin categoría"
    row = query_df("SELECT id_categoria FROM categorias WHERE lower(nombre_categoria)=lower(:n)", {"n": nombre})
    if not row.empty:
        return int(row.iloc[0]["id_categoria"])
    with ENGINE.begin() as conn:
        conn.execute(text("INSERT INTO categorias (nombre_categoria, descripcion, estado) VALUES (:n, '', 'Activo')"), {"n": nombre})
        return int(conn.execute(text("SELECT id_categoria FROM categorias WHERE lower(nombre_categoria)=lower(:n)"), {"n": nombre}).scalar())


def current_stock_for_product(id_producto: int) -> float:
    value = scalar("""
        SELECT COALESCE(SUM(CASE
            WHEN tipo IN ('ENTRADA_COMPRA','AJUSTE_POSITIVO','MIGRACION_INICIAL') THEN cantidad
            WHEN tipo IN ('SALIDA_VENTA','AJUSTE_NEGATIVO','MERMA') THEN -cantidad
            ELSE 0 END),0)
        FROM movimientos_stock WHERE id_producto=:idp
    """, {"idp": int(id_producto)}, 0)
    return float(value or 0)


def product_card_markup(r, show_cost=False, show_image=True):
    nombre = html.escape(str(r.get("nombre_producto", "")))
    codigo = html.escape(str(r.get("codigo", "")))
    categoria = html.escape(str(r.get("categoria", "")))
    descripcion = html.escape(str(r.get("descripcion", "") or ""))
    stock = float(r.get("stock_actual", 0) or 0)
    minimo = float(r.get("stock_minimo", 0) or 0)
    chip = "chip-ok" if stock > minimo else "chip-red"
    costo = f"<span class='chip chip-dark'>Costo {money(r.get('costo_unitario'))}</span>" if show_cost else ""
    img_url = str(r.get("imagen_url", "") or "").strip()
    img_html = ""
    if show_image and img_url:
        img_html = f"<img src='{html.escape(img_url, quote=True)}' style='width:100%;height:140px;object-fit:cover;border-radius:16px;margin-bottom:12px;border:1px solid #e5e7eb;'>"
    desc_html = f"<div class='product-meta'>{descripcion[:90]}</div>" if descripcion else ""
    return f"""
    <div class='product-card'>
      {img_html}
      <div class='product-name'>📦 {nombre}</div>
      <div class='product-meta'>{codigo} · {categoria}</div>
      {desc_html}
      <div class='product-price'>{money(r.get('precio_venta'))}</div>
      <span class='chip {chip}'>Stock {num(stock)}</span>
      {costo}
    </div>
    """




def normalize_phone(raw: str) -> str:
    """Convierte teléfono local a formato wa.me simple. Si no tiene país y tiene 9 dígitos, asume Perú 51."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 9:
        return "51" + digits
    return digits


def whatsapp_product_url(product_name: str, price, cfg: dict) -> str:
    phone = normalize_phone(cfg.get("telefono", ""))
    msg = f"Hola, estoy interesado(a) en el producto {product_name} de {cfg.get('nombre_tienda', APP_NAME)}. Precio: {money(price)}. ¿Está disponible?"
    encoded = urllib.parse.quote(msg)
    if phone:
        return f"https://wa.me/{phone}?text={encoded}"
    return f"https://wa.me/?text={encoded}"


def public_catalog_url() -> str:
    cfg = get_store_config()
    base = str(cfg.get("catalogo_url_base") or get_secret("PUBLIC_APP_URL", "https://clomar-store.streamlit.app")).strip().rstrip("/")
    if "?" in base:
        return base + "&catalogo=1"
    return base + "/?catalogo=1"


def fetch_image_bytes(url: str, timeout: int = 5) -> bytes | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(str(url), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(900_000)
        return data
    except Exception:
        return None


def build_catalog_pdf(productos_df: pd.DataFrame, cfg: dict, incluir_stock: bool = True) -> bytes:
    """Genera catálogo PDF liviano para clientes. No incluye costos."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
    from reportlab.lib.utils import ImageReader

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.1*cm, leftMargin=1.1*cm, topMargin=1.0*cm, bottomMargin=1.0*cm)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="StoreTitle", parent=styles["Title"], fontSize=22, textColor=colors.HexColor("#111827"), leading=26, spaceAfter=6))
    styles.add(ParagraphStyle(name="Muted", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#667085"), leading=12))
    styles.add(ParagraphStyle(name="ProductName", parent=styles["Heading3"], fontSize=11, textColor=colors.HexColor("#111827"), leading=14, spaceAfter=3))
    styles.add(ParagraphStyle(name="Price", parent=styles["Normal"], fontSize=13, textColor=colors.HexColor("#b42318"), leading=16, fontName="Helvetica-Bold"))

    story = []
    logo_data = fetch_image_bytes(str(cfg.get("logo_url", "")))
    if logo_data:
        try:
            img = Image(io.BytesIO(logo_data), width=4.6*cm, height=1.6*cm)
            img.hAlign = "LEFT"
            story.append(img)
            story.append(Spacer(1, 4))
        except Exception:
            pass
    story.append(Paragraph(str(cfg.get("nombre_tienda", APP_NAME)), styles["StoreTitle"]))
    info = []
    if cfg.get("direccion"):
        info.append(str(cfg.get("direccion")))
    if cfg.get("telefono"):
        info.append(f"WhatsApp: {cfg.get('telefono')}")
    info.append(f"Catálogo actualizado: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    story.append(Paragraph(" | ".join(info), styles["Muted"]))
    story.append(Spacer(1, 12))

    if productos_df is None or productos_df.empty:
        story.append(Paragraph("No hay productos activos para mostrar.", styles["Normal"]))
    else:
        df = productos_df.copy()
        df = df.sort_values(["categoria", "nombre_producto"])
        data_rows = []
        for _, r in df.iterrows():
            stock_txt = "Disponible" if float(r.get("stock_actual", 0) or 0) > 0 else "Consultar disponibilidad"
            if incluir_stock and float(r.get("stock_actual", 0) or 0) <= 0:
                stock_txt = "Consultar disponibilidad"
            code_cat = f"{r.get('codigo','')} · {r.get('categoria','')}"
            product_html = f"<b>{html.escape(str(r.get('nombre_producto','')))}</b><br/><font color='#667085'>{html.escape(str(code_cat))}</font>"
            if str(r.get("descripcion", "") or "").strip():
                product_html += f"<br/><font color='#667085'>{html.escape(str(r.get('descripcion'))[:120])}</font>"
            data_rows.append([
                Paragraph(product_html, styles["Normal"]),
                Paragraph(money(r.get("precio_venta")), styles["Price"]),
                Paragraph(stock_txt, styles["Muted"]),
            ])
        table = Table([["Producto", "Precio", "Disponibilidad"]] + data_rows, colWidths=[10.2*cm, 3.0*cm, 4.0*cm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#111827")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,0), 9),
            ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#e5e7eb")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f8fafc")]),
            ("LEFTPADDING", (0,0), (-1,-1), 7),
            ("RIGHTPADDING", (0,0), (-1,-1), 7),
            ("TOPPADDING", (0,0), (-1,-1), 7),
            ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ]))
        story.append(table)
    story.append(Spacer(1, 10))
    msg = str(cfg.get("mensaje_comprobante") or "Gracias por su compra.")
    story.append(Paragraph(msg, styles["Muted"]))
    doc.build(story)
    return buffer.getvalue()


def page_catalogo_clientes(public: bool = False):
    cfg = get_store_config()
    productos = productos_con_stock()
    categorias = query_df("SELECT * FROM categorias WHERE estado='Activo' ORDER BY nombre_categoria")
    if public:
        logo = cfg.get("logo_url", "")
        logo_html = f"<img src='{html.escape(logo, quote=True)}' style='max-height:76px;max-width:260px;object-fit:contain;margin-bottom:8px;'>" if logo else ""
        st.markdown(f"""
        <div class='public-hero'>
          {logo_html}
          <h1>{html.escape(cfg.get('nombre_tienda', APP_NAME))}</h1>
          <p>Catálogo de productos · Consulta disponibilidad por WhatsApp.</p>
          <p>{html.escape(cfg.get('direccion',''))} {(' · WhatsApp ' + html.escape(cfg.get('telefono',''))) if cfg.get('telefono') else ''}</p>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("<div class='clomar-hero'><h1>📘 Catálogo para clientes</h1><p>Vista comercial, PDF y consultas por WhatsApp. No muestra costos.</p></div>", unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1.6, 1, .8])
    with c1:
        buscar = st.text_input("Buscar en catálogo", placeholder="Nombre, código, categoría...", key="cat_cliente_buscar")
    with c2:
        cats = ["Todas"] + (categorias["nombre_categoria"].tolist() if not categorias.empty else [])
        categoria = st.selectbox("Categoría", cats, key="cat_cliente_categoria")
    with c3:
        solo_stock = st.toggle("Solo con stock", value=True, key="cat_cliente_stock")

    fil = productos.copy()
    if buscar.strip() and not fil.empty:
        txt = buscar.lower().strip()
        fil = fil[
            fil["nombre_producto"].astype(str).str.lower().str.contains(txt, na=False) |
            fil["codigo"].astype(str).str.lower().str.contains(txt, na=False) |
            fil["categoria"].astype(str).str.lower().str.contains(txt, na=False)
        ]
    if categoria != "Todas" and not fil.empty:
        fil = fil[fil["categoria"] == categoria]
    if solo_stock and not fil.empty:
        fil = fil[fil["stock_actual"].astype(float) > 0]

    p1, p2, p3 = st.columns([1, 1, 1])
    with p1:
        st.download_button(
            "📄 Descargar catálogo PDF",
            data=build_catalog_pdf(fil, cfg),
            file_name=f"catalogo_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
            mime="application/pdf",
            use_container_width=True,
            type="primary",
        )
    with p2:
        if not public:
            st.link_button("🌐 Abrir catálogo público", public_catalog_url(), use_container_width=True)
    with p3:
        if cfg.get("telefono"):
            st.link_button("💬 WhatsApp tienda", f"https://wa.me/{normalize_phone(cfg.get('telefono'))}", use_container_width=True)

    st.caption(f"Productos visibles: {len(fil) if fil is not None else 0}. El catálogo no muestra costos internos.")
    if fil.empty:
        st.info("No hay productos para mostrar con esos filtros.")
        return

    cols = st.columns(3)
    for i, (_, r) in enumerate(fil.head(120).iterrows()):
        with cols[i % 3]:
            img_url = str(r.get("imagen_url", "") or "").strip()
            if img_url:
                img = f"<img class='catalog-img' src='{html.escape(img_url, quote=True)}'>"
            else:
                img = "<div class='catalog-empty-img'>🛍️</div>"
            stock = float(r.get("stock_actual", 0) or 0)
            stock_chip = "Disponible" if stock > 0 else "Consultar"
            st.markdown(f"""
            <div class='catalog-card'>
              {img}
              <div class='product-name'>{html.escape(str(r.get('nombre_producto','')))}</div>
              <div class='product-meta'>{html.escape(str(r.get('codigo','')))} · {html.escape(str(r.get('categoria','')))}</div>
              <div class='product-price'>{money(r.get('precio_venta'))}</div>
              <span class='chip {'chip-ok' if stock > 0 else 'chip-warn'}'>{stock_chip}</span>
            </div>
            """, unsafe_allow_html=True)
            st.link_button("💬 Consultar por WhatsApp", whatsapp_product_url(str(r.get("nombre_producto", "Producto")), r.get("precio_venta"), cfg), use_container_width=True)

def page_productos():
    st.markdown("<div class='clomar-hero'><h1>📦 Productos</h1><p>Catálogo visual, importación desde Excel, precios y stock.</p></div>", unsafe_allow_html=True)
    productos = productos_con_stock()
    categorias = query_df("SELECT * FROM categorias WHERE estado='Activo' ORDER BY nombre_categoria")

    if is_admin():
        tab1, tab2, tab3 = st.tabs(["Catálogo", "Crear / editar", "Importar Excel"])
    else:
        tab1, = st.tabs(["Catálogo"])
        tab2 = tab3 = None

    with tab1:
        col_a, col_b = st.columns([2, 1])
        with col_a:
            buscar = st.text_input("Buscar producto", key="buscar_productos", placeholder="Nombre, código o categoría...")
        with col_b:
            cats = ["Todas"] + (categorias["nombre_categoria"].tolist() if not categorias.empty else [])
            cat_filter = st.selectbox("Categoría", cats, key="prod_cat_filter")
        fil = productos.copy()
        if buscar.strip():
            txt = buscar.lower().strip()
            fil = fil[
                fil["nombre_producto"].astype(str).str.lower().str.contains(txt, na=False) |
                fil["codigo"].astype(str).str.lower().str.contains(txt, na=False) |
                fil["categoria"].astype(str).str.lower().str.contains(txt, na=False)
            ]
        if cat_filter != "Todas":
            fil = fil[fil["categoria"] == cat_filter]
        if fil.empty:
            st.info("No hay productos.")
        else:
            cols = st.columns(3 if is_admin() else 2)
            for i, (_, r) in enumerate(fil.head(120).iterrows()):
                with cols[i % len(cols)]:
                    st.markdown(product_card_markup(r, show_cost=is_admin()), unsafe_allow_html=True)
        if not is_admin():
            st.info("Vista de vendedor: puedes consultar productos, precios y stock. La creación y edición quedan reservadas al administrador.")

    if is_admin() and tab2 is not None:
        with tab2:
            productos_actuales = productos.copy()
            modo = st.radio("Modo", ["Nuevo producto", "Editar producto existente"], horizontal=True)
            selected = None
            if modo == "Editar producto existente" and not productos_actuales.empty:
                opciones = {f"{r['codigo']} · {r['nombre_producto']}": r for _, r in productos_actuales.iterrows()}
                elegido = st.selectbox("Selecciona producto", list(opciones.keys()))
                selected = opciones[elegido]

            cat_opts = {r["nombre_categoria"]: int(r["id_categoria"]) for _, r in categorias.iterrows()} if not categorias.empty else {}
            default_cat = str(selected.get("categoria", "")) if selected is not None else (list(cat_opts.keys())[0] if cat_opts else "Sin categoría")
            if default_cat not in cat_opts and default_cat:
                cat_opts[default_cat] = ensure_category(default_cat)

            with st.form("form_producto_v247"):
                a, b = st.columns(2)
                with a:
                    codigo = st.text_input("Código", value=str(selected.get("codigo", "")) if selected is not None else "", placeholder="SKU o código interno")
                    nombre = st.text_input("Nombre del producto", value=str(selected.get("nombre_producto", "")) if selected is not None else "")
                    marca = st.text_input("Marca", value=str(selected.get("marca", "")) if selected is not None else "")
                    unidad = st.selectbox("Unidad", ["Unidad", "Par", "Caja", "Docena", "Metro", "Kilo", "Litro", "Paquete"], index=0)
                    descripcion = st.text_area("Descripción corta", value=str(selected.get("descripcion", "")) if selected is not None else "")
                    imagen_url = st.text_input("Imagen URL", value=str(selected.get("imagen_url", "")) if selected is not None else "", placeholder="https://...")
                with b:
                    categoria = st.selectbox("Categoría", list(cat_opts.keys()) if cat_opts else ["Sin categoría"], index=list(cat_opts.keys()).index(default_cat) if default_cat in cat_opts else 0)
                    costo = st.number_input("Costo unitario", min_value=0.0, value=float(selected.get("costo_unitario", 0) or 0) if selected is not None else 0.0, step=1.0)
                    precio = st.number_input("Precio de venta", min_value=0.0, value=float(selected.get("precio_venta", 0) or 0) if selected is not None else 0.0, step=1.0)
                    stock_min = st.number_input("Stock mínimo", min_value=0.0, value=float(selected.get("stock_minimo", 0) or 0) if selected is not None else 0.0, step=1.0)
                    stock_objetivo = st.number_input("Stock actual / objetivo", min_value=0.0, value=float(selected.get("stock_actual", 0) or 0) if selected is not None else 0.0, step=1.0)
                    estado = st.selectbox("Estado", ["Activo", "Inactivo"], index=0)
                submitted = st.form_submit_button("Guardar producto", type="primary", use_container_width=True)

            if submitted:
                if not codigo.strip() or not nombre.strip():
                    st.error("Código y nombre son obligatorios.")
                else:
                    u = current_user()
                    id_cat = ensure_category(categoria)
                    try:
                        with ENGINE.begin() as conn:
                            if selected is not None:
                                idp = int(selected["id_producto"])
                                conn.execute(text("""
                                    UPDATE productos SET codigo=:codigo, nombre_producto=:nombre, id_categoria=:cat, marca=:marca,
                                        unidad=:unidad, costo_unitario=:costo, precio_venta=:precio, stock_minimo=:stock_min,
                                        descripcion=:descripcion, imagen_url=:imagen_url, estado=:estado, actualizado_en=CURRENT_TIMESTAMP
                                    WHERE id_producto=:idp
                                """), {"codigo": codigo.strip(), "nombre": nombre.strip(), "cat": id_cat, "marca": marca, "unidad": unidad, "costo": costo, "precio": precio, "stock_min": stock_min, "descripcion": descripcion, "imagen_url": imagen_url, "estado": estado, "idp": idp})
                            else:
                                conn.execute(text("""
                                    INSERT INTO productos (codigo, nombre_producto, id_categoria, marca, unidad, costo_unitario, precio_venta, stock_minimo, descripcion, imagen_url, estado)
                                    VALUES (:codigo, :nombre, :cat, :marca, :unidad, :costo, :precio, :stock_min, :descripcion, :imagen_url, :estado)
                                """), {"codigo": codigo.strip(), "nombre": nombre.strip(), "cat": id_cat, "marca": marca, "unidad": unidad, "costo": costo, "precio": precio, "stock_min": stock_min, "descripcion": descripcion, "imagen_url": imagen_url, "estado": estado})
                                idp = int(conn.execute(text("SELECT id_producto FROM productos WHERE codigo=:codigo"), {"codigo": codigo.strip()}).scalar())
                            actual = current_stock_for_product(idp)
                            diff = float(stock_objetivo or 0) - actual
                            if abs(diff) > 0.0001:
                                tipo = "AJUSTE_POSITIVO" if diff > 0 else "AJUSTE_NEGATIVO"
                                conn.execute(text("""
                                    INSERT INTO movimientos_stock (id_producto, tipo, cantidad, costo_unitario, referencia, id_usuario, observacion)
                                    VALUES (:idp, :tipo, :cant, :costo, 'Ajuste manual producto', :uid, 'Edición producto')
                                """), {"idp": idp, "tipo": tipo, "cant": abs(diff), "costo": costo, "uid": u["id_usuario"]})
                        st.success("Producto guardado correctamente.")
                        st.rerun()
                    except Exception as e:
                        st.error("No se pudo guardar. Revisa si el código ya existe.")
                        st.exception(e)

    if is_admin() and tab3 is not None:
        with tab3:
            st.subheader("Importar productos desde Excel")
            st.write("Descarga la plantilla, llena tus productos y vuelve a subir el archivo. El sistema creará productos nuevos y actualizará los existentes por código.")
            st.download_button(
                "⬇️ Descargar plantilla Excel",
                data=build_excel_template(),
                file_name="plantilla_productos_clomar_store.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
            )
            uploaded = st.file_uploader("Subir Excel de productos", type=["xlsx", "xls"])
            if uploaded is not None:
                try:
                    df = pd.read_excel(uploaded, sheet_name=0)
                    df.columns = [str(c).strip().lower() for c in df.columns]
                    required = ["codigo", "nombre", "precio", "stock"]
                    missing = [c for c in required if c not in df.columns]
                    if missing:
                        st.error("Faltan columnas obligatorias: " + ", ".join(missing))
                    else:
                        for c in ["categoria","marca","descripcion","costo","stock_minimo","imagen_url","estado"]:
                            if c not in df.columns:
                                df[c] = "" if c not in ["costo","stock_minimo"] else 0
                        df_preview = df[["codigo","nombre","categoria","costo","precio","stock","stock_minimo","imagen_url","estado"]].copy()
                        st.write("Vista previa:")
                        st.dataframe(df_preview.head(50), use_container_width=True)
                        total_rows = len(df_preview)
                        invalid = df_preview[df_preview["codigo"].isna() | df_preview["nombre"].isna() | df_preview["precio"].isna() | df_preview["stock"].isna()]
                        st.info(f"Productos detectados: {total_rows}. Filas con datos obligatorios incompletos: {len(invalid)}.")
                        if st.button("✅ Importar / actualizar productos", type="primary", use_container_width=True):
                            if len(invalid) > 0:
                                st.error("Corrige las filas incompletas antes de importar.")
                            else:
                                creados = actualizados = ajustes = 0
                                errores = []
                                u = current_user()
                                for idx, row in df.iterrows():
                                    try:
                                        codigo = str(row.get("codigo", "")).strip()
                                        nombre = str(row.get("nombre", "")).strip()
                                        categoria = str(row.get("categoria", "") or "Sin categoría").strip() or "Sin categoría"
                                        marca = str(row.get("marca", "") or "").strip()
                                        descripcion = str(row.get("descripcion", "") or "").strip()
                                        imagen_url = str(row.get("imagen_url", "") or "").strip()
                                        estado = str(row.get("estado", "Activo") or "Activo").strip() or "Activo"
                                        costo = float(row.get("costo", 0) or 0)
                                        precio = float(row.get("precio", 0) or 0)
                                        stock = float(row.get("stock", 0) or 0)
                                        stock_min = float(row.get("stock_minimo", 0) or 0)
                                        id_cat = ensure_category(categoria)
                                        existing = query_df("SELECT id_producto FROM productos WHERE codigo=:codigo", {"codigo": codigo})
                                        with ENGINE.begin() as conn:
                                            if existing.empty:
                                                conn.execute(text("""
                                                    INSERT INTO productos (codigo, nombre_producto, id_categoria, marca, unidad, costo_unitario, precio_venta, stock_minimo, descripcion, imagen_url, estado)
                                                    VALUES (:codigo, :nombre, :cat, :marca, 'Unidad', :costo, :precio, :stock_min, :descripcion, :imagen_url, :estado)
                                                """), {"codigo": codigo, "nombre": nombre, "cat": id_cat, "marca": marca, "costo": costo, "precio": precio, "stock_min": stock_min, "descripcion": descripcion, "imagen_url": imagen_url, "estado": estado})
                                                idp = int(conn.execute(text("SELECT id_producto FROM productos WHERE codigo=:codigo"), {"codigo": codigo}).scalar())
                                                creados += 1
                                                actual_stock = 0
                                            else:
                                                idp = int(existing.iloc[0]["id_producto"])
                                                conn.execute(text("""
                                                    UPDATE productos SET nombre_producto=:nombre, id_categoria=:cat, marca=:marca,
                                                        costo_unitario=:costo, precio_venta=:precio, stock_minimo=:stock_min,
                                                        descripcion=:descripcion, imagen_url=:imagen_url, estado=:estado, actualizado_en=CURRENT_TIMESTAMP
                                                    WHERE id_producto=:idp
                                                """), {"nombre": nombre, "cat": id_cat, "marca": marca, "costo": costo, "precio": precio, "stock_min": stock_min, "descripcion": descripcion, "imagen_url": imagen_url, "estado": estado, "idp": idp})
                                                actualizados += 1
                                                actual_stock = current_stock_for_product(idp)
                                            diff = stock - actual_stock
                                            if abs(diff) > 0.0001:
                                                tipo = "AJUSTE_POSITIVO" if diff > 0 else "AJUSTE_NEGATIVO"
                                                conn.execute(text("""
                                                    INSERT INTO movimientos_stock (id_producto, tipo, cantidad, costo_unitario, referencia, id_usuario, observacion)
                                                    VALUES (:idp, :tipo, :cant, :costo, 'Importación Excel', :uid, 'Carga masiva Excel')
                                                """), {"idp": idp, "tipo": tipo, "cant": abs(diff), "costo": costo, "uid": u["id_usuario"]})
                                                ajustes += 1
                                    except Exception as e:
                                        errores.append(f"Fila {idx+2}: {e}")
                                st.success(f"Importación terminada. Creados: {creados}. Actualizados: {actualizados}. Ajustes de stock: {ajustes}.")
                                if errores:
                                    st.warning("Algunas filas tuvieron errores:")
                                    st.write(errores[:20])
                                st.rerun()
                except Exception as e:
                    st.error("No se pudo leer el Excel. Verifica que sea .xlsx y que tenga la hoja de productos.")
                    st.exception(e)

def page_inventario():
    st.markdown("<div class='clomar-hero'><h1>📊 Inventario</h1><p>Stock actual, stock crítico y valorización.</p></div>", unsafe_allow_html=True)
    productos = productos_con_stock()
    if productos.empty:
        st.info("No hay productos registrados.")
        return
    total_costo = (productos["stock_actual"] * productos["costo_unitario"]).sum()
    total_venta = (productos["stock_actual"] * productos["precio_venta"]).sum()
    criticos = productos[productos["stock_actual"] <= productos["stock_minimo"]]
    a, b, c = st.columns(3)
    with a: kpi("Valor stock costo", money(total_costo), "Solo administrador")
    with b: kpi("Valor stock venta", money(total_venta), "Potencial de venta")
    with c: kpi("Productos críticos", str(len(criticos)), "Stock bajo o cero")
    tab = productos.copy()
    tab["precio"] = tab["precio_venta"].apply(money)
    tab["costo"] = tab["costo_unitario"].apply(money)
    cols = ["codigo", "nombre_producto", "categoria", "stock_actual", "stock_minimo", "precio"]
    heads = ["Código", "Producto", "Categoría", "Stock", "Mínimo", "Precio"]
    if is_admin():
        cols.insert(-1, "costo"); heads.insert(-1, "Costo")
    html_table(tab, cols, heads, 300)


def page_ingreso_mercaderia():
    st.markdown("<div class='clomar-hero'><h1>📥 Ingreso de mercadería</h1><p>Formulario claro para aumentar stock y registrar pago al proveedor.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede ingresar mercadería.")
        return
    productos = productos_con_stock()
    if productos.empty:
        st.info("Crea productos antes de registrar mercadería.")
        return
    st.markdown("<div class='card'>", unsafe_allow_html=True)
    with st.form("form_compra_v248"):
        st.markdown("### Nueva entrada de stock")
        c1, c2 = st.columns([1, 1])
        with c1:
            proveedor = st.text_input("Proveedor", placeholder="Nombre del proveedor")
            producto_label = st.selectbox("Producto", [f"{r['id_producto']} - {r['nombre_producto']} | Stock {num(r['stock_actual'])}" for _, r in productos.iterrows()])
            idp = int(producto_label.split(" - ")[0])
            cantidad = st.number_input("Cantidad a ingresar", min_value=0.0, step=1.0)
        with c2:
            costo = st.number_input("Costo unitario", min_value=0.0, step=1.0)
            total = cantidad * costo
            metodo = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Crédito"])
            pagado = st.number_input("Monto pagado", min_value=0.0, value=float(total if metodo != "Crédito" else 0), step=1.0)
        obs = st.text_area("Observación", placeholder="Factura, guía, nota del proveedor...")
        saldo = max(total - pagado, 0)
        st.markdown(f"<div class='kpi-card'><div class='kpi-label'>Resumen de compra</div><div class='kpi-value'>{money(total)}</div><div class='kpi-sub'>Pagado {money(pagado)} · Saldo {money(saldo)}</div></div>", unsafe_allow_html=True)
        if st.form_submit_button("Registrar ingreso de mercadería", type="primary", use_container_width=True):
            if cantidad <= 0:
                st.error("La cantidad debe ser mayor a cero.")
            else:
                u = current_user()
                saldo = max(total - pagado, 0)
                estado = "Pagada" if saldo <= 0 else ("Parcial" if pagado > 0 else "Pendiente")
                with ENGINE.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO compras (proveedor, metodo_pago, total_compra, monto_pagado, saldo_pendiente, estado_pago, id_usuario, observacion)
                        VALUES (:prov, :metodo, :total, :pagado, :saldo, :estado, :uid, :obs)
                    """), {"prov": proveedor, "metodo": metodo, "total": total, "pagado": pagado, "saldo": saldo, "estado": estado, "uid": u["id_usuario"], "obs": obs})
                    idc = conn.execute(text("SELECT MAX(id_compra) FROM compras WHERE id_usuario=:uid"), {"uid": u["id_usuario"]}).scalar()
                    conn.execute(text("INSERT INTO detalle_compras (id_compra, id_producto, cantidad, costo_unitario, subtotal) VALUES (:idc, :idp, :cant, :costo, :sub)"), {"idc": int(idc), "idp": idp, "cant": cantidad, "costo": costo, "sub": total})
                    conn.execute(text("INSERT INTO movimientos_stock (id_producto, tipo, cantidad, costo_unitario, referencia, id_usuario, observacion) VALUES (:idp, 'ENTRADA_COMPRA', :cant, :costo, :ref, :uid, :obs)"), {"idp": idp, "cant": cantidad, "costo": costo, "ref": f"COMPRA {idc}", "uid": u["id_usuario"], "obs": obs})
                    conn.execute(text("UPDATE productos SET costo_unitario=:costo, actualizado_en=CURRENT_TIMESTAMP WHERE id_producto=:idp"), {"costo": costo, "idp": idp})
                    if pagado > 0:
                        conn.execute(text("INSERT INTO caja (tipo, concepto, metodo_pago, monto, referencia, id_usuario, observacion) VALUES ('Egreso', :concepto, :metodo, :monto, :ref, :uid, :obs)"), {"concepto": f"Compra mercadería {idc}", "metodo": metodo, "monto": pagado, "ref": f"COMPRA {idc}", "uid": u["id_usuario"], "obs": obs})
                st.success("Ingreso de mercadería registrado.")
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def page_clientes():
    st.markdown("<div class='clomar-hero'><h1>👥 Clientes</h1><p>Ficha comercial, compras y saldos pendientes.</p></div>", unsafe_allow_html=True)
    with st.expander("Crear cliente"):
        with st.form("form_cliente"):
            nombre = st.text_input("Nombre del cliente")
            telefono = st.text_input("Teléfono")
            documento = st.text_input("Documento")
            direccion = st.text_input("Dirección")
            if st.form_submit_button("Guardar cliente", type="primary"):
                if nombre.strip():
                    exec_sql("INSERT INTO clientes (nombre_cliente, telefono, documento, direccion, estado) VALUES (:n,:t,:d,:dir,'Activo')", {"n": nombre, "t": telefono, "d": documento, "dir": direccion})
                    st.success("Cliente guardado.")
                    st.rerun()
                else:
                    st.error("Ingresa el nombre.")
    clientes = query_df("""
        SELECT
            c.id_cliente, c.nombre_cliente, c.telefono, c.documento, c.direccion,
            c.observacion, c.limite_credito, c.estado, c.creado_en,
            COALESCE(SUM(v.total_venta),0) AS total_comprado,
            COALESCE(SUM(v.saldo_pendiente),0) AS total_por_cobrar,
            COUNT(v.id_venta) AS ventas
        FROM clientes c
        LEFT JOIN ventas v ON v.id_cliente=c.id_cliente AND COALESCE(v.anulada,0)=0
        GROUP BY c.id_cliente, c.nombre_cliente, c.telefono, c.documento, c.direccion,
                 c.observacion, c.limite_credito, c.estado, c.creado_en
        ORDER BY c.nombre_cliente
    """)
    if clientes.empty:
        st.info("No hay clientes.")
    else:
        cols = st.columns(3)
        for i, (_, r) in enumerate(clientes.iterrows()):
            with cols[i % 3]:
                cobrar = float(r["total_por_cobrar"] or 0)
                chip = "chip-red" if cobrar > 0 else "chip-ok"
                st.markdown(f"""
                <div class='card'><h3>👤 {r['nombre_cliente']}</h3>
                <div class='product-meta'>{r.get('telefono','')} · {r.get('documento','')}</div>
                <span class='chip chip-dark'>Comprado {money(r['total_comprado'])}</span>
                <span class='chip {chip}'>Por cobrar {money(cobrar)}</span>
                <span class='chip'>Ventas {int(r['ventas'])}</span></div>
                """, unsafe_allow_html=True)


def page_caja():
    st.markdown("<div class='clomar-hero'><h1>💰 Caja</h1><p>Movimientos, ingresos, egresos y cierre del día.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede ver caja.")
        return
    f = st.date_input("Fecha", date.today())
    caja = query_df("SELECT * FROM caja WHERE date(fecha)=:f AND COALESCE(anulada,0)=0 ORDER BY fecha DESC", {"f": str(f)})
    ingresos = caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum() if not caja.empty else 0
    egresos = caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum() if not caja.empty else 0
    a,b,c = st.columns(3)
    with a: kpi("Ingresos", money(ingresos))
    with b: kpi("Egresos", money(egresos))
    with c: kpi("Saldo neto", money(ingresos-egresos))
    if not caja.empty:
        ctab = caja.copy(); ctab["monto_fmt"] = ctab["monto"].apply(money)
        html_table(ctab, ["fecha", "tipo", "concepto", "metodo_pago", "monto_fmt", "referencia"], ["Fecha", "Tipo", "Concepto", "Pago", "Monto", "Ref"], 200)
    with st.expander("Registrar egreso / salida"):
        with st.form("form_egreso"):
            concepto = st.text_input("Concepto")
            monto = st.number_input("Monto", min_value=0.0, step=1.0)
            metodo = st.selectbox("Método", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta"])
            if st.form_submit_button("Registrar egreso", type="primary"):
                u=current_user()
                exec_sql("INSERT INTO caja (tipo, concepto, metodo_pago, monto, id_usuario) VALUES ('Egreso', :c, :m, :mo, :u)", {"c":concepto, "m":metodo, "mo":monto, "u":u["id_usuario"]})
                st.success("Egreso registrado.")
                st.rerun()


def page_reportes():
    st.markdown("<div class='clomar-hero'><h1>📈 Reportes</h1><p>Ventas por fecha, vendedor, producto y método de pago.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede ver reportes.")
        return
    c1,c2=st.columns(2)
    with c1: desde=st.date_input("Desde", date.today()-timedelta(days=7), key="rep_d")
    with c2: hasta=st.date_input("Hasta", date.today(), key="rep_h")
    ventas=ventas_periodo(desde,hasta); detalle=detalle_productos_vendidos(desde,hasta)
    if ventas.empty:
        st.info("No hay ventas en el rango.")
        return
    ventas["dia"] = pd.to_datetime(ventas["fecha"]).dt.date
    diario = ventas.groupby("dia", as_index=False)["total_venta"].sum()
    fig = px.line(diario, x="dia", y="total_venta", markers=True, title="Ventas por día", template="plotly_white")
    fig.update_layout(paper_bgcolor="white", plot_bgcolor="white", font_color="#111827")
    st.plotly_chart(fig, use_container_width=True)
    a,b=st.columns(2)
    with a:
        metodo = ventas.groupby("metodo_pago", as_index=False)["total_venta"].sum()
        fig_m = px.pie(metodo, names="metodo_pago", values="total_venta", title="Métodos de pago", template="plotly_white")
        fig_m.update_layout(paper_bgcolor="white", font_color="#111827")
        st.plotly_chart(fig_m, use_container_width=True)
    with b:
        vendedor = ventas.groupby("vendedor_nombre", as_index=False)["total_venta"].sum()
        fig_v = px.bar(vendedor, x="vendedor_nombre", y="total_venta", title="Ventas por vendedor", template="plotly_white")
        fig_v.update_layout(paper_bgcolor="white", plot_bgcolor="white", font_color="#111827")
        st.plotly_chart(fig_v, use_container_width=True)
    if not detalle.empty:
        detalle["total"] = detalle["total_vendido"].apply(money)
        detalle["utilidad_fmt"] = detalle["utilidad"].apply(money)
        html_table(detalle, ["vendedor","producto","cantidad","total","utilidad_fmt"], ["Vendedor","Producto","Cantidad","Total","Utilidad"], 100)


def page_usuarios():
    st.markdown("<div class='clomar-hero'><h1>🔐 Usuarios</h1><p>Controla accesos, roles, estado y contraseñas.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede gestionar usuarios.")
        return
    users=query_df("SELECT id_usuario, usuario, nombre, rol, estado, creado_en FROM usuarios ORDER BY id_usuario")
    tab1, tab2, tab3 = st.tabs(["Lista", "Editar / contraseña", "Crear usuario"])
    with tab1:
        html_table(users, ["usuario","nombre","rol","estado","creado_en"], ["Usuario","Nombre","Rol","Estado","Creado"], 100)
    with tab2:
        if users.empty:
            st.info("No hay usuarios para editar.")
        else:
            opciones = {f"{r['usuario']} · {r['nombre']} · {r['rol']}": r for _, r in users.iterrows()}
            elegido = st.selectbox("Selecciona usuario", list(opciones.keys()))
            r = opciones[elegido]
            with st.form("form_edit_user_v248"):
                nombre = st.text_input("Nombre visible", value=str(r['nombre']))
                roles = ["Vendedor", "Administrador", "Supervisor"]
                rol = st.selectbox("Rol", roles, index=roles.index(str(r['rol'])) if str(r['rol']) in roles else 0)
                estado = st.selectbox("Estado", ["Activo", "Inactivo"], index=0 if str(r['estado']) == "Activo" else 1)
                nueva_clave = st.text_input("Nueva contraseña", type="password", placeholder="Déjalo vacío para no cambiar")
                if st.form_submit_button("Actualizar usuario", type="primary", use_container_width=True):
                    params = {"id": int(r['id_usuario']), "nombre": nombre or str(r['usuario']), "rol": rol, "estado": estado}
                    if nueva_clave.strip():
                        params["password_hash"] = hash_password(nueva_clave)
                        exec_sql("UPDATE usuarios SET nombre=:nombre, rol=:rol, estado=:estado, password_hash=:password_hash WHERE id_usuario=:id", params)
                    else:
                        exec_sql("UPDATE usuarios SET nombre=:nombre, rol=:rol, estado=:estado WHERE id_usuario=:id", params)
                    st.success("Usuario actualizado.")
                    st.rerun()
    with tab3:
        with st.form("form_user_v248"):
            usuario=st.text_input("Usuario")
            nombre=st.text_input("Nombre")
            clave=st.text_input("Contraseña", type="password")
            rol=st.selectbox("Rol", ["Vendedor", "Administrador", "Supervisor"], key="rol_new_user")
            if st.form_submit_button("Crear usuario", type="primary", use_container_width=True):
                if not usuario or not clave:
                    st.error("Usuario y contraseña son obligatorios.")
                else:
                    try:
                        exec_sql("INSERT INTO usuarios (usuario,password_hash,nombre,rol,estado) VALUES (:u,:p,:n,:r,'Activo')", {"u":usuario,"p":hash_password(clave),"n":nombre or usuario,"r":rol})
                        st.success("Usuario creado.")
                        st.rerun()
                    except Exception:
                        st.error("No se pudo crear. Quizás el usuario ya existe.")


def page_backup():
    st.markdown("<div class='clomar-hero'><h1>💾 Backup / exportación</h1><p>Descarga datos de la nube en CSV para respaldo.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede descargar backups.")
        return
    tablas=["usuarios","categorias","clientes","proveedores","productos","movimientos_stock","ventas","detalle_ventas","compras","detalle_compras","caja","egresos","cierres_caja"]
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as z:
        for t in tablas:
            try:
                df=query_df(f"SELECT * FROM {t}")
                z.writestr(f"{t}.csv", df.to_csv(index=False).encode("utf-8-sig"))
            except Exception:
                pass
    buffer.seek(0)
    st.download_button("Descargar backup ZIP", data=buffer, file_name=f"clomar_store_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip", mime="application/zip", type="primary")
    st.info("Este ZIP no contiene tu contraseña de Neon. Solo exporta datos de negocio.")



def page_estado_nube():
    st.markdown("<div class='clomar-hero'><h1>☁️ Estado nube</h1><p>Verifica si la app trabaja local o conectada a PostgreSQL Neon.</p></div>", unsafe_allow_html=True)
    if IS_POSTGRES:
        st.success("Modo nube PostgreSQL activo.")
    else:
        st.warning("Modo local SQLite activo. En Streamlit Cloud debes configurar NEON_DATABASE_URL en Secrets.")
    st.code("NEON_DATABASE_URL = postgresql://...?...sslmode=require", language="toml")
    st.write("Versión:", APP_VERSION)


def page_configuracion_tienda():
    st.markdown("<div class='clomar-hero'><h1>⚙️ Configuración de tienda</h1><p>Personaliza logo, datos comerciales y comprobante.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede cambiar la configuración de la tienda.")
        return
    cfg = get_store_config()
    c1, c2 = st.columns([1.2, .8])
    with c1:
        with st.form("form_config_tienda"):
            nombre = st.text_input("Nombre de la tienda", value=cfg.get("nombre_tienda", APP_NAME))
            logo_url = st.text_input("Logo URL", value=cfg.get("logo_url", ""), placeholder="https://.../logo.png")
            telefono = st.text_input("Teléfono / WhatsApp", value=cfg.get("telefono", ""))
            direccion = st.text_area("Dirección", value=cfg.get("direccion", ""))
            mensaje = st.text_input("Mensaje del comprobante", value=cfg.get("mensaje_comprobante", "Gracias por su compra."))
            color = st.color_picker("Color principal", value=cfg.get("color_principal", "#111827") or "#111827")
            catalogo_url_base = st.text_input("URL base de la app para catálogo público", value=cfg.get("catalogo_url_base", get_secret("PUBLIC_APP_URL", "https://clomar-store.streamlit.app")))
            if st.form_submit_button("Guardar configuración", type="primary", use_container_width=True):
                set_store_config({
                    "nombre_tienda": nombre,
                    "logo_url": logo_url,
                    "telefono": telefono,
                    "direccion": direccion,
                    "mensaje_comprobante": mensaje,
                    "color_principal": color,
                    "catalogo_url_base": catalogo_url_base,
                })
                clear_app_cache()
                st.success("Configuración guardada.")
                st.rerun()
    with c2:
        st.subheader("Vista previa")
        if cfg.get("logo_url"):
            st.image(cfg.get("logo_url"), width=120)
        st.markdown(f"""
        <div class='card'>
          <h3>{html.escape(cfg.get('nombre_tienda', APP_NAME))}</h3>
          <div class='product-meta'>{html.escape(cfg.get('direccion',''))}</div>
          <div class='product-meta'>{html.escape(cfg.get('telefono',''))}</div>
          <span class='chip chip-dark'>Comprobante</span>
          <p>{html.escape(cfg.get('mensaje_comprobante','Gracias por su compra.'))}</p>
        </div>
        """, unsafe_allow_html=True)
        st.info("Para usar tu logo, pega un enlace directo de imagen. Más adelante podemos integrar un almacenamiento dedicado para fotos y logos.")
        st.link_button("Abrir catálogo público", public_catalog_url(), use_container_width=True)


# ============================================================
# NAVEGACIÓN
# ============================================================
def sidebar_nav():
    u = current_user()
    cfg = get_store_config()
    st.sidebar.markdown(f"### {logo_or_icon(26)} {html.escape(cfg.get('nombre_tienda', APP_NAME))}", unsafe_allow_html=True)
    st.sidebar.caption(f"{u['nombre']} · {u['rol']}")
    if is_admin():
        opciones = [
            "📊 Panel dueño", "🧾 Ventas", "📦 Productos", "📘 Catálogo clientes", "📊 Inventario", "📥 Ingreso mercadería", "👥 Clientes", "💰 Caja", "📈 Reportes", "🔐 Usuarios", "⚙️ Configuración", "💾 Backup", "☁️ Estado nube"
        ]
    else:
        opciones = ["🧾 Ventas", "📦 Productos", "📘 Catálogo clientes", "👥 Clientes"]
    selected = st.sidebar.radio("Menú", opciones, label_visibility="collapsed")
    st.sidebar.divider()
    if st.sidebar.button("Cerrar sesión", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    return selected


try:
    _public_catalog = str(st.query_params.get("catalogo", "")) == "1"
except Exception:
    _public_catalog = False

if _public_catalog:
    page_catalogo_clientes(public=True)
elif "user" not in st.session_state:
    login_screen()
else:
    selected = sidebar_nav()
    if selected == "📊 Panel dueño": page_panel_dueno()
    elif selected == "🧾 Ventas": page_ventas()
    elif selected == "📦 Productos": page_productos()
    elif selected == "📘 Catálogo clientes": page_catalogo_clientes(public=False)
    elif selected == "📊 Inventario": page_inventario()
    elif selected == "📥 Ingreso mercadería": page_ingreso_mercaderia()
    elif selected == "👥 Clientes": page_clientes()
    elif selected == "💰 Caja": page_caja()
    elif selected == "📈 Reportes": page_reportes()
    elif selected == "🔐 Usuarios": page_usuarios()
    elif selected == "⚙️ Configuración": page_configuracion_tienda()
    elif selected == "💾 Backup": page_backup()
    elif selected == "☁️ Estado nube": page_estado_nube()
