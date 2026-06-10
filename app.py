import os
import io
import re
import zipfile
import hashlib
import secrets
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import create_engine, text

try:
    import requests
except Exception:
    requests = None

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
except Exception:
    colors = None

APP_VERSION = "V29.1 POS rápido sin lag - buscador desplegable y compras automáticas"
APP_NAME_DEFAULT = "Clomar Store"

st.set_page_config(
    page_title="Clomar Store",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# V27 PWA / APP INSTALABLE
# ============================================================
PWA_MANIFEST_URL = "https://raw.githubusercontent.com/CLOMARstore/clomar-store/main/static/manifest.json"
PWA_ICON_URL = "https://raw.githubusercontent.com/CLOMARstore/clomar-store/main/static/icon-192.png"
PWA_ICON_512_URL = "https://raw.githubusercontent.com/CLOMARstore/clomar-store/main/static/icon-512.png"


def inject_pwa_assets():
    """Inyecta manifest e iconos para que Chrome/Edge puedan sugerir instalación.
    Esta variante usa RAW de GitHub porque en algunas apps de Streamlit Cloud /app/static/ no carga de forma confiable.
    """
    try:
        st.markdown(
            f"""
            <link rel="manifest" href="{PWA_MANIFEST_URL}" crossorigin="anonymous">
            <meta name="theme-color" content="#111827">
            <link rel="icon" type="image/png" sizes="192x192" href="{PWA_ICON_URL}">
            <link rel="shortcut icon" type="image/png" href="{PWA_ICON_URL}">
            <link rel="apple-touch-icon" href="{PWA_ICON_512_URL}">
            """,
            unsafe_allow_html=True,
        )
        components.html(
            f"""
            <script>
            (function() {{
              try {{
                const d = window.parent.document;
                function upsert(tag, attr, value, attrs) {{
                  let el = d.querySelector(tag + '[' + attr + '="' + value + '"]');
                  if (!el) {{
                    el = d.createElement(tag);
                    el.setAttribute(attr, value);
                    d.head.appendChild(el);
                  }}
                  for (const k in attrs) el.setAttribute(k, attrs[k]);
                }}
                d.title = 'Clomar Store';
                upsert('link', 'rel', 'manifest', {{href: '{PWA_MANIFEST_URL}', crossorigin: 'anonymous'}});
                upsert('link', 'rel', 'icon', {{href: '{PWA_ICON_URL}', type:'image/png', sizes:'192x192'}});
                upsert('link', 'rel', 'shortcut icon', {{href: '{PWA_ICON_URL}', type:'image/png'}});
                upsert('link', 'rel', 'apple-touch-icon', {{href: '{PWA_ICON_512_URL}'}});
                let theme = d.querySelector('meta[name="theme-color"]');
                if (!theme) {{ theme = d.createElement('meta'); theme.setAttribute('name','theme-color'); d.head.appendChild(theme); }}
                theme.setAttribute('content', '#111827');
                let desc = d.querySelector('meta[name="description"]');
                if (!desc) {{ desc = d.createElement('meta'); desc.setAttribute('name','description'); d.head.appendChild(desc); }}
                desc.setAttribute('content', 'Clomar Store POS: ventas, inventario, caja, créditos y reportes.');
              }} catch(e) {{ console.log('PWA inject fallback', e); }}
            }})();
            </script>
            """,
            height=0,
        )
    except Exception:
        pass


inject_pwa_assets()


# Ajustes móviles V27: reduce anchos, tarjetas y botones cuando se abre como app en celular.
st.markdown("""
<style>
@media (max-width: 760px) {
  .block-container { padding-left: .75rem !important; padding-right: .75rem !important; padding-top: .75rem !important; }
  .hero, .card { border-radius: 16px !important; }
  .metric-card h1, .metric-card .big, .kpi-value { font-size: 1.65rem !important; }
  .product-grid, .catalog-grid { grid-template-columns: 1fr !important; gap: 12px !important; }
  .product-card { padding: 12px !important; }
  .product-img-wrap { height: 150px !important; }
  .ticket-pro { max-width: 100% !important; }
  div[data-testid="stHorizontalBlock"] { gap: .7rem !important; }
  .stButton > button { min-height: 44px !important; }
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# DB / CONFIG
# ============================================================
def get_secret(name: str, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)


def database_url():
    return get_secret("NEON_DATABASE_URL") or get_secret("DATABASE_URL") or "sqlite:///clomar_store_cloud_local.db"


DB_URL = database_url()
IS_POSTGRES = DB_URL.startswith("postgresql")
IS_LOCAL_SQLITE = DB_URL.startswith("sqlite")


@st.cache_resource(show_spinner=False)
def get_engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False, "timeout": 60})
    return create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=5, pool_recycle=1800)


ENGINE = get_engine(DB_URL)


def id_sql():
    return "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def exec_sql(sql: str, params: dict | None = None):
    with ENGINE.begin() as conn:
        return conn.execute(text(sql), params or {})


def query_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    with ENGINE.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def scalar(sql: str, params: dict | None = None, default=0):
    try:
        with ENGINE.connect() as conn:
            v = conn.execute(text(sql), params or {}).scalar()
        return default if v is None else v
    except Exception:
        return default


def safe_alter(table: str, column_sql: str):
    try:
        exec_sql(f"ALTER TABLE {table} ADD COLUMN {column_sql}")
    except Exception:
        pass


@st.cache_resource(show_spinner=False)
def init_db():
    ddl = [
        f"""
        CREATE TABLE IF NOT EXISTS usuarios (
            id_usuario {id_sql()},
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
            id_categoria {id_sql()},
            nombre_categoria VARCHAR(160) UNIQUE NOT NULL,
            descripcion TEXT DEFAULT '',
            estado VARCHAR(30) DEFAULT 'Activo',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS clientes (
            id_cliente {id_sql()},
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
            id_proveedor {id_sql()},
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
            id_producto {id_sql()},
            codigo VARCHAR(80) UNIQUE,
            nombre_producto VARCHAR(240) NOT NULL,
            id_categoria INTEGER,
            marca VARCHAR(120) DEFAULT '',
            descripcion TEXT DEFAULT '',
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
        CREATE TABLE IF NOT EXISTS movimientos_stock (
            id_movimiento {id_sql()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            id_producto INTEGER,
            tipo VARCHAR(60),
            cantidad NUMERIC(12,2) DEFAULT 0,
            costo_unitario NUMERIC(12,2) DEFAULT 0,
            referencia VARCHAR(120) DEFAULT '',
            id_usuario INTEGER,
            observacion TEXT DEFAULT ''
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS ventas (
            id_venta {id_sql()},
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
            id_detalle {id_sql()},
            id_venta INTEGER,
            id_producto INTEGER,
            producto_nombre VARCHAR(240),
            cantidad NUMERIC(12,2),
            precio_unitario NUMERIC(12,2),
            costo_unitario NUMERIC(12,2),
            subtotal NUMERIC(12,2)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS caja (
            id_caja {id_sql()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tipo VARCHAR(60) DEFAULT 'Ingreso',
            concepto TEXT,
            metodo_pago VARCHAR(60),
            monto NUMERIC(12,2) DEFAULT 0,
            referencia VARCHAR(120) DEFAULT '',
            id_usuario INTEGER,
            observacion TEXT DEFAULT '',
            anulada INTEGER DEFAULT 0
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS pagos_credito (
            id_pago {id_sql()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            id_venta INTEGER NOT NULL,
            id_cliente INTEGER,
            metodo_pago VARCHAR(60) DEFAULT 'Efectivo',
            monto NUMERIC(12,2) DEFAULT 0,
            referencia VARCHAR(120) DEFAULT '',
            id_usuario INTEGER,
            observacion TEXT DEFAULT ''
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS compras (
            id_compra {id_sql()},
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            proveedor VARCHAR(200) DEFAULT '',
            total_compra NUMERIC(12,2) DEFAULT 0,
            monto_pagado NUMERIC(12,2) DEFAULT 0,
            metodo_pago VARCHAR(60) DEFAULT 'Efectivo',
            observacion TEXT DEFAULT '',
            id_usuario INTEGER
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS ajustes (
            clave VARCHAR(120) PRIMARY KEY,
            valor TEXT DEFAULT ''
        )
        """,
    ]
    for s in ddl:
        exec_sql(s)

    # Índices para que la nube responda mejor en ventas, créditos, caja e inventario.
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_productos_codigo ON productos(codigo)",
        "CREATE INDEX IF NOT EXISTS idx_productos_nombre ON productos(nombre_producto)",
        "CREATE INDEX IF NOT EXISTS idx_mov_stock_producto ON movimientos_stock(id_producto)",
        "CREATE INDEX IF NOT EXISTS idx_ventas_fecha ON ventas(fecha)",
        "CREATE INDEX IF NOT EXISTS idx_ventas_saldo ON ventas(saldo_pendiente)",
        "CREATE INDEX IF NOT EXISTS idx_detalle_venta ON detalle_ventas(id_venta)",
        "CREATE INDEX IF NOT EXISTS idx_caja_fecha ON caja(fecha)",
        "CREATE INDEX IF NOT EXISTS idx_pagos_credito_venta ON pagos_credito(id_venta)",
    ]:
        try:
            exec_sql(idx)
        except Exception:
            pass

    # Migraciones suaves para tablas antiguas
    safe_alter("productos", "descripcion TEXT DEFAULT ''")
    safe_alter("productos", "imagen_url TEXT DEFAULT ''")
    safe_alter("productos", "actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    safe_alter("ventas", "anulada INTEGER DEFAULT 0")
    safe_alter("ventas", "fecha_vencimiento DATE")
    safe_alter("ventas", "tipo_venta VARCHAR(40) DEFAULT 'Contado'")
    safe_alter("caja", "anulada INTEGER DEFAULT 0")

    if scalar("SELECT COUNT(*) FROM usuarios") == 0:
        exec_sql("""
            INSERT INTO usuarios (usuario,password_hash,nombre,rol,estado)
            VALUES (:u,:p,:n,:r,'Activo')
        """, {"u": "admin", "p": hash_password("admin123"), "n": "Administrador", "r": "Administrador"})
        exec_sql("""
            INSERT INTO usuarios (usuario,password_hash,nombre,rol,estado)
            VALUES (:u,:p,:n,:r,'Activo')
        """, {"u": "vendedor", "p": hash_password("venta123"), "n": "Vendedor", "r": "Vendedor"})

    for k, v in {
        "store_name": "Clomar Store",
        "logo_url": "",
        "icon_url": "",
        "telefono": "",
        "direccion": "",
        "mensaje_comprobante": "Gracias por su compra.",
        "color_principal": "#E8A06D",
        "catalogo_base_url": "https://clomar-store.streamlit.app",
        "imagenes_base_url": "https://raw.githubusercontent.com/CLOMARstore/clomar-store/main/imagenes_productos",
        "whatsapp_codigo_pais": "51",
    }.items():
        try:
            exec_sql("INSERT INTO ajustes (clave, valor) VALUES (:k,:v)", {"k": k, "v": v})
        except Exception:
            pass


# ============================================================
# SEGURIDAD / UTILIDADES
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


# Hora comercial de Perú para mostrar comprobantes y reportes.
PERU_TZ = ZoneInfo("America/Lima")
UTC_TZ = ZoneInfo("UTC")


def peru_now() -> datetime:
    return datetime.now(PERU_TZ)


def peru_today() -> date:
    return peru_now().date()


def _to_peru_dt(value):
    try:
        dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            return None
        py = dt.to_pydatetime()
        # Neon/Streamlit normalmente guarda CURRENT_TIMESTAMP en UTC sin zona.
        # Para presentación comercial lo mostramos en America/Lima.
        if py.tzinfo is None:
            py = py.replace(tzinfo=UTC_TZ)
        return py.astimezone(PERU_TZ)
    except Exception:
        return None


def fmt_date(value) -> str:
    dt = _to_peru_dt(value)
    return dt.strftime("%d/%m/%Y") if dt else str(value or "")


def fmt_time(value) -> str:
    dt = _to_peru_dt(value)
    if not dt:
        return ""
    return dt.strftime("%I:%M %p").replace("AM", "a. m.").replace("PM", "p. m.")


def fmt_dt(value) -> str:
    dt = _to_peru_dt(value)
    if not dt:
        return str(value or "")
    return dt.strftime("%d/%m/%Y %I:%M %p").replace("AM", "a. m.").replace("PM", "p. m.")


def esc(x):
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def settings() -> dict:
    try:
        df = query_df("SELECT clave, valor FROM ajustes")
        return {r["clave"]: r["valor"] for _, r in df.iterrows()}
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def cached_settings() -> dict:
    return settings()


def get_setting(k, default=""):
    return cached_settings().get(k, default)


def set_setting(k, v):
    try:
        exec_sql("UPDATE ajustes SET valor=:v WHERE clave=:k", {"k": k, "v": str(v or "")})
        if scalar("SELECT COUNT(*) FROM ajustes WHERE clave=:k", {"k": k}) == 0:
            exec_sql("INSERT INTO ajustes (clave, valor) VALUES (:k,:v)", {"k": k, "v": str(v or "")})
        cached_settings.clear()
    except Exception:
        pass


def current_user():
    return st.session_state.get("user")


def is_admin():
    u = current_user() or {}
    return str(u.get("rol", "")).lower() in ["administrador", "admin", "dueño", "propietario", "supervisor"]


def user_is_vendor():
    return not is_admin()


def normalize_code(c):
    s = str(c or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def code_match_key(c):
    """Clave normalizada para evitar duplicados: 3, 03 y 0003 se tratan como el mismo producto."""
    s = normalize_code(c)
    if not s:
        return ""
    if s.isdigit():
        return s.lstrip("0") or "0"
    return s.lower()


def find_product_id_by_code_conn(conn, codigo: str):
    codigo = normalize_code(codigo)
    if not codigo:
        return None
    # Exacto primero; luego equivalente sin ceros a la izquierda. Funciona en PostgreSQL y SQLite.
    return conn.execute(text("""
        SELECT id_producto FROM productos
        WHERE COALESCE(codigo,'') = :c
           OR LOWER(COALESCE(codigo,'')) = LOWER(:c)
           OR LTRIM(COALESCE(codigo,''), '0') = LTRIM(:c, '0')
        ORDER BY
           CASE WHEN COALESCE(codigo,'') = :c THEN 0 ELSE 1 END,
           CASE WHEN COALESCE(estado,'Activo') = 'Activo' THEN 0 ELSE 1 END,
           LENGTH(COALESCE(codigo,'')) DESC,
           id_producto ASC
        LIMIT 1
    """), {"c": codigo}).scalar()


def merge_duplicate_products_by_code(conn=None):
    """Une productos duplicados por código normalizado sin borrar historial.
    Mueve stock y detalle de ventas al producto principal e inactiva duplicados.
    """
    own = conn is None
    if own:
        conn_ctx = ENGINE.begin()
        conn = conn_ctx.__enter__()
    try:
        rows = conn.execute(text("""
            SELECT id_producto, codigo, nombre_producto, estado
            FROM productos
            WHERE COALESCE(codigo,'') <> ''
            ORDER BY id_producto
        """)).mappings().all()
        groups = {}
        for r in rows:
            k = code_match_key(r.get('codigo'))
            if k:
                groups.setdefault(k, []).append(dict(r))
        merged = 0
        for k, items in groups.items():
            if len(items) <= 1:
                continue
            # Conserva como principal el código más completo, por ejemplo 0003 antes que 3.
            items_sorted = sorted(items, key=lambda x: (0 if str(x.get('estado') or 'Activo') == 'Activo' else 1, -len(str(x.get('codigo') or '')), int(x.get('id_producto') or 0)))
            primary = items_sorted[0]
            primary_id = int(primary['id_producto'])
            for dup in items_sorted[1:]:
                dup_id = int(dup['id_producto'])
                old_code = str(dup.get('codigo') or '')
                conn.execute(text("UPDATE movimientos_stock SET id_producto=:p WHERE id_producto=:d"), {"p": primary_id, "d": dup_id})
                conn.execute(text("UPDATE detalle_ventas SET id_producto=:p WHERE id_producto=:d"), {"p": primary_id, "d": dup_id})
                conn.execute(text("""
                    UPDATE productos
                    SET estado='Inactivo', codigo=:new_code, actualizado_en=CURRENT_TIMESTAMP
                    WHERE id_producto=:d
                """), {"new_code": f"{old_code}-DUP-{dup_id}", "d": dup_id})
                merged += 1
        if own:
            conn_ctx.__exit__(None, None, None)
        return merged
    except Exception:
        if own:
            conn_ctx.__exit__(*__import__('sys').exc_info())
        raise


def wa_link(producto: str = ""):
    tel = re.sub(r"\D+", "", str(get_setting("telefono", "")))
    if tel and not tel.startswith("51") and len(tel) == 9:
        tel = str(get_setting("whatsapp_codigo_pais", "51")) + tel
    msg = f"Hola, deseo consultar por {producto}" if producto else "Hola, deseo consultar productos de Clomar Store"
    return f"https://wa.me/{tel}?text={quote(msg)}" if tel else "#"


def product_image_url(row_or_dict) -> str:
    d = dict(row_or_dict)
    url = str(d.get("imagen_url") or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    codigo = normalize_code(d.get("codigo"))
    if not codigo:
        return ""
    base = str(get_setting("imagenes_base_url", "")).strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/{codigo}.jpg"


def product_image_candidates(row_or_dict) -> list[str]:
    d = dict(row_or_dict)
    explicit = str(d.get("imagen_url") or "").strip()
    if explicit.startswith("http://") or explicit.startswith("https://"):
        return [explicit]
    codigo = normalize_code(d.get("codigo"))
    base = str(get_setting("imagenes_base_url", "")).strip().rstrip("/")
    if not codigo or not base:
        return []
    # Soporta nombres habituales: 0001.jpg, 0001.png, 0001.jpeg, 0001.JPG
    return [f"{base}/{codigo}.jpg", f"{base}/{codigo}.png", f"{base}/{codigo}.jpeg", f"{base}/{codigo}.JPG"]


def stock_expr_sql():
    return """
        COALESCE(SUM(CASE
            WHEN ms.tipo IN ('ENTRADA','ENTRADA_COMPRA','AJUSTE_POSITIVO') THEN ms.cantidad
            WHEN ms.tipo IN ('SALIDA','SALIDA_VENTA','AJUSTE_NEGATIVO') THEN -ms.cantidad
            WHEN ms.tipo = 'AJUSTE' THEN ms.cantidad
            ELSE 0 END),0)
    """


@st.cache_data(ttl=120, show_spinner=False)
def productos_con_stock_cached():
    return query_df(f"""
        SELECT p.*, COALESCE(c.nombre_categoria,'Sin categoría') AS categoria,
               {stock_expr_sql()} AS stock_actual
        FROM productos p
        LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
        LEFT JOIN movimientos_stock ms ON ms.id_producto=p.id_producto
        WHERE COALESCE(p.estado,'Activo')='Activo'
        GROUP BY p.id_producto, p.codigo, p.nombre_producto, p.id_categoria, p.marca, p.descripcion,
                 p.unidad, p.costo_unitario, p.precio_venta, p.stock_minimo, p.imagen_url, p.estado,
                 p.creado_en, p.actualizado_en, c.nombre_categoria
        ORDER BY p.nombre_producto
    """)


def productos_con_stock():
    return productos_con_stock_cached().copy()


def clear_product_cache():
    productos_con_stock_cached.clear()


@st.cache_data(ttl=60, show_spinner=False)
def ventas_periodo_cached(desde: str, hasta: str):
    return query_df("""
        SELECT v.*, COALESCE(c.nombre_cliente,'Cliente general') AS cliente
        FROM ventas v
        LEFT JOIN clientes c ON c.id_cliente=v.id_cliente
        WHERE DATE(v.fecha) BETWEEN :d AND :h AND COALESCE(v.anulada,0)=0
        ORDER BY v.fecha DESC
    """, {"d": desde, "h": hasta})


def ventas_periodo(desde, hasta):
    return ventas_periodo_cached(str(desde), str(hasta)).copy()


@st.cache_data(ttl=60, show_spinner=False)
def detalle_productos_vendidos_cached(desde: str, hasta: str):
    return query_df("""
        SELECT v.vendedor_nombre AS vendedor, dv.producto_nombre AS producto,
               SUM(dv.cantidad) AS cantidad,
               SUM(dv.subtotal) AS total_vendido,
               SUM((dv.precio_unitario - COALESCE(dv.costo_unitario,0))*dv.cantidad) AS utilidad
        FROM detalle_ventas dv
        JOIN ventas v ON v.id_venta=dv.id_venta
        WHERE DATE(v.fecha) BETWEEN :d AND :h AND COALESCE(v.anulada,0)=0
        GROUP BY v.vendedor_nombre, dv.producto_nombre
        ORDER BY total_vendido DESC
    """, {"d": desde, "h": hasta})


def detalle_productos_vendidos(desde, hasta):
    return detalle_productos_vendidos_cached(str(desde), str(hasta)).copy()


def clear_report_cache():
    ventas_periodo_cached.clear()
    detalle_productos_vendidos_cached.clear()


# ============================================================
# ESTILOS
# ============================================================
def inject_css():
    color = get_setting("color_principal", "#E8A06D") or "#E8A06D"
    st.markdown(f"""
    <style>
    :root {{ --primary:{color}; --warm:#E8A06D; --warm-dark:#B96D45; --dark:#0f172a; --muted:#64748b; --line:#e5e7eb; --bg:#f7f4f2; --danger:#b42318; --ok:#16a34a; }}
    .stApp {{ background:#f7f4f2; color:#111827; }}
    header[data-testid="stHeader"] {{ background:#0b0f19; }}
    .block-container {{ padding-top:.65rem; padding-bottom:2.0rem; max-width:1480px; }}
    section[data-testid="stSidebar"] {{ background:#ffffff; border-right:1px solid #e5e7eb; }}
    section[data-testid="stSidebar"] * {{ color:#111827 !important; opacity:1 !important; }}
    section[data-testid="stSidebar"] button {{ background:#ffffff !important; color:#111827 !important; border:1px solid #e5e7eb !important; }}
    .sidebar-logo {{ display:flex; align-items:center; gap:10px; margin:8px 0 18px; }}
    .sidebar-logo img {{ width:38px; height:38px; object-fit:contain; border-radius:10px; }}
    .sidebar-title {{ font-size:20px; font-weight:900; color:#111827 !important; }}
    .clomar-hero {{ background:linear-gradient(135deg,#111827,#1e293b); border-radius:0 0 18px 18px; padding:18px 28px; margin-bottom:18px; box-shadow:0 12px 26px rgba(15,23,42,.12); }}
    .clomar-hero, .clomar-hero * {{ color:#ffffff !important; opacity:1 !important; }}
    .clomar-hero h1 {{ margin:0; font-size:32px; line-height:1.08; font-weight:950; letter-spacing:-.02em; }}
    .clomar-hero p {{ margin:9px 0 0; color:#e5e7eb !important; font-size:15px; }}
    .card, .product-card, .kpi-card, .receipt-box {{ background:#fff; border:1px solid #e5e7eb; border-radius:20px; padding:20px; box-shadow:0 8px 25px rgba(15,23,42,.06); }}
    .kpi-label {{ color:#6b7280; font-size:13px; font-weight:900; letter-spacing:.06em; text-transform:uppercase; }}
    .kpi-value {{ color:#0f172a; font-size:34px; font-weight:950; margin-top:12px; }}
    .product-card {{ overflow:hidden; min-height:310px; display:flex; flex-direction:column; gap:9px; }}
    .product-img-wrap {{ width:100%; height:190px; background:linear-gradient(135deg,#fff7ed,#fff1f2); border:1px solid #eef2f7; border-radius:16px; overflow:hidden; display:flex; align-items:center; justify-content:center; margin-bottom:8px; }}
    .product-img-wrap.no-img::after {{ content:'🛍️'; font-size:58px; opacity:.65; }}
    .product-img {{ width:100%; height:100%; object-fit:contain; display:block; background:#fff; }}
    .product-name {{ font-weight:950; font-size:18px; line-height:1.3; color:#111827; }}
    .product-meta {{ color:#64748b; font-size:13px; line-height:1.45; }}
    .product-desc {{ color:#64748b; font-size:13px; line-height:1.35; min-height:20px; }}
    .product-price {{ font-size:28px; font-weight:950; color:#0f172a; margin-top:6px; }}
    .chip {{ display:inline-block; border-radius:999px; padding:7px 12px; font-weight:900; font-size:12px; margin:3px 4px 0 0; border:1px solid #e5e7eb; background:#f8fafc; color:#111827; }}
    .chip-ok {{ background:#dcfce7; color:#166534; border-color:#bbf7d0; }}
    .chip-red {{ background:#fff1ed; color:#9f1239; border-color:#fecdd3; }}
    .chip-dark {{ background:#0f172a; color:#fff; border-color:#0f172a; }}
    .chip-warn {{ background:#fef3c7; color:#92400e; border-color:#fde68a; }}
    .success-panel {{ background:#fff; border:1px solid #e5e7eb; border-radius:24px; padding:36px; text-align:center; box-shadow:0 10px 30px rgba(15,23,42,.08); margin:22px 0; }}
    .success-icon {{ font-size:52px; }} .success-title {{ font-size:32px; font-weight:950; color:#0f172a; }} .success-sub {{ color:#64748b; font-size:16px; }}
    .public-wrap {{ max-width:1180px; margin:0 auto; }}
    div[data-testid="stTextInput"] input, div[data-testid="stNumberInput"] input, div[data-testid="stTextArea"] textarea, div[data-testid="stSelectbox"] div[data-baseweb="select"] > div, div[data-testid="stDateInput"] input {{
        background:#ffffff !important; color:#111827 !important; border:1px solid #cbd5e1 !important; border-radius:12px !important;
    }}
    div[data-testid="stWidgetLabel"], div[data-testid="stWidgetLabel"] *, label {{ color:#111827 !important; opacity:1 !important; font-weight:800 !important; }}
    .stMarkdown:not(.clomar-hero) p, .stMarkdown:not(.clomar-hero) span {{ color:inherit; }}
    .stButton > button, .stDownloadButton > button {{ border-radius:12px !important; min-height:44px; font-weight:900 !important; color:#111827 !important; background:#ffffff !important; border:1px solid #cbd5e1 !important; }}
    .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {{ background:var(--warm) !important; color:#fff !important; border:1px solid var(--warm-dark) !important; box-shadow:0 8px 18px rgba(232,160,109,.26) !important; }}
    .stButton > button[kind="primary"] *, .stDownloadButton > button[kind="primary"] * {{ color:#fff !important; }}
    .stButton > button:hover, .stDownloadButton > button:hover {{ filter:brightness(.98); border-color:var(--warm-dark) !important; }}
    div[data-testid="stAlert"] {{ border-radius:14px; color:#111827 !important; }}
    .dataframe, table {{ color:#111827 !important; }}
    .clomar-table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:18px; overflow:hidden; box-shadow:0 8px 25px rgba(15,23,42,.06); }}
    .clomar-table th {{ background:#f8fafc; color:#475569; text-align:left; padding:14px; font-size:12px; text-transform:uppercase; letter-spacing:.06em; }}
    .clomar-table td {{ padding:14px; border-top:1px solid #eef2f7; color:#111827; }}
    .small-note {{ color:#64748b; font-size:13px; }}
    .pos-card {{ background:#fff; border:1px solid #e5e7eb; border-radius:14px; padding:12px; margin-bottom:8px; box-shadow:0 5px 14px rgba(15,23,42,.04); }}
    .pos-title {{ color:#111827; font-weight:950; font-size:16px; line-height:1.25; }}
    .pos-meta {{ color:#64748b; font-size:12px; margin-top:3px; }}
    .pos-price {{ color:#0f172a; font-size:22px; font-weight:950; margin:8px 0; }}
    .cart-line {{ background:#fff; border:1px solid #e5e7eb; border-radius:14px; padding:12px; margin-bottom:10px; }}
    .pay-box {{ background:#fff; border:1px solid #e5e7eb; border-radius:20px; padding:18px; box-shadow:0 8px 25px rgba(15,23,42,.06); }}
    .receipt-actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
    .print-button {{ background:#0f172a; color:white !important; border:0; border-radius:12px; padding:12px 18px; font-weight:900; cursor:pointer; }}

    .ticket-box {{ background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:24px; box-shadow:0 8px 24px rgba(15,23,42,.06); max-width:860px; margin:0 auto; }}
    .ticket-head {{ display:flex; justify-content:space-between; gap:18px; align-items:flex-start; border-bottom:1px dashed #cbd5e1; padding-bottom:14px; margin-bottom:14px; }}
    .ticket-logo {{ max-width:160px; max-height:72px; object-fit:contain; }}
    .ticket-title {{ font-size:28px; font-weight:950; color:#0f172a; margin:0; }}
    .ticket-sub {{ color:#64748b; font-size:13px; line-height:1.35; }}
    .ticket-meta {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:6px 18px; margin:12px 0 16px; font-size:14px; color:#111827; }}
    .ticket-total {{ display:flex; justify-content:space-between; align-items:center; border-top:1px dashed #cbd5e1; margin-top:14px; padding-top:14px; font-size:26px; font-weight:950; color:#0f172a; }}
    .quick-pay-note {{ background:#ecfeff; border:1px solid #bae6fd; color:#075985; padding:12px 14px; border-radius:14px; font-weight:800; }}
    @media print {{
        header[data-testid="stHeader"], section[data-testid="stSidebar"], .no-print, .stButton, .stDownloadButton, div[data-testid="stToolbar"], iframe {{ display:none !important; }}
        .block-container {{ max-width:100% !important; padding:0 !important; }}
        .ticket-box, .receipt-box {{ box-shadow:none !important; border:0 !important; margin:0 !important; padding:0 !important; }}
        .stApp {{ background:#fff !important; }}
    }}
    @media (max-width: 900px) {{ .clomar-hero h1 {{font-size:28px;}} .product-img-wrap {{height:145px;}} .block-container {{padding-left:.75rem; padding-right:.75rem;}} }}


    /* ===== V25.4: contraste fuerte, ticket imprimible y rendimiento visual ===== */
    .clomar-hero {{ margin-top:0 !important; padding:16px 24px !important; min-height:0 !important; }}
    .clomar-hero h1, .clomar-hero p {{ text-shadow:none !important; filter:none !important; }}
    .stApp, .stApp * {{ text-rendering:optimizeLegibility; }}
    div[data-testid="stMarkdownContainer"] p, div[data-testid="stMarkdownContainer"] li,
    div[data-testid="stMarkdownContainer"] span {{ color:#111827; }}
    .clomar-hero div[data-testid="stMarkdownContainer"] p,
    .clomar-hero div[data-testid="stMarkdownContainer"] span {{ color:#fff !important; }}
    button, button * {{ opacity:1 !important; }}
    .stButton > button:disabled, .stDownloadButton > button:disabled {{ color:#64748b !important; background:#f1f5f9 !important; border-color:#e2e8f0 !important; opacity:1 !important; }}
    div[data-testid="stTabs"] button, div[data-testid="stTabs"] button * {{ color:#111827 !important; opacity:1 !important; font-weight:900 !important; }}
    div[data-testid="stTabs"] button[aria-selected="true"], div[data-testid="stTabs"] button[aria-selected="true"] * {{ color:var(--warm-dark) !important; }}
    .pos-panel {{ background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(15,23,42,.05); }}
    .pos-panel h2, .pos-panel h3 {{ color:#0f172a !important; margin-top:0; }}
    .pos-total-banner {{ background:#0f172a; color:#fff !important; border-radius:18px; padding:18px 20px; display:flex; align-items:center; justify-content:space-between; margin:10px 0 16px; }}
    .pos-total-banner * {{ color:#fff !important; }}
    .cart-item-pro {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:14px; padding:12px; margin-bottom:10px; }}
    .cart-item-pro strong {{ color:#0f172a !important; }}
    .cart-item-pro .meta {{ color:#64748b !important; font-size:12px; }}
    .credit-action {{ background:#fff; border:1px solid #e5e7eb; border-radius:20px; padding:20px; box-shadow:0 10px 28px rgba(15,23,42,.06); margin:12px 0 18px; }}
    .ticket-pro {{ max-width:760px; margin:0 auto 18px; background:#fff; color:#111827; border-radius:24px; overflow:hidden; box-shadow:0 12px 32px rgba(15,23,42,.12); border:1px solid #e5e7eb; }}
    .ticket-pro * {{ color:#111827; box-sizing:border-box; }}
    .ticket-top {{ background:#111827; color:#fff !important; text-align:center; padding:24px 26px 22px; }}
    .ticket-top * {{ color:#fff !important; }}
    .ticket-icon {{ width:64px; height:64px; object-fit:contain; border-radius:16px; background:#f8fafc; padding:8px; margin-bottom:12px; }}
    .ticket-store {{ font-size:30px; font-weight:950; letter-spacing:.04em; margin:0; }}
    .ticket-kind {{ margin:6px 0 0; font-size:14px; opacity:.95; }}
    .ticket-body {{ padding:24px 28px 26px; }}
    .ticket-logo-wide {{ max-width:165px; max-height:68px; object-fit:contain; margin-bottom:8px; }}
    .ticket-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:16px 0; }}
    .ticket-box-mini {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:16px; padding:14px; }}
    .ticket-label {{ color:#64748b !important; font-size:12px; font-weight:900; text-transform:uppercase; letter-spacing:.06em; }}
    .ticket-value {{ color:#0f172a !important; font-size:18px; font-weight:950; margin-top:4px; }}
    .ticket-section-title {{ color:#64748b !important; font-weight:950; font-size:14px; text-transform:uppercase; border-bottom:1px solid #e5e7eb; padding-bottom:8px; margin:16px 0 8px; }}
    .ticket-info-row {{ display:flex; justify-content:space-between; gap:15px; padding:5px 0; font-size:14px; }}
    .ticket-info-row b {{ font-weight:950; }}
    .ticket-products {{ width:100%; border-collapse:collapse; margin-top:10px; }}
    .ticket-products th {{ background:#f8fafc; color:#475569 !important; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.05em; padding:10px; }}
    .ticket-products td {{ border-top:1px solid #e5e7eb; padding:11px 10px; font-size:14px; vertical-align:top; }}
    .ticket-total-pro {{ margin-top:18px; background:#111827; border-radius:18px; padding:18px; display:flex; justify-content:space-between; align-items:center; }}
    .ticket-total-pro * {{ color:#fff !important; font-weight:950; font-size:28px; }}
    .ticket-payment-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:14px; }}
    .ticket-footer {{ text-align:center; margin-top:18px; color:#64748b !important; font-size:13px; }}
    @media (max-width: 900px) {{ .ticket-grid,.ticket-payment-grid {{ grid-template-columns:1fr; }} .ticket-store {{font-size:24px;}} }}
    @media print {{
        body * {{ visibility:hidden !important; }}
        #ticket-print-area, #ticket-print-area * {{ visibility:visible !important; }}
        #ticket-print-area {{ position:absolute !important; left:0 !important; top:0 !important; width:100% !important; max-width:100% !important; margin:0 !important; box-shadow:none !important; border:0 !important; border-radius:0 !important; }}
        .ticket-pro {{ box-shadow:none !important; border:0 !important; border-radius:0 !important; }}
        .ticket-body {{ padding:14mm !important; }}
        .ticket-top {{ padding:12mm 10mm !important; }}
    }}


    /* ===== V25.7: visual limpio, botones negros, productos compactos y reportes sin gráficos pesados ===== */
    .product-card {{ min-height:265px !important; padding:14px !important; border-radius:18px !important; gap:6px !important; }}
    .product-img-wrap {{ height:132px !important; margin-bottom:4px !important; border-radius:14px !important; }}
    .product-name {{ font-size:15px !important; line-height:1.25 !important; min-height:38px !important; }}
    .product-meta {{ font-size:12px !important; line-height:1.28 !important; }}
    .product-price {{ font-size:23px !important; margin-top:4px !important; }}
    .chip {{ padding:6px 10px !important; font-size:11px !important; }}
    .stButton > button[kind="primary"], .stFormSubmitButton button[kind="primary"], button[data-testid="baseButton-primary"], .stDownloadButton > button[kind="primary"] {{
        background:#0f172a !important; color:#ffffff !important; border:1px solid #0f172a !important; box-shadow:0 8px 18px rgba(15,23,42,.18) !important;
    }}
    .stButton > button[kind="primary"] *, .stFormSubmitButton button[kind="primary"] *, button[data-testid="baseButton-primary"] *, .stDownloadButton > button[kind="primary"] * {{ color:#ffffff !important; }}
    .stButton > button[kind="primary"] p, .stFormSubmitButton button[kind="primary"] p, button[data-testid="baseButton-primary"] p {{ color:#ffffff !important; }}
    .stButton > button:hover, .stDownloadButton > button:hover {{ border-color:#0f172a !important; }}
    [data-testid="stFormSubmitButton"] button {{ background:#0f172a !important; color:#fff !important; border-color:#0f172a !important; }}
    [data-testid="stFormSubmitButton"] button * {{ color:#fff !important; }}
    .report-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin:16px 0; }}
    .report-card {{ background:#fff; border:1px solid #e5e7eb; border-radius:20px; padding:18px; box-shadow:0 8px 25px rgba(15,23,42,.06); }}
    .report-card h3 {{ margin:0 0 12px; color:#111827; font-weight:950; }}
    .bar-row {{ margin:12px 0; }}
    .bar-line {{ display:flex; justify-content:space-between; gap:10px; color:#111827; font-weight:850; font-size:13px; margin-bottom:6px; }}
    .bar-track {{ background:#f1f5f9; height:12px; border-radius:999px; overflow:hidden; border:1px solid #e2e8f0; }}
    .bar-fill {{ background:#0f172a; height:100%; border-radius:999px; }}
    .empty-chart {{ background:#fff; border:1px dashed #cbd5e1; border-radius:18px; padding:26px; color:#64748b; font-weight:800; text-align:center; }}
    @media (min-width: 1500px) {{ .product-img-wrap {{ height:145px !important; }} .product-name {{ font-size:16px !important; }} }}
    @media (max-width: 1200px) {{ .report-grid {{ grid-template-columns:1fr; }} }}
    @media (max-width: 900px) {{
        .product-card {{ min-height:auto !important; }}
        .product-img-wrap {{ height:180px !important; }}
        .report-grid {{ grid-template-columns:1fr; }}
    }}

    </style>
    """, unsafe_allow_html=True)


def html_table(df: pd.DataFrame, cols: list[str], labels: list[str], limit=100):
    if df.empty:
        st.info("Sin registros.")
        return
    rows = []
    for _, r in df.head(limit).iterrows():
        tds = "".join(f"<td>{esc(r.get(c,''))}</td>" for c in cols)
        rows.append(f"<tr>{tds}</tr>")
    th = "".join(f"<th>{esc(x)}</th>" for x in labels)
    st.markdown(f"<table class='clomar-table'><thead><tr>{th}</tr></thead><tbody>{''.join(rows)}</tbody></table>", unsafe_allow_html=True)


def kpi(label, value, sub=""):
    st.markdown(f"<div class='kpi-card'><div class='kpi-label'>{esc(label)}</div><div class='kpi-value'>{esc(value)}</div><div class='product-meta'>{esc(sub)}</div></div>", unsafe_allow_html=True)


def hero(title, subtitle, icon=""):
    st.markdown(f"<div class='clomar-hero'><h1>{icon} {esc(title)}</h1><p>{esc(subtitle)}</p></div>", unsafe_allow_html=True)


# ============================================================
# LOGIN / SIDEBAR
# ============================================================
def login_screen():
    cfg = cached_settings()
    logo = cfg.get("logo_url", "")
    icon = cfg.get("icon_url", "") or logo
    store = cfg.get("store_name") or APP_NAME_DEFAULT
    st.markdown("""
    <div style='max-width:520px;margin:28px auto 8px auto;'>
      <div class='card' style='padding:28px 30px;'>
    """, unsafe_allow_html=True)
    if logo:
        st.markdown(f"<div style='text-align:center;margin-bottom:12px'><img src='{esc(logo)}' style='max-width:240px;max-height:95px;object-fit:contain'></div>", unsafe_allow_html=True)
    elif icon:
        st.markdown(f"<div style='text-align:center;margin-bottom:12px'><img src='{esc(icon)}' style='width:82px;height:82px;object-fit:contain'></div>", unsafe_allow_html=True)
    st.markdown(f"<h2 style='margin:0 0 18px;color:#111827;font-weight:950;text-align:center'>Iniciar sesión</h2>", unsafe_allow_html=True)
    usuario = st.text_input("Usuario", placeholder="admin o vendedor")
    clave = st.text_input("Contraseña", type="password", placeholder="Tu contraseña")
    if st.button("Entrar", type="primary", use_container_width=True):
        df = query_df("SELECT * FROM usuarios WHERE usuario=:u AND estado='Activo'", {"u": usuario.strip()})
        if df.empty or not verify_password(clave, df.iloc[0]["password_hash"]):
            st.error("Usuario o contraseña incorrectos.")
        else:
            row = df.iloc[0].to_dict()
            st.session_state.user = {
                "id_usuario": int(row["id_usuario"]), "usuario": row["usuario"],
                "nombre": row["nombre"], "rol": row["rol"]
            }
            st.rerun()
    st.markdown("<div class='small-note' style='text-align:center;margin-top:14px'>Acceso seguro. Usa las credenciales definidas por el administrador.</div>", unsafe_allow_html=True)
    st.markdown("</div></div>", unsafe_allow_html=True)

def sidebar_nav():
    u = current_user()
    cfg = cached_settings()
    logo = cfg.get("icon_url") or cfg.get("logo_url")
    store = cfg.get("store_name") or APP_NAME_DEFAULT
    if logo:
        st.sidebar.markdown(f"<div class='sidebar-logo'><img src='{esc(logo)}'><div class='sidebar-title'>{esc(store)}</div></div>", unsafe_allow_html=True)
    else:
        st.sidebar.markdown(f"### 🛍️ {store}")
    st.sidebar.caption(f"{u['nombre']} · {u['rol']}")
    if is_admin():
        opciones = [
            "📊 Panel dueño", "🧾 Ventas", "💳 Créditos", "📦 Productos", "📘 Catálogo clientes", "📊 Inventario", "📥 Ingreso mercadería", "👥 Clientes", "💰 Caja", "📈 Reportes", "🔐 Usuarios", "⚙️ Configuración", "💾 Backup", "📲 Instalar app", "☁️ Estado nube"
        ]
    else:
        opciones = ["🧾 Ventas", "👥 Clientes", "📦 Productos", "📘 Catálogo clientes", "📲 Instalar app"]
    selected = st.sidebar.radio("Menú", opciones, label_visibility="collapsed")
    st.sidebar.divider()
    if st.sidebar.button("Cerrar sesión", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    return selected


# ============================================================
# COMPONENTES PRODUCTO / RECIBO / PDF
# ============================================================
def product_card_html(r, show_cost=False, show_stock=True, whatsapp=False, show_desc=False):
    img_candidates = product_image_candidates(r)
    img = img_candidates[0] if img_candidates else ""
    stock = float(r.get("stock_actual") or 0)
    stock_min = float(r.get("stock_minimo") or 0)
    chip_stock = ""
    if show_stock:
        chip_stock = f"<span class='chip {'chip-ok' if stock > stock_min else 'chip-red'}'>Stock {num(stock)}</span>"
    chip_cost = f"<span class='chip chip-dark'>Costo {money(r.get('costo_unitario'))}</span>" if show_cost else ""
    desc = esc(str(r.get("descripcion") or "")[:120])
    desc_html = f"<div class='product-desc'>{desc}</div>" if show_desc and desc else ""
    img_html = f"<img class='preview-img' src='{esc(img)}'>" if img else "<div class='preview-img'></div>"
    wa = f"<a href='{esc(wa_link(r.get('nombre_producto')))}' target='_blank' style='text-decoration:none'><div class='chip chip-dark' style='text-align:center;width:100%;box-sizing:border-box;margin-top:8px'>💬 Consultar por WhatsApp</div></a>" if whatsapp else ""
    return f"""
    <div class='product-card'>
      <div class='product-img-wrap'>{img_html}</div>
      <div class='product-name'>{esc(r.get('nombre_producto'))}</div>
      <div class='product-meta'>{esc(r.get('codigo'))} · {esc(r.get('categoria'))}</div>
      {desc_html}
      <div class='product-price'>{money(r.get('precio_venta'))}</div>
      <div>{chip_stock}{chip_cost}</div>
      {wa}
    </div>
    """


def add_product_button(r, keyprefix="add"):
    stock = float(r.get("stock_actual") or 0)
    disabled = stock <= 0
    label = "🛒 Agregar" if stock > 0 else "Sin stock"
    if st.button(label, key=f"{keyprefix}_{r['id_producto']}", use_container_width=True, disabled=disabled):
        if "cart" not in st.session_state:
            st.session_state.cart = []
        found = False
        for item in st.session_state.cart:
            if item["id_producto"] == int(r["id_producto"]):
                item["cantidad"] += 1
                found = True
                break
        if not found:
            st.session_state.cart.append({
                "id_producto": int(r["id_producto"]), "nombre": r["nombre_producto"], "codigo": r.get("codigo", ""),
                "precio": float(r["precio_venta"] or 0), "costo": float(r["costo_unitario"] or 0), "stock": stock, "cantidad": 1.0
            })
        st.toast("Producto agregado al carrito")
        st.rerun()



def fmt_dt(v):
    dt = _to_peru_dt(v)
    if not dt:
        return str(v or "")
    return dt.strftime("%d/%m/%Y %I:%M %p").replace("AM", "a. m.").replace("PM", "p. m.")


def print_button_component(label="🖨️ Imprimir"):
    components.html(f"""
    <button onclick="window.parent.print()" style="width:100%;height:44px;border:0;border-radius:12px;background:#0f172a;color:white;font-weight:900;font-size:15px;cursor:pointer;">{label}</button>
    """, height=52)

def render_receipt(id_venta: int):
    venta = query_df("""
        SELECT v.*, COALESCE(c.nombre_cliente,'Cliente general') AS cliente,
               COALESCE(c.telefono,'') AS cliente_telefono,
               COALESCE(c.documento,'') AS cliente_documento,
               COALESCE(c.direccion,'') AS cliente_direccion
        FROM ventas v LEFT JOIN clientes c ON c.id_cliente=v.id_cliente
        WHERE v.id_venta=:id
    """, {"id": id_venta})
    if venta.empty:
        return
    v = venta.iloc[0]
    det = query_df("SELECT * FROM detalle_ventas WHERE id_venta=:id", {"id": id_venta})
    cfg = cached_settings()
    icon = cfg.get("icon_url") or cfg.get("logo_url") or ""
    logo = cfg.get("logo_url") or ""
    icon_html = f"<img class='ticket-icon' src='{esc(icon)}'>" if icon else "<div class='ticket-icon' style='display:inline-flex;align-items:center;justify-content:center;font-size:34px'>🛍️</div>"
    logo_html = f"<img class='ticket-logo-wide' src='{esc(logo)}'>" if logo else ""
    fecha = fmt_dt(v.get('fecha'))
    comprobante = esc(v.get('comprobante'))
    cliente = esc(v.get('cliente'))
    telefono = esc(v.get('cliente_telefono')) or "-"
    documento = esc(v.get('cliente_documento')) or "-"
    direccion_cli = esc(v.get('cliente_direccion')) or "-"
    metodo = esc(v.get('metodo_pago') or '-')
    estado = esc(v.get('estado_pago') or '-')
    vendedor = esc(v.get('vendedor_nombre') or '-')
    total = float(v.get('total_venta') or 0)
    pagado = float(v.get('monto_pagado') or 0)
    saldo = float(v.get('saldo_pendiente') or 0)
    rows = ""
    for _, r in det.iterrows():
        rows += f"""
        <tr>
          <td><b>{esc(r.get('producto_nombre'))}</b><br><span style='color:#64748b;font-size:12px'>Código interno</span></td>
          <td style='text-align:center'>{num(r.get('cantidad'))}</td>
          <td style='text-align:right'>{money(r.get('precio_unitario'))}</td>
          <td style='text-align:right'><b>{money(r.get('subtotal'))}</b></td>
        </tr>
        """
    if not rows:
        rows = "<tr><td colspan='4'>Sin detalle de productos.</td></tr>"
    direccion = esc(cfg.get('direccion',''))
    telefono_tienda = esc(cfg.get('telefono',''))
    msg = esc(cfg.get('mensaje_comprobante','Gracias por su compra.'))
    st.markdown(f"""
    <div class='ticket-pro' id='ticket-print-area'>
      <div class='ticket-top'>
        {icon_html}
        <h1 class='ticket-store'>{esc(cfg.get('store_name') or APP_NAME_DEFAULT).upper()}</h1>
        <div class='ticket-kind'>Tienda multirrubro · Boleta de venta interna</div>
      </div>
      <div class='ticket-body'>
        <div style='display:flex;justify-content:space-between;gap:14px;align-items:flex-start'>
          <div>{logo_html}<div class='ticket-label'>Datos comerciales</div><div style='font-weight:800'>{direccion or '-'}</div><div style='color:#64748b'>WhatsApp: {telefono_tienda or '-'}</div></div>
          <div style='text-align:right'><span class='chip chip-dark'>BOLETA DE VENTA</span><div class='ticket-value'>{comprobante}</div><div class='ticket-sub'>{fecha}</div></div>
        </div>
        <div class='ticket-grid'>
          <div class='ticket-box-mini'><div class='ticket-label'>Boleta N.°</div><div class='ticket-value'>{comprobante}</div></div>
          <div class='ticket-box-mini'><div class='ticket-label'>Fecha / hora</div><div class='ticket-value'>{fecha}</div></div>
        </div>
        <div class='ticket-section-title'>Datos de la venta</div>
        <div class='ticket-info-row'><span>Tipo / estado</span><b>{estado}</b></div>
        <div class='ticket-info-row'><span>Método de pago</span><b>{metodo}</b></div>
        <div class='ticket-info-row'><span>Vendedor</span><b>{vendedor}</b></div>
        <div class='ticket-section-title'>Datos del cliente</div>
        <div class='ticket-info-row'><span>Cliente</span><b>{cliente}</b></div>
        <div class='ticket-info-row'><span>Documento</span><b>{documento}</b></div>
        <div class='ticket-info-row'><span>Teléfono</span><b>{telefono}</b></div>
        <div class='ticket-info-row'><span>Dirección</span><b>{direccion_cli}</b></div>
        <div class='ticket-section-title'>Detalle de productos</div>
        <table class='ticket-products'>
          <thead><tr><th>Producto</th><th style='text-align:center'>Cant.</th><th style='text-align:right'>Precio</th><th style='text-align:right'>Subtotal</th></tr></thead>
          <tbody>{rows.replace('<td><b>', '<td></td><td><b>', 1) if False else rows}</tbody>
        </table>
        <div class='ticket-total-pro'><span>TOTAL</span><span>{money(total)}</span></div>
        <div class='ticket-payment-grid'>
          <div class='ticket-box-mini'><div class='ticket-label'>Pagado</div><div class='ticket-value'>{money(pagado)}</div></div>
          <div class='ticket-box-mini'><div class='ticket-label'>Saldo</div><div class='ticket-value'>{money(saldo)}</div></div>
        </div>
        <div class='ticket-footer'>{msg}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)


def generate_receipt_pdf(id_venta: int):
    if colors is None:
        return None
    venta = query_df("""
        SELECT v.*, COALESCE(c.nombre_cliente,'Cliente general') AS cliente,
               COALESCE(c.telefono,'') AS cliente_telefono,
               COALESCE(c.documento,'') AS cliente_documento,
               COALESCE(c.direccion,'') AS cliente_direccion
        FROM ventas v LEFT JOIN clientes c ON c.id_cliente=v.id_cliente
        WHERE v.id_venta=:id
    """, {"id": id_venta})
    if venta.empty:
        return None
    v = venta.iloc[0]
    det = query_df("SELECT * FROM detalle_ventas WHERE id_venta=:id", {"id": id_venta})
    cfg = cached_settings()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.2*cm, leftMargin=1.2*cm, topMargin=1.1*cm, bottomMargin=1.1*cm)
    styles = getSampleStyleSheet()
    title_white = ParagraphStyle('TitleWhite', parent=styles['Title'], fontSize=22, leading=26, textColor=colors.white, alignment=1, fontName='Helvetica-Bold')
    subtitle_white = ParagraphStyle('SubWhite', parent=styles['Normal'], fontSize=10, leading=13, textColor=colors.white, alignment=1)
    normal = ParagraphStyle('NormalPro', parent=styles['Normal'], fontSize=9, leading=12, textColor=colors.HexColor('#111827'))
    small = ParagraphStyle('SmallPro', parent=styles['Normal'], fontSize=8, leading=10, textColor=colors.HexColor('#64748b'))
    label = ParagraphStyle('Label', parent=styles['Normal'], fontSize=8, leading=10, textColor=colors.HexColor('#64748b'), fontName='Helvetica-Bold')
    story = []

    # Encabezado oscuro tipo tienda
    header_data = []
    icon_cell = Paragraph('🛍️', title_white)
    logo_url = str(cfg.get('icon_url') or cfg.get('logo_url') or '').strip()
    if requests and logo_url:
        try:
            resp = requests.get(logo_url, timeout=2)
            if resp.ok and len(resp.content) < 1_200_000:
                icon_cell = RLImage(io.BytesIO(resp.content), width=1.8*cm, height=1.8*cm)
        except Exception:
            pass
    header_data.append([icon_cell])
    header_data.append([Paragraph(str(cfg.get('store_name') or APP_NAME_DEFAULT).upper(), title_white)])
    header_data.append([Paragraph('Tienda multirrubro · Boleta de venta interna', subtitle_white)])
    header_tbl = Table(header_data, colWidths=[16.8*cm])
    header_tbl.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1), colors.HexColor('#111827')),
        ('ALIGN',(0,0),(-1,-1),'CENTER'),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('TOPPADDING',(0,0),(-1,-1),8),
        ('BOTTOMPADDING',(0,0),(-1,-1),6),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, .35*cm))

    # Datos principales en cajas
    box_data = [
        [Paragraph('<b>VENTA N.°</b><br/>'+str(v['comprobante']), normal), Paragraph('<b>FECHA / HORA</b><br/>'+fmt_dt(v['fecha']), normal)],
        [Paragraph('<b>CLIENTE</b><br/>'+str(v['cliente']), normal), Paragraph('<b>VENDEDOR</b><br/>'+str(v.get('vendedor_nombre') or '-'), normal)],
        [Paragraph('<b>MÉTODO DE PAGO</b><br/>'+str(v.get('metodo_pago') or '-'), normal), Paragraph('<b>ESTADO</b><br/>'+str(v.get('estado_pago') or '-'), normal)],
    ]
    bt = Table(box_data, colWidths=[8.2*cm, 8.2*cm])
    bt.setStyle(TableStyle([
        ('BOX',(0,0),(-1,-1),0.8,colors.HexColor('#e5e7eb')),
        ('INNERGRID',(0,0),(-1,-1),0.4,colors.HexColor('#e5e7eb')),
        ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#f8fafc')),
        ('PADDING',(0,0),(-1,-1),9),
        ('VALIGN',(0,0),(-1,-1),'TOP'),
    ]))
    story.append(bt)
    story.append(Spacer(1, .35*cm))

    story.append(Paragraph('DETALLE DE PRODUCTOS', label))
    data = [["Cant.", "Producto", "Precio", "Subtotal"]]
    for _, r in det.iterrows():
        data.append([num(r['cantidad']), Paragraph(str(r['producto_nombre'])[:80], normal), money(r['precio_unitario']), money(r['subtotal'])])
    tbl = Table(data, colWidths=[1.6*cm, 8.7*cm, 3.0*cm, 3.1*cm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f1f5f9')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.HexColor('#334155')),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('ALIGN',(0,1),(0,-1),'CENTER'),
        ('ALIGN',(2,1),(-1,-1),'RIGHT'),
        ('GRID',(0,0),(-1,-1),0.4,colors.HexColor('#e5e7eb')),
        ('PADDING',(0,0),(-1,-1),8),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE')
    ]))
    story.append(tbl)
    story.append(Spacer(1, .35*cm))

    total_data = [[Paragraph('<b>TOTAL</b>', title_white), Paragraph('<b>'+money(v['total_venta'])+'</b>', title_white)]]
    total_tbl = Table(total_data, colWidths=[8.2*cm, 8.2*cm])
    total_tbl.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#111827')),
        ('TEXTCOLOR',(0,0),(-1,-1),colors.white),
        ('ALIGN',(1,0),(1,0),'RIGHT'),
        ('PADDING',(0,0),(-1,-1),12),
    ]))
    story.append(total_tbl)
    story.append(Spacer(1, .25*cm))
    pay_tbl = Table([
        [Paragraph('<b>Pagado:</b> '+money(v.get('monto_pagado',0)), normal), Paragraph('<b>Saldo:</b> '+money(v.get('saldo_pendiente',0)), normal)]
    ], colWidths=[8.2*cm, 8.2*cm])
    pay_tbl.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#f8fafc')),('BOX',(0,0),(-1,-1),0.4,colors.HexColor('#e5e7eb')),('PADDING',(0,0),(-1,-1),8)]))
    story.append(pay_tbl)
    story.append(Spacer(1, .35*cm))
    if cfg.get('direccion') or cfg.get('telefono'):
        story.append(Paragraph(f"{cfg.get('direccion','')} · WhatsApp: {cfg.get('telefono','')}", small))
    story.append(Paragraph(str(cfg.get('mensaje_comprobante','Gracias por su compra.')), small))
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def generate_catalog_pdf(df: pd.DataFrame):
    if colors is None:
        return None
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.2*cm, leftMargin=1.2*cm, topMargin=1.2*cm, bottomMargin=1.2*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('T', parent=styles['Title'], textColor=colors.HexColor('#0f172a'), fontSize=22, leading=26)
    normal = ParagraphStyle('N', parent=styles['Normal'], textColor=colors.HexColor('#111827'), fontSize=9, leading=12)
    small = ParagraphStyle('S', parent=styles['Normal'], textColor=colors.HexColor('#64748b'), fontSize=8, leading=10)
    story = []
    cfg = cached_settings()
    story.append(Paragraph(esc(cfg.get('store_name') or APP_NAME_DEFAULT), title_style))
    story.append(Paragraph(f"Catálogo comercial · {esc(cfg.get('telefono',''))} · {esc(cfg.get('direccion',''))}", small))
    story.append(Spacer(1, .35*cm))
    data = []
    for _, r in df.iterrows():
        img_cell = Paragraph("🛍️", normal)
        img_url = product_image_url(r)
        if requests and img_url:
            try:
                resp = requests.get(img_url, timeout=2)
                if resp.ok and len(resp.content) < 2_000_000:
                    img_cell = RLImage(io.BytesIO(resp.content), width=2.6*cm, height=2.2*cm)
                
            except Exception:
                pass
        texto = Paragraph(f"<b>{esc(r.get('nombre_producto'))}</b><br/>{esc(r.get('codigo'))} · {esc(r.get('categoria'))}<br/><font color='#0f172a'><b>{money(r.get('precio_venta'))}</b></font><br/>{'Disponible' if float(r.get('stock_actual') or 0)>0 else 'Consultar disponibilidad'}", normal)
        data.append([img_cell, texto])
    if not data:
        data = [[Paragraph("Sin productos visibles", normal), ""]]
    table = Table(data, colWidths=[3.1*cm, 13.5*cm])
    table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1), colors.white), ('BOX',(0,0),(-1,-1),0.25,colors.HexColor('#e5e7eb')),
        ('INNERGRID',(0,0),(-1,-1),0.25,colors.HexColor('#e5e7eb')), ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('LEFTPADDING',(0,0),(-1,-1),8), ('RIGHTPADDING',(0,0),(-1,-1),8), ('TOPPADDING',(0,0),(-1,-1),8), ('BOTTOMPADDING',(0,0),(-1,-1),8),
    ]))
    story.append(table)
    story.append(Spacer(1, .25*cm))
    story.append(Paragraph(esc(cfg.get('mensaje_comprobante','Gracias por su preferencia.')), small))
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


# ============================================================
# PÁGINAS
# ============================================================
def page_panel_dueno():
    hero("Panel del dueño", "Vista ejecutiva en tiempo real para controlar ventas, inventario y caja.", "📊")
    if not is_admin():
        st.warning("Solo administrador puede ver el panel del dueño.")
        return
    c1, c2 = st.columns(2)
    with c1:
        desde = st.date_input("Desde", peru_today(), key="pd_desde")
    with c2:
        hasta = st.date_input("Hasta", peru_today(), key="pd_hasta")
    ventas = ventas_periodo(desde, hasta)
    detalle = detalle_productos_vendidos(desde, hasta)
    productos = productos_con_stock()
    total = ventas["total_venta"].sum() if not ventas.empty else 0
    utilidad = detalle["utilidad"].sum() if not detalle.empty else 0
    ticket = total / len(ventas) if len(ventas) else 0
    caja = query_df("SELECT * FROM caja WHERE DATE(fecha) BETWEEN :d AND :h AND COALESCE(anulada,0)=0", {"d": str(desde), "h": str(hasta)})
    caja_neta = caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum() - caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum() if not caja.empty else 0
    a,b,c,d = st.columns(4)
    with a: kpi("Ventas", money(total), f"{len(ventas)} comprobantes")
    with b: kpi("Utilidad estimada", money(utilidad), "Según costo registrado")
    with c: kpi("Ticket promedio", money(ticket), "Promedio por venta")
    with d: kpi("Caja neta", money(caja_neta), "Ingresos - egresos")
    x1,x2 = st.columns([1.4,1])
    with x1:
        st.subheader("Ventas recientes")
        if ventas.empty:
            st.info("Todavía no hay ventas en el período.")
        else:
            vv = ventas.copy()
            vv["total_fmt"] = vv["total_venta"].apply(money)
            html_table(vv, ["comprobante","fecha","cliente","vendedor_nombre","metodo_pago","total_fmt"], ["Comprobante","Fecha","Cliente","Vendedor","Pago","Total"], 8)
    with x2:
        st.subheader("Stock crítico")
        crit = productos[productos["stock_actual"].astype(float) <= productos["stock_minimo"].astype(float)] if not productos.empty else pd.DataFrame()
        if crit.empty:
            st.success("Sin productos críticos.")
        else:
            for _, r in crit.head(10).iterrows():
                st.markdown(f"<span class='chip chip-red'>⚠️ {esc(r['nombre_producto'])} · Stock {num(r['stock_actual'])}</span>", unsafe_allow_html=True)
    st.subheader("Productos vendidos")
    if detalle.empty:
        st.info("No hay detalle de productos vendidos.")
    else:
        top = detalle.head(10).copy()
        fig = px.bar(top, x="producto", y="total_vendido", title="Top productos por venta")
        fig.update_layout(plot_bgcolor="white", paper_bgcolor="white", font_color="#111827")
        st.plotly_chart(fig, use_container_width=True)


def page_ventas():
    hero("Venta rápida", "POS ultraligero: selecciona producto, agrega al carrito, cobra y genera comprobante.", "🧾")
    if "cart" not in st.session_state:
        st.session_state.cart = []
    if "pos_step" not in st.session_state:
        st.session_state.pos_step = "carrito"

    # Venta finalizada: solo comprobante y acciones. No carga productos.
    if st.session_state.get("show_success_sale") and st.session_state.get("last_sale_id"):
        venta_id = int(st.session_state.last_sale_id)
        vdf = query_df("SELECT comprobante,total_venta,monto_pagado,saldo_pendiente,estado_pago FROM ventas WHERE id_venta=:id", {"id": venta_id})
        saldo = float(vdf.iloc[0]["saldo_pendiente"] or 0) if not vdf.empty else 0
        st.markdown("<div class='success-panel'><div class='success-icon'>✅</div><div class='success-title'>¡Venta registrada!</div><div class='success-sub'>Descarga, imprime o continúa vendiendo.</div></div>", unsafe_allow_html=True)
        render_receipt(venta_id)
        pdf = generate_receipt_pdf(venta_id)
        st.markdown("<div class='no-print'>", unsafe_allow_html=True)
        a,b,c = st.columns(3)
        with a:
            if pdf:
                st.download_button("⬇️ Descargar PDF", data=pdf, file_name=f"comprobante_{vdf.iloc[0]['comprobante'] if not vdf.empty else venta_id}.pdf", mime="application/pdf", use_container_width=True)
        with b:
            print_button_component("🖨️ Imprimir comprobante")
        with c:
            if st.button("Seguir vendiendo", type="primary", use_container_width=True):
                st.session_state.show_success_sale = False
                st.session_state.pos_step = "carrito"
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        if saldo > 0:
            st.warning(f"Esta venta quedó con saldo pendiente: {money(saldo)}. Registra pagos en 💳 Créditos.")
        return

    total_cart = sum(float(i.get("cantidad", 0)) * float(i.get("precio", 0)) for i in st.session_state.cart)
    c1,c2,c3 = st.columns(3)
    with c1: kpi("Carrito", f"{len(st.session_state.cart)} productos", "Edita antes de cobrar")
    with c2: kpi("Total actual", money(total_cart), "No se registra hasta confirmar")
    with c3: kpi("Paso", "Carrito" if st.session_state.pos_step == "carrito" else "Pago", "POS rápido")

    if st.session_state.pos_step == "carrito":
        productos = productos_con_stock()
        productos = productos[productos["stock_actual"].astype(float) > 0].copy() if not productos.empty else productos
        left, right = st.columns([1.05,.95])
        with left:
            st.markdown("<div class='pos-panel'><h3>Agregar producto</h3><p class='product-meta'>Usa la lista desplegable. No se cargan imágenes para que sea rápido.</p>", unsafe_allow_html=True)
            if productos.empty:
                st.info("No hay productos con stock disponible.")
            else:
                productos["label_pos"] = productos.apply(lambda r: f"{normalize_code(r.get('codigo'))} · {r.get('nombre_producto')} · Stock {num(r.get('stock_actual'))} · {money(r.get('precio_venta'))}", axis=1)
                with st.form("form_add_pos_ultra", clear_on_submit=False):
                    sel = st.selectbox("Producto", productos["label_pos"].tolist(), key="pos_producto_select_v254")
                    r = productos[productos["label_pos"].eq(sel)].iloc[0]
                    stock = float(r.get("stock_actual") or 0)
                    a,b = st.columns(2)
                    with a:
                        cantidad = st.number_input("Cantidad", min_value=1.0, max_value=max(stock,1.0), value=1.0, step=1.0, key="pos_cantidad_v254")
                    with b:
                        precio_default = float(r.get("precio_venta") or 0)
                        precio = st.number_input("Precio", min_value=0.0, value=precio_default, step=1.0, key="pos_precio_v254") if is_admin() else precio_default
                        if not is_admin():
                            st.text_input("Precio", value=money(precio), disabled=True, key="pos_precio_view_v254")
                    st.markdown(f"<div class='pos-card'><div class='pos-title'>{esc(r.get('nombre_producto'))}</div><div class='pos-meta'>{esc(r.get('codigo'))} · {esc(r.get('categoria'))}</div><div class='pos-price'>{money(precio)}</div><span class='chip chip-ok'>Stock {num(stock)}</span><span class='chip chip-dark'>Subtotal {money(float(cantidad)*float(precio))}</span></div>", unsafe_allow_html=True)
                    add = st.form_submit_button("🛒 Agregar al carrito", type="primary", use_container_width=True)
                if add:
                    found = False
                    for item in st.session_state.cart:
                        if item["id_producto"] == int(r["id_producto"]):
                            item["cantidad"] = min(float(item["cantidad"]) + float(cantidad), stock)
                            item["precio"] = float(precio)
                            found = True
                            break
                    if not found:
                        st.session_state.cart.append({
                            "id_producto": int(r["id_producto"]), "nombre": r["nombre_producto"], "codigo": r.get("codigo", ""),
                            "precio": float(precio), "costo": float(r.get("costo_unitario") or 0), "stock": stock, "cantidad": float(cantidad)
                        })
                    st.toast("Producto agregado")
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with right:
            st.markdown("<div class='pos-panel'><h3>🛒 Carrito</h3>", unsafe_allow_html=True)
            if not st.session_state.cart:
                st.info("Agrega productos para vender.")
            else:
                total = 0.0
                nuevo = []
                for idx, item in enumerate(st.session_state.cart):
                    st.markdown(f"<div class='cart-item-pro'><strong>{esc(item['nombre'])}</strong><div class='meta'>{esc(item.get('codigo',''))} · Stock {num(item.get('stock',0))}</div></div>", unsafe_allow_html=True)
                    c1,c2,c3,c4 = st.columns([.6,.75,.75,.25])
                    with c1:
                        cant = st.number_input("Cant.", min_value=0.0, max_value=float(item["stock"]), value=float(item["cantidad"]), step=1.0, key=f"cart_cant_v254_{idx}")
                    with c2:
                        precio = st.number_input("Precio", min_value=0.0, value=float(item["precio"]), step=1.0, key=f"cart_precio_v254_{idx}") if is_admin() else float(item["precio"])
                        if not is_admin(): st.text_input("Precio", value=money(precio), disabled=True, key=f"cart_precio_view_v254_{idx}")
                    with c3:
                        st.text_input("Subtotal", value=money(cant*precio), disabled=True, key=f"cart_sub_v254_{idx}")
                    with c4:
                        if st.button("❌", key=f"cart_del_v254_{idx}"):
                            cant = 0
                    if cant > 0:
                        item["cantidad"] = cant; item["precio"] = precio
                        total += cant * precio
                        nuevo.append(item)
                st.session_state.cart = nuevo
                st.markdown(f"<div class='pos-total-banner'><b>Total</b><b>{money(total)}</b></div>", unsafe_allow_html=True)
                a,b = st.columns(2)
                with a:
                    if st.button("Vaciar", use_container_width=True):
                        st.session_state.cart = []
                        st.rerun()
                with b:
                    if st.button("Continuar al pago", type="primary", use_container_width=True, disabled=total<=0):
                        st.session_state.pos_step = "pago"
                        st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
    else:
        if not st.session_state.cart:
            st.warning("El carrito está vacío.")
            if st.button("Volver al carrito"):
                st.session_state.pos_step = "carrito"
                st.rerun()
            return
        total = sum(float(i.get("cantidad", 0)) * float(i.get("precio", 0)) for i in st.session_state.cart)
        st.markdown(f"<div class='pos-total-banner'><b>Total a cobrar</b><b>{money(total)}</b></div>", unsafe_allow_html=True)
        left,right = st.columns([1.05,.95])
        with left:
            clientes = query_df("SELECT id_cliente, nombre_cliente, telefono FROM clientes WHERE COALESCE(estado,'Activo')='Activo' ORDER BY nombre_cliente")
            opciones_cliente = {"Cliente general": None}
            if not clientes.empty:
                opciones_cliente.update({f"{r['nombre_cliente']}" + (f" · {r['telefono']}" if str(r.get('telefono') or '').strip() else ""): int(r["id_cliente"]) for _, r in clientes.iterrows()})
            with st.form("form_confirmar_venta_v254"):
                tipo_venta = st.selectbox("Tipo de venta", ["Contado", "Crédito"])
                cliente_nombre = st.selectbox("Cliente", list(opciones_cliente.keys()))
                metodo_pago = st.selectbox("Medio de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto"])
                if tipo_venta == "Contado":
                    monto_pagado = st.number_input("Monto recibido", min_value=0.0, value=float(total), step=1.0)
                    fecha_venc = None
                else:
                    monto_pagado = st.number_input("Pago inicial", min_value=0.0, max_value=float(total), value=0.0, step=1.0)
                    fecha_venc = st.date_input("Fecha de vencimiento", peru_today() + timedelta(days=15))
                saldo = max(total - monto_pagado, 0)
                vuelto = max(monto_pagado - total, 0)
                obs = st.text_area("Observación", placeholder="Entrega, nota interna, pedido...")
                a,b = st.columns(2)
                with a: volver = st.form_submit_button("← Volver al carrito", use_container_width=True)
                with b: confirmar = st.form_submit_button("Confirmar venta", type="primary", use_container_width=True)
            if volver:
                st.session_state.pos_step = "carrito"; st.rerun()
        with right:
            st.markdown("<div class='pay-box'><h3>Resumen</h3>", unsafe_allow_html=True)
            for item in st.session_state.cart:
                st.write(f"{num(item['cantidad'])} x {item['nombre']} — {money(float(item['cantidad'])*float(item['precio']))}")
            st.divider()
            kpi("Total", money(total)); kpi("Pagado", money(monto_pagado)); kpi("Saldo", money(saldo));
            if tipo_venta == "Contado": st.markdown(f"<span class='chip chip-ok'>Vuelto {money(vuelto)}</span>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)
        if confirmar:
            if total <= 0:
                st.error("El total debe ser mayor a cero."); return
            if tipo_venta == "Contado" and monto_pagado < total:
                st.error("En contado, el monto recibido debe cubrir el total. Si quedará deuda, cambia a Crédito."); return
            if tipo_venta == "Crédito" and opciones_cliente[cliente_nombre] is None:
                st.error("Para vender a crédito debes seleccionar un cliente registrado."); return
            u=current_user(); comprobante=f"V{peru_now().strftime('%Y%m%d%H%M%S')}"
            metodo_final = metodo_pago if tipo_venta == "Contado" else ("Crédito" if monto_pagado <= 0 else f"Crédito + {metodo_pago}")
            try:
                with ENGINE.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO ventas (comprobante,id_cliente,id_usuario,vendedor_nombre,metodo_pago,total_venta,monto_pagado,saldo_pendiente,estado_pago,observacion,fecha_vencimiento,tipo_venta)
                        VALUES (:comp,:cli,:uid,:vend,:metodo,:total,:pagado,:saldo,:estado,:obs,:venc,:tipo)
                    """), {"comp": comprobante, "cli": opciones_cliente[cliente_nombre], "uid": u["id_usuario"], "vend": u["nombre"], "metodo": metodo_final, "total": total, "pagado": monto_pagado, "saldo": saldo, "estado": estado_pago, "obs": obs, "venc": str(fecha_venc) if fecha_venc else None, "tipo": tipo_venta})
                    venta_id = int(conn.execute(text("SELECT id_venta FROM ventas WHERE comprobante=:c"), {"c": comprobante}).scalar())
                    for item in st.session_state.cart:
                        subtotal=float(item["cantidad"])*float(item["precio"])
                        conn.execute(text("""
                            INSERT INTO detalle_ventas (id_venta,id_producto,producto_nombre,cantidad,precio_unitario,costo_unitario,subtotal)
                            VALUES (:idv,:idp,:prod,:cant,:precio,:costo,:sub)
                        """), {"idv": venta_id, "idp": item["id_producto"], "prod": item["nombre"], "cant": item["cantidad"], "precio": item["precio"], "costo": item["costo"], "sub": subtotal})
                        conn.execute(text("""
                            INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion)
                            VALUES (:idp,'SALIDA_VENTA',:cant,:costo,:ref,:uid,'Venta')
                        """), {"idp": item["id_producto"], "cant": item["cantidad"], "costo": item["costo"], "ref": comprobante, "uid": u["id_usuario"]})
                    if monto_pagado > 0:
                        conn.execute(text("""
                            INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion)
                            VALUES ('Ingreso',:concepto,:metodo,:monto,:ref,:uid,:obs)
                        """), {"concepto": f"Venta {comprobante}", "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": obs})
                    if tipo_venta == "Crédito" and monto_pagado > 0:
                        conn.execute(text("""
                            INSERT INTO pagos_credito (id_venta,id_cliente,metodo_pago,monto,referencia,id_usuario,observacion)
                            VALUES (:idv,:cli,:metodo,:monto,:ref,:uid,:obs)
                        """), {"idv": venta_id, "cli": opciones_cliente[cliente_nombre], "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": "Pago inicial"})
                clear_product_cache(); clear_report_cache()
                st.session_state.cart=[]; st.session_state.pos_step="carrito"; st.session_state.last_sale_id=venta_id; st.session_state.show_success_sale=True
                st.rerun()
            except Exception as e:
                st.error("No se pudo registrar la venta."); st.exception(e)

def page_productos():
    hero("Productos", "Catálogo visual, imágenes automáticas por código, importación Excel, precios y stock.", "📦")
    productos = productos_con_stock()
    categorias_df = query_df("SELECT * FROM categorias WHERE COALESCE(estado,'Activo')='Activo' ORDER BY nombre_categoria")
    if is_admin():
        tab1, tab2, tab3 = st.tabs(["Catálogo", "Crear / editar", "Importar Excel"])
    else:
        tab1, = st.tabs(["Catálogo"])
        tab2 = tab3 = None
    with tab1:
        c1, c2 = st.columns([1.5, .8])
        with c1:
            buscar = st.text_input("Buscar producto", placeholder="Nombre, código o categoría...", key="buscar_productos")
        with c2:
            cats = sorted([x for x in productos["categoria"].dropna().unique()]) if not productos.empty else []
            cat = st.selectbox("Categoría", ["Todas"] + cats, key="cat_prod")
        fil = productos.copy()
        if buscar.strip():
            txt = buscar.lower()
            fil = fil[fil["nombre_producto"].astype(str).str.lower().str.contains(txt, na=False) | fil["codigo"].astype(str).str.lower().str.contains(txt, na=False) | fil["categoria"].astype(str).str.lower().str.contains(txt, na=False)]
        if cat != "Todas":
            fil = fil[fil["categoria"].eq(cat)]
        if fil.empty:
            st.info("No hay productos.")
        else:
            cols = st.columns(5)
            for i, (_, r) in enumerate(fil.iterrows()):
                with cols[i % 5]:
                    st.markdown(product_card_html(r, show_cost=is_admin(), show_stock=True, show_desc=False), unsafe_allow_html=True)
        if not is_admin():
            st.info("Vista de vendedor: puedes consultar productos, precios y stock. La creación y edición quedan reservadas al administrador.")
    if tab2:
        with tab2:
            sub1, sub2 = st.tabs(["Nuevo producto", "Editar producto"])
            cat_opts = {r["nombre_categoria"]: int(r["id_categoria"]) for _, r in categorias_df.iterrows()} if not categorias_df.empty else {}
            with sub1:
                with st.form("form_producto"):
                    a,b = st.columns(2)
                    with a:
                        codigo = st.text_input("Código", placeholder="Ej: 0001")
                        nombre = st.text_input("Nombre del producto")
                        marca = st.text_input("Marca")
                        descripcion = st.text_area("Descripción")
                    with b:
                        categoria = st.selectbox("Categoría", list(cat_opts.keys()) if cat_opts else ["Sin categoría"])
                        costo = st.number_input("Costo unitario", min_value=0.0, step=1.0)
                        precio = st.number_input("Precio de venta", min_value=0.0, step=1.0)
                        stock_min = st.number_input("Stock mínimo", min_value=0.0, step=1.0)
                        stock_ini = st.number_input("Stock inicial", min_value=0.0, step=1.0)
                        imagen_url = st.text_input("Imagen URL opcional")
                    if st.form_submit_button("Guardar producto", type="primary", use_container_width=True):
                        if not nombre.strip():
                            st.error("Ingresa el nombre del producto.")
                        else:
                            codigo_final = normalize_code(codigo) or f"P{datetime.now().strftime('%Y%m%d%H%M%S')}"
                            try:
                                with ENGINE.begin() as conn:
                                    conn.execute(text("""
                                        INSERT INTO productos (codigo,nombre_producto,id_categoria,marca,descripcion,unidad,costo_unitario,precio_venta,stock_minimo,imagen_url,estado)
                                        VALUES (:codigo,:nombre,:cat,:marca,:desc,'Unidad',:costo,:precio,:stock_min,:img,'Activo')
                                    """), {"codigo": codigo_final, "nombre": nombre.strip(), "cat": cat_opts.get(categoria), "marca": marca, "desc": descripcion, "costo": costo, "precio": precio, "stock_min": stock_min, "img": imagen_url})
                                    idp = conn.execute(text("SELECT id_producto FROM productos WHERE codigo=:c"), {"c": codigo_final}).scalar()
                                    if stock_ini > 0:
                                        conn.execute(text("INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion) VALUES (:idp,'ENTRADA',:cant,:costo,'Stock inicial',:uid,'Alta de producto')"), {"idp": idp, "cant": stock_ini, "costo": costo, "uid": current_user()["id_usuario"]})
                                clear_product_cache(); st.success("Producto guardado."); st.rerun()
                            except Exception as e:
                                st.error("No se pudo guardar. Revisa si el código ya existe.")
            with sub2:
                if productos.empty:
                    st.info("No hay productos para editar.")
                else:
                    opciones = {f"{r['codigo']} - {r['nombre_producto']}": int(r['id_producto']) for _, r in productos.iterrows()}
                    sel = st.selectbox("Producto a editar", list(opciones.keys()))
                    r = productos[productos["id_producto"].eq(opciones[sel])].iloc[0]
                    with st.form("form_editar_producto"):
                        a,b = st.columns(2)
                        with a:
                            nombre = st.text_input("Nombre", value=str(r["nombre_producto"]))
                            marca = st.text_input("Marca", value=str(r.get("marca") or ""))
                            descripcion = st.text_area("Descripción", value=str(r.get("descripcion") or ""))
                            imagen_url = st.text_input("Imagen URL", value=str(r.get("imagen_url") or ""))
                        with b:
                            catnames = list(cat_opts.keys()) if cat_opts else ["Sin categoría"]
                            current_cat = r.get("categoria") if r.get("categoria") in catnames else catnames[0]
                            categoria = st.selectbox("Categoría", catnames, index=catnames.index(current_cat))
                            costo = st.number_input("Costo", min_value=0.0, value=float(r.get("costo_unitario") or 0), step=1.0)
                            precio = st.number_input("Precio", min_value=0.0, value=float(r.get("precio_venta") or 0), step=1.0)
                            stock_min = st.number_input("Stock mínimo", min_value=0.0, value=float(r.get("stock_minimo") or 0), step=1.0)
                            estado = st.selectbox("Estado", ["Activo", "Inactivo"], index=0 if str(r.get("estado","Activo")) == "Activo" else 1)
                        if st.form_submit_button("Actualizar producto", type="primary", use_container_width=True):
                            exec_sql("""
                                UPDATE productos SET nombre_producto=:n, marca=:m, descripcion=:d, id_categoria=:cat, costo_unitario=:costo, precio_venta=:precio, stock_minimo=:sm, imagen_url=:img, estado=:e, actualizado_en=CURRENT_TIMESTAMP
                                WHERE id_producto=:id
                            """, {"n": nombre, "m": marca, "d": descripcion, "cat": cat_opts.get(categoria), "costo": costo, "precio": precio, "sm": stock_min, "img": imagen_url, "e": estado, "id": int(r["id_producto"])})
                            clear_product_cache(); st.success("Producto actualizado."); st.rerun()
    if tab3:
        with tab3:
            st.subheader("Importar productos desde Excel")
            st.write("Descarga la plantilla, llena tus productos y vuelve a subir el archivo. Si el código ya existe, se actualiza.")
            plantilla = pd.DataFrame([{ "codigo":"0001", "nombre":"Producto ejemplo", "categoria":"General", "marca":"", "descripcion":"", "costo":10, "precio":20, "stock":5, "stock_minimo":1, "imagen_url":"", "estado":"Activo" }])
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                plantilla.to_excel(writer, index=False, sheet_name="productos")
            buf.seek(0)
            st.download_button("⬇️ Descargar plantilla Excel", data=buf, file_name="plantilla_productos_clomar_store.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
            if st.button("🧹 Unificar productos duplicados por código", use_container_width=True):
                dep = merge_duplicate_products_by_code()
                clear_product_cache()
                st.success(f"Depuración terminada. Duplicados unificados: {dep}.")
                st.rerun()
            up = st.file_uploader("Subir Excel de productos", type=["xlsx", "xls"])
            if up:
                df = pd.read_excel(up, dtype={"codigo": str})
                df.columns = [str(c).strip().lower() for c in df.columns]
                required = ["codigo", "nombre", "categoria", "costo", "precio", "stock"]
                missing = [c for c in required if c not in df.columns]
                if missing:
                    st.error(f"Faltan columnas obligatorias: {', '.join(missing)}")
                else:
                    st.write("Vista previa")
                    st.dataframe(df.head(20), use_container_width=True)
                    if st.button("Importar / actualizar productos", type="primary", use_container_width=True):
                        creados = actualizados = categorias_creadas = 0
                        with ENGINE.begin() as conn:
                            for _, row in df.iterrows():
                                codigo = normalize_code(row.get("codigo"))
                                nombre = str(row.get("nombre") or "").strip()
                                if not codigo or not nombre:
                                    continue
                                cat_name = str(row.get("categoria") or "General").strip() or "General"
                                cat_id = conn.execute(text("SELECT id_categoria FROM categorias WHERE lower(nombre_categoria)=lower(:n)"), {"n": cat_name}).scalar()
                                if not cat_id:
                                    conn.execute(text("INSERT INTO categorias (nombre_categoria, descripcion, estado) VALUES (:n,'Importada desde Excel','Activo')"), {"n": cat_name})
                                    cat_id = conn.execute(text("SELECT id_categoria FROM categorias WHERE lower(nombre_categoria)=lower(:n)"), {"n": cat_name}).scalar()
                                    categorias_creadas += 1
                                costo = float(row.get("costo") or 0); precio = float(row.get("precio") or 0); stock = float(row.get("stock") or 0)
                                stock_min = float(row.get("stock_minimo") or 0) if "stock_minimo" in df.columns else 0
                                imagen_url = str(row.get("imagen_url") or "").strip() if "imagen_url" in df.columns else ""
                                marca = str(row.get("marca") or "").strip() if "marca" in df.columns else ""
                                desc = str(row.get("descripcion") or "").strip() if "descripcion" in df.columns else ""
                                estado = str(row.get("estado") or "Activo").strip() if "estado" in df.columns else "Activo"
                                idp = find_product_id_by_code_conn(conn, codigo)
                                if idp:
                                    conn.execute(text("""
                                        UPDATE productos SET codigo=:codigo,nombre_producto=:n,id_categoria=:cat,marca=:m,descripcion=:d,costo_unitario=:cu,precio_venta=:pv,stock_minimo=:sm,imagen_url=COALESCE(NULLIF(:img,''),imagen_url),estado=:e,actualizado_en=CURRENT_TIMESTAMP WHERE id_producto=:id
                                    """), {"codigo": codigo, "n": nombre, "cat": cat_id, "m": marca, "d": desc, "cu": costo, "pv": precio, "sm": stock_min, "img": imagen_url, "e": estado, "id": idp})
                                    actualizados += 1
                                else:
                                    conn.execute(text("""
                                        INSERT INTO productos (codigo,nombre_producto,id_categoria,marca,descripcion,costo_unitario,precio_venta,stock_minimo,imagen_url,estado)
                                        VALUES (:c,:n,:cat,:m,:d,:cu,:pv,:sm,:img,:e)
                                    """), {"c": codigo, "n": nombre, "cat": cat_id, "m": marca, "d": desc, "cu": costo, "pv": precio, "sm": stock_min, "img": imagen_url, "e": estado})
                                    idp = find_product_id_by_code_conn(conn, codigo)
                                    creados += 1
                                current_stock = conn.execute(text(f"SELECT {stock_expr_sql()} FROM movimientos_stock ms WHERE ms.id_producto=:id"), {"id": idp}).scalar() or 0
                                delta = stock - float(current_stock)
                                if abs(delta) > 0.0001:
                                    conn.execute(text("INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion) VALUES (:id,'AJUSTE',:cant,:cu,'Importación Excel',:uid,'Ajuste de stock desde Excel')"), {"id": idp, "cant": delta, "cu": costo, "uid": current_user()["id_usuario"]})
                        depurados = merge_duplicate_products_by_code()
                        clear_product_cache()
                        st.success(f"Importación terminada. Creados: {creados}. Actualizados: {actualizados}. Categorías nuevas: {categorias_creadas}. Duplicados depurados: {depurados}.")
                        st.rerun()


def filtered_catalog_products():
    productos = productos_con_stock()
    return productos[productos["precio_venta"].astype(float) > 0].copy() if not productos.empty else productos


def page_catalogo_clientes(public=False):
    if not public:
        hero("Catálogo para clientes", "Vista comercial, PDF y consultas por WhatsApp. No muestra costos.", "📘")
    productos = filtered_catalog_products()
    if productos.empty:
        st.info("No hay productos para mostrar.")
        return
    c1, c2, c3 = st.columns([1.2,.8,.5])
    with c1:
        buscar = st.text_input("Buscar en catálogo", placeholder="Nombre, código, categoría...", key="buscar_cat_public" if public else "buscar_cat")
    cats = sorted([x for x in productos["categoria"].dropna().unique()])
    with c2:
        cat = st.selectbox("Categoría", ["Todas"]+cats, key="cat_public" if public else "cat_catalogo")
    with c3:
        solo_stock = st.toggle("Solo con stock", value=True)
    fil = productos.copy()
    if buscar.strip():
        txt = buscar.lower()
        fil = fil[fil["nombre_producto"].astype(str).str.lower().str.contains(txt, na=False) | fil["codigo"].astype(str).str.lower().str.contains(txt, na=False) | fil["categoria"].astype(str).str.lower().str.contains(txt, na=False)]
    if cat != "Todas":
        fil = fil[fil["categoria"].eq(cat)]
    if solo_stock:
        fil = fil[fil["stock_actual"].astype(float) > 0]
    if not public:
        b1,b2,b3 = st.columns(3)
        with b1:
            pdf = generate_catalog_pdf(fil)
            st.download_button("📄 Descargar catálogo PDF", data=pdf or b"", file_name="catalogo_clomar_store.pdf", mime="application/pdf", use_container_width=True, disabled=pdf is None)
        with b2:
            base = (get_setting("catalogo_base_url", "https://clomar-store.streamlit.app") or "").rstrip("/")
            st.link_button("🌐 Abrir catálogo público", f"{base}/?catalogo=1", use_container_width=True)
        with b3:
            st.link_button("💬 WhatsApp tienda", wa_link(), use_container_width=True)
        st.caption(f"Productos visibles: {len(fil)}. El catálogo no muestra costos internos.")
    cols = st.columns(4)
    for i, (_, r) in enumerate(fil.iterrows()):
        with cols[i % 4]:
            st.markdown(product_card_html(r, show_cost=False, show_stock=False, whatsapp=True, show_desc=True), unsafe_allow_html=True)


def page_inventario():
    hero("Inventario", "Stock actual, alertas y valorización de mercadería.", "📊")
    if not is_admin():
        st.warning("Solo administrador puede ver inventario completo.")
        return
    productos = productos_con_stock()
    if productos.empty:
        st.info("No hay productos.")
        return
    total_val = (productos["stock_actual"].astype(float) * productos["costo_unitario"].astype(float)).sum()
    total_stock = productos["stock_actual"].astype(float).sum()
    criticos = productos[productos["stock_actual"].astype(float) <= productos["stock_minimo"].astype(float)]
    a,b,c=st.columns(3)
    with a: kpi("Productos", str(len(productos)))
    with b: kpi("Unidades en stock", num(total_stock))
    with c: kpi("Valorización costo", money(total_val))
    view = productos.copy()
    view["precio_fmt"] = view["precio_venta"].apply(money)
    view["costo_fmt"] = view["costo_unitario"].apply(money)
    view["stock_fmt"] = view["stock_actual"].apply(num)
    html_table(view, ["codigo","nombre_producto","categoria","stock_fmt","stock_minimo","costo_fmt","precio_fmt"], ["Código","Producto","Categoría","Stock","Mínimo","Costo","Precio"], 300)
    if not criticos.empty:
        st.warning(f"Hay {len(criticos)} producto(s) en stock crítico.")


def page_ingreso_mercaderia():
    hero("Ingreso de mercadería", "Registra compras, aumenta stock y controla pagos a proveedores.", "📥")
    if not is_admin():
        st.warning("Solo administrador puede registrar ingresos de mercadería.")
        return
    productos = productos_con_stock()
    if productos.empty:
        st.info("Primero registra productos.")
        return
    with st.form("ingreso_mercaderia"):
        proveedor = st.text_input("Proveedor", placeholder="Nombre del proveedor")
        opciones = {f"{r['codigo']} - {r['nombre_producto']} | Stock {num(r['stock_actual'])}": int(r['id_producto']) for _, r in productos.iterrows()}
        prod_sel = st.selectbox("Producto", list(opciones.keys()))
        c1,c2 = st.columns(2)
        with c1:
            cantidad = st.number_input("Cantidad", min_value=0.0, step=1.0)
            costo = st.number_input("Costo unitario", min_value=0.0, step=1.0)
        with c2:
            metodo = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Crédito"])
            monto_pagado = st.number_input("Monto pagado", min_value=0.0, step=1.0)
        obs = st.text_area("Observación")
        total = cantidad * costo
        kpi("Total de ingreso", money(total), "Costo de mercadería registrada")
        if st.form_submit_button("Registrar ingreso de mercadería", type="primary", use_container_width=True):
            if cantidad <= 0:
                st.error("Cantidad debe ser mayor a cero.")
            else:
                idp = opciones[prod_sel]
                u = current_user()
                ref = f"ING{datetime.now().strftime('%Y%m%d%H%M%S')}"
                with ENGINE.begin() as conn:
                    conn.execute(text("INSERT INTO compras (proveedor,total_compra,monto_pagado,metodo_pago,observacion,id_usuario) VALUES (:p,:t,:mp,:m,:o,:u)"), {"p": proveedor, "t": total, "mp": monto_pagado, "m": metodo, "o": obs, "u": u["id_usuario"]})
                    conn.execute(text("INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion) VALUES (:id,'ENTRADA_COMPRA',:cant,:costo,:ref,:u,:obs)"), {"id": idp, "cant": cantidad, "costo": costo, "ref": ref, "u": u["id_usuario"], "obs": proveedor})
                    conn.execute(text("UPDATE productos SET costo_unitario=:c, actualizado_en=CURRENT_TIMESTAMP WHERE id_producto=:id"), {"c": costo, "id": idp})
                    if monto_pagado > 0:
                        conn.execute(text("INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion) VALUES ('Egreso',:c,:m,:mo,:r,:u,:o)"), {"c": f"Compra {ref}", "m": metodo, "mo": monto_pagado, "r": ref, "u": u["id_usuario"], "o": obs})
                clear_product_cache(); clear_report_cache()
                st.success("Ingreso registrado y stock actualizado.")
                st.rerun()


def page_clientes():
    hero("Clientes", "Registro de clientes, compras y cuentas por cobrar.", "👥")
    with st.expander("Crear cliente"):
        with st.form("cliente_nuevo"):
            nombre = st.text_input("Nombre")
            telefono = st.text_input("Teléfono")
            documento = st.text_input("Documento")
            direccion = st.text_input("Dirección")
            if st.form_submit_button("Guardar cliente", type="primary"):
                if nombre.strip():
                    exec_sql("INSERT INTO clientes (nombre_cliente,telefono,documento,direccion,estado) VALUES (:n,:t,:d,:dir,'Activo')", {"n":nombre,"t":telefono,"d":documento,"dir":direccion})
                    st.success("Cliente guardado."); st.rerun()
                else:
                    st.error("Ingresa el nombre.")
    clientes = query_df("""
        SELECT c.id_cliente,c.nombre_cliente,c.telefono,c.documento,c.direccion,c.estado,
               COALESCE(SUM(v.total_venta),0) total_comprado, COALESCE(SUM(v.saldo_pendiente),0) total_por_cobrar, COUNT(v.id_venta) ventas
        FROM clientes c LEFT JOIN ventas v ON v.id_cliente=c.id_cliente AND COALESCE(v.anulada,0)=0
        GROUP BY c.id_cliente,c.nombre_cliente,c.telefono,c.documento,c.direccion,c.estado
        ORDER BY c.nombre_cliente
    """)
    if clientes.empty:
        st.info("No hay clientes.")
    else:
        cols=st.columns(3)
        for i,(_,r) in enumerate(clientes.iterrows()):
            with cols[i%3]:
                st.markdown(f"<div class='card'><h3>👤 {esc(r['nombre_cliente'])}</h3><div class='product-meta'>{esc(r['telefono'])} · {esc(r['documento'])}</div><span class='chip chip-dark'>Comprado {money(r['total_comprado'])}</span><span class='chip {'chip-red' if float(r['total_por_cobrar'])>0 else 'chip-ok'}'>Por cobrar {money(r['total_por_cobrar'])}</span><span class='chip'>Ventas {int(r['ventas'])}</span></div>", unsafe_allow_html=True)


def page_creditos():
    hero("Créditos / cuentas por cobrar", "Consulta saldos, registra abonos y revisa historial de pagos.", "💳")
    if not is_admin():
        st.warning("Solo administrador puede ver créditos y cuentas por cobrar.")
        return
    pendientes = query_df("""
        SELECT v.id_venta, v.id_cliente, v.comprobante, v.fecha, v.fecha_vencimiento, v.tipo_venta, v.metodo_pago,
               v.total_venta, v.monto_pagado, v.saldo_pendiente, v.estado_pago,
               COALESCE(c.nombre_cliente,'Cliente general') AS cliente, COALESCE(c.telefono,'') AS telefono
        FROM ventas v
        LEFT JOIN clientes c ON c.id_cliente=v.id_cliente
        WHERE COALESCE(v.anulada,0)=0 AND COALESCE(v.saldo_pendiente,0) > 0
        ORDER BY COALESCE(v.fecha_vencimiento, DATE(v.fecha)) ASC, v.fecha DESC
    """)
    total_pendiente = float(pendientes["saldo_pendiente"].sum()) if not pendientes.empty else 0
    vencidas = 0
    if not pendientes.empty and "fecha_vencimiento" in pendientes.columns:
        fv = pd.to_datetime(pendientes["fecha_vencimiento"], errors="coerce").dt.date
        vencidas = int(((fv < peru_today()) & pendientes["saldo_pendiente"].astype(float).gt(0)).sum())
    c1,c2,c3 = st.columns(3)
    with c1: kpi("Total por cobrar", money(total_pendiente), "Saldo pendiente")
    with c2: kpi("Ventas pendientes", str(len(pendientes)), "Créditos abiertos")
    with c3: kpi("Vencidas", str(vencidas), "Requieren seguimiento")

    st.markdown("<div class='credit-action'><h3>💵 Registrar abono / pago parcial</h3><p class='product-meta'>Selecciona el comprobante, registra cuánto pagó el cliente y el sistema actualizará saldo y caja.</p>", unsafe_allow_html=True)
    if pendientes.empty:
        st.success("No hay cuentas por cobrar pendientes.")
    else:
        opts = {f"{r['comprobante']} · {r['cliente']} · Saldo {money(r['saldo_pendiente'])}": int(r["id_venta"]) for _, r in pendientes.iterrows()}
        with st.form("form_pago_credito_directo"):
            sel = st.selectbox("Venta pendiente", list(opts.keys()))
            venta_sel = pendientes[pendientes["id_venta"].eq(opts[sel])].iloc[0]
            saldo_actual = float(venta_sel["saldo_pendiente"] or 0)
            a,b,c = st.columns([.8,.8,1.2])
            with a:
                monto = st.number_input("Monto recibido", min_value=0.0, max_value=saldo_actual, value=saldo_actual, step=1.0)
            with b:
                metodo = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto"])
            with c:
                obs = st.text_input("Observación", placeholder="Ej: abono, cancelación, pago parcial")
            registrar = st.form_submit_button("💵 Registrar pago y actualizar deuda", type="primary", use_container_width=True)
        st.markdown(f"<div class='card'><b>Cliente:</b> {esc(venta_sel['cliente'])} &nbsp; <span class='chip chip-red'>Saldo actual {money(saldo_actual)}</span> <span class='chip'>Comprobante {esc(venta_sel['comprobante'])}</span></div>", unsafe_allow_html=True)
        if registrar:
            if monto <= 0:
                st.error("El monto debe ser mayor a cero.")
            else:
                nuevo_pagado = float(venta_sel["monto_pagado"] or 0) + monto
                nuevo_saldo = max(float(venta_sel["total_venta"] or 0) - nuevo_pagado, 0)
                nuevo_estado = "Pagada" if nuevo_saldo <= 0 else "Parcial"
                with ENGINE.begin() as conn:
                    conn.execute(text("""
                        UPDATE ventas SET monto_pagado=:pagado, saldo_pendiente=:saldo, estado_pago=:estado
                        WHERE id_venta=:id
                    """), {"pagado": nuevo_pagado, "saldo": nuevo_saldo, "estado": nuevo_estado, "id": int(venta_sel["id_venta"])})
                    conn.execute(text("""
                        INSERT INTO pagos_credito (id_venta,id_cliente,metodo_pago,monto,referencia,id_usuario,observacion)
                        VALUES (:idv,:cli,:metodo,:monto,:ref,:uid,:obs)
                    """), {"idv": int(venta_sel["id_venta"]), "cli": int(venta_sel["id_cliente"]) if pd.notna(venta_sel.get("id_cliente")) else None, "metodo": metodo, "monto": monto, "ref": venta_sel["comprobante"], "uid": current_user()["id_usuario"], "obs": obs})
                    conn.execute(text("""
                        INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion)
                        VALUES ('Ingreso',:concepto,:metodo,:monto,:ref,:uid,:obs)
                    """), {"concepto": f"Pago crédito {venta_sel['comprobante']}", "metodo": metodo, "monto": monto, "ref": venta_sel["comprobante"], "uid": current_user()["id_usuario"], "obs": obs})
                clear_report_cache()
                st.success(f"Pago registrado. Nuevo saldo: {money(nuevo_saldo)}")
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    st.subheader("Pendientes por cobrar")
    if pendientes.empty:
        st.info("No hay créditos pendientes.")
    else:
        df = pendientes.copy()
        df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M")
        df["total_fmt"] = df["total_venta"].apply(money)
        df["pagado_fmt"] = df["monto_pagado"].apply(money)
        df["saldo_fmt"] = df["saldo_pendiente"].apply(money)
        html_table(df, ["comprobante","fecha","fecha_vencimiento","cliente","telefono","total_fmt","pagado_fmt","saldo_fmt","estado_pago"], ["Comprobante","Fecha","Vence","Cliente","Teléfono","Total","Pagado","Saldo","Estado"], 300)

    with st.expander("📜 Historial de pagos registrados", expanded=False):
        pagos = query_df("""
            SELECT pc.fecha, pc.referencia, COALESCE(c.nombre_cliente,'Cliente') AS cliente, pc.metodo_pago, pc.monto, pc.observacion
            FROM pagos_credito pc
            LEFT JOIN clientes c ON c.id_cliente=pc.id_cliente
            ORDER BY pc.fecha DESC
        """)
        if pagos.empty:
            st.info("Todavía no hay pagos de crédito registrados.")
        else:
            pagos["fecha"] = pd.to_datetime(pagos["fecha"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M")
            pagos["monto_fmt"] = pagos["monto"].apply(money)
            html_table(pagos, ["fecha","referencia","cliente","metodo_pago","monto_fmt","observacion"], ["Fecha","Comprobante","Cliente","Método","Monto","Observación"], 300)


def page_caja():
    hero("Caja", "Movimientos, ingresos, egresos y cierre del día.", "💰")
    if not is_admin():
        st.warning("Solo administrador puede ver caja."); return
    f = st.date_input("Fecha", peru_today())
    caja = query_df("SELECT * FROM caja WHERE DATE(fecha)=:f AND COALESCE(anulada,0)=0 ORDER BY fecha DESC", {"f": str(f)})
    ingresos = caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum() if not caja.empty else 0
    egresos = caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum() if not caja.empty else 0
    a,b,c=st.columns(3)
    with a: kpi("Ingresos", money(ingresos))
    with b: kpi("Egresos", money(egresos))
    with c: kpi("Saldo neto", money(ingresos-egresos))
    if not caja.empty:
        ctab=caja.copy(); ctab["monto_fmt"]=ctab["monto"].apply(money)
        html_table(ctab, ["fecha","tipo","concepto","metodo_pago","monto_fmt","referencia"], ["Fecha","Tipo","Concepto","Pago","Monto","Ref"], 200)
    with st.expander("Registrar egreso / salida"):
        with st.form("egreso"):
            concepto=st.text_input("Concepto")
            monto=st.number_input("Monto", min_value=0.0, step=1.0)
            metodo=st.selectbox("Método", ["Efectivo","Yape","Plin","Transferencia","Tarjeta"])
            if st.form_submit_button("Registrar egreso", type="primary"):
                exec_sql("INSERT INTO caja (tipo,concepto,metodo_pago,monto,id_usuario) VALUES ('Egreso',:c,:m,:mo,:u)", {"c":concepto,"m":metodo,"mo":monto,"u":current_user()["id_usuario"]})
                clear_report_cache(); st.success("Egreso registrado."); st.rerun()


def page_reportes():
    hero("Reportes", "Ventas por fecha, vendedor, producto y método de pago.", "📈")
    if not is_admin():
        st.warning("Solo administrador puede ver reportes."); return
    c1,c2=st.columns(2)
    with c1: desde=st.date_input("Desde", peru_today()-timedelta(days=7), key="repd")
    with c2: hasta=st.date_input("Hasta", peru_today(), key="reph")
    ventas=ventas_periodo(desde,hasta); detalle=detalle_productos_vendidos(desde,hasta)
    if ventas.empty:
        st.info("No hay ventas en el rango."); return
    ventas["dia"] = pd.to_datetime(ventas["fecha"]).dt.date
    diario = ventas.groupby("dia", as_index=False)["total_venta"].sum()
    fig = px.line(diario, x="dia", y="total_venta", markers=True, title="Ventas por día")
    fig.update_layout(template="plotly_white", plot_bgcolor="white", paper_bgcolor="white", font_color="#111827", title_font_color="#111827")
    fig.update_xaxes(color="#111827", gridcolor="#e5e7eb")
    fig.update_yaxes(color="#111827", gridcolor="#e5e7eb")
    st.plotly_chart(fig, use_container_width=True)
    a,b=st.columns(2)
    with a:
        metodo=ventas.groupby("metodo_pago", as_index=False)["total_venta"].sum()
        fig2=px.pie(metodo, names="metodo_pago", values="total_venta", title="Métodos de pago"); fig2.update_layout(template="plotly_white", paper_bgcolor="white", font_color="#111827", title_font_color="#111827")
        st.plotly_chart(fig2, use_container_width=True)
    with b:
        vendedor=ventas.groupby("vendedor_nombre", as_index=False)["total_venta"].sum()
        fig3=px.bar(vendedor, x="vendedor_nombre", y="total_venta", title="Ventas por vendedor"); fig3.update_layout(template="plotly_white", plot_bgcolor="white", paper_bgcolor="white", font_color="#111827", title_font_color="#111827"); fig3.update_xaxes(color="#111827", gridcolor="#e5e7eb"); fig3.update_yaxes(color="#111827", gridcolor="#e5e7eb")
        st.plotly_chart(fig3, use_container_width=True)
    if not detalle.empty:
        dd=detalle.copy(); dd["total"]=dd["total_vendido"].apply(money); dd["utilidad_fmt"]=dd["utilidad"].apply(money)
        html_table(dd, ["vendedor","producto","cantidad","total","utilidad_fmt"], ["Vendedor","Producto","Cantidad","Total","Utilidad"], 200)


def page_usuarios():
    hero("Usuarios", "Control de accesos: administrador y vendedor.", "🔐")
    if not is_admin():
        st.warning("Solo administrador puede gestionar usuarios."); return
    tab1, tab2, tab3 = st.tabs(["Lista", "Editar / contraseña", "Crear usuario"])
    with tab1:
        users=query_df("SELECT id_usuario, usuario, nombre, rol, estado, creado_en FROM usuarios ORDER BY id_usuario")
        html_table(users, ["usuario","nombre","rol","estado","creado_en"], ["Usuario","Nombre","Rol","Estado","Creado"], 200)
    with tab2:
        users=query_df("SELECT id_usuario, usuario, nombre, rol, estado FROM usuarios ORDER BY usuario")
        if users.empty:
            st.info("Sin usuarios.")
        else:
            opts={f"{r['usuario']} - {r['nombre']}": int(r['id_usuario']) for _,r in users.iterrows()}
            sel=st.selectbox("Usuario a editar", list(opts.keys()))
            r=users[users["id_usuario"].eq(opts[sel])].iloc[0]
            with st.form("edit_user"):
                nombre=st.text_input("Nombre", value=str(r["nombre"]))
                rol=st.selectbox("Rol", ["Vendedor","Administrador","Supervisor"], index=["Vendedor","Administrador","Supervisor"].index(str(r["rol"])) if str(r["rol"]) in ["Vendedor","Administrador","Supervisor"] else 0)
                estado=st.selectbox("Estado", ["Activo","Inactivo"], index=0 if str(r["estado"])=="Activo" else 1)
                nueva=st.text_input("Nueva contraseña opcional", type="password")
                if st.form_submit_button("Actualizar usuario", type="primary"):
                    params={"n":nombre,"r":rol,"e":estado,"id":int(r["id_usuario"])}
                    exec_sql("UPDATE usuarios SET nombre=:n, rol=:r, estado=:e WHERE id_usuario=:id", params)
                    if nueva:
                        exec_sql("UPDATE usuarios SET password_hash=:p WHERE id_usuario=:id", {"p":hash_password(nueva),"id":int(r["id_usuario"])})
                    st.success("Usuario actualizado."); st.rerun()
    with tab3:
        with st.form("crear_user"):
            usuario=st.text_input("Usuario")
            nombre=st.text_input("Nombre")
            clave=st.text_input("Contraseña", type="password")
            rol=st.selectbox("Rol", ["Vendedor","Administrador","Supervisor"], key="rolnuevo")
            if st.form_submit_button("Crear usuario", type="primary"):
                if not usuario or not clave:
                    st.error("Usuario y contraseña son obligatorios.")
                else:
                    try:
                        exec_sql("INSERT INTO usuarios (usuario,password_hash,nombre,rol,estado) VALUES (:u,:p,:n,:r,'Activo')", {"u":usuario,"p":hash_password(clave),"n":nombre or usuario,"r":rol})
                        st.success("Usuario creado."); st.rerun()
                    except Exception:
                        st.error("No se pudo crear. Quizás el usuario ya existe.")


def page_config_tienda():
    hero("Configuración de tienda", "Personaliza logo, ícono, fotos, datos comerciales y comprobantes.", "⚙️")
    if not is_admin():
        st.warning("Solo administrador puede modificar configuración."); return
    cfg = cached_settings()
    c1,c2=st.columns([1.25,.9])
    with c1:
        with st.form("config_tienda"):
            store_name=st.text_input("Nombre de la tienda", value=cfg.get("store_name", APP_NAME_DEFAULT))
            logo_url=st.text_input("Logo URL", value=cfg.get("logo_url", ""), help="Logo horizontal para login, catálogo y comprobantes.")
            icon_url=st.text_input("Ícono URL", value=cfg.get("icon_url", ""), help="Ícono cuadrado para menú lateral y vista compacta.")
            telefono=st.text_input("Teléfono / WhatsApp", value=cfg.get("telefono", ""))
            direccion=st.text_area("Dirección", value=cfg.get("direccion", ""))
            mensaje=st.text_input("Mensaje del comprobante", value=cfg.get("mensaje_comprobante", "Gracias por su compra."))
            base_cat=st.text_input("URL base de catálogo público", value=cfg.get("catalogo_base_url", "https://clomar-store.streamlit.app"))
            base_imgs=st.text_input("URL base automática de imágenes", value=cfg.get("imagenes_base_url", "https://raw.githubusercontent.com/CLOMARstore/clomar-store/main/imagenes_productos"))
            color=st.color_picker("Color principal", value=cfg.get("color_principal", "#E49A86"))
            if st.form_submit_button("Guardar configuración", type="primary", use_container_width=True):
                for k,v in {"store_name":store_name,"logo_url":logo_url,"icon_url":icon_url,"telefono":telefono,"direccion":direccion,"mensaje_comprobante":mensaje,"catalogo_base_url":base_cat,"imagenes_base_url":base_imgs,"color_principal":color}.items():
                    set_setting(k,v)
                st.success("Configuración guardada."); st.rerun()
    with c2:
        st.subheader("Vista previa")
        if cfg.get("logo_url"):
            st.image(cfg.get("logo_url"), width=220)
        if cfg.get("icon_url"):
            st.image(cfg.get("icon_url"), width=72)
        st.markdown(f"<div class='card'><h2>{esc(cfg.get('store_name') or APP_NAME_DEFAULT)}</h2><div class='product-meta'>{esc(cfg.get('direccion',''))}<br>{esc(cfg.get('telefono',''))}</div><br><span class='chip chip-dark'>Comprobante</span><p>{esc(cfg.get('mensaje_comprobante','Gracias por su compra.'))}</p></div>", unsafe_allow_html=True)
        st.info("V25 usa imágenes automáticas por código. Sube fotos a imagenes_productos/0001.jpg y define la URL base aquí.")


def page_backup():
    hero("Backup / exportación", "Descarga datos de la nube en CSV para respaldo.", "💾")
    if not is_admin():
        st.warning("Solo administrador puede descargar backups."); return
    tablas=["usuarios","categorias","clientes","proveedores","productos","movimientos_stock","ventas","detalle_ventas","compras","caja","ajustes"]
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
    st.info("Este ZIP no contiene la contraseña de Neon. Solo exporta datos de negocio.")


def page_instalar_app():
    hero("Instalar como app", "Instala Clomar Store en celular o laptop sin Play Store ni App Store.", "📲")
    cfg = cached_settings()
    icon = cfg.get("icon_url") or cfg.get("logo_url") or "./app/static/icon-192.png"
    st.markdown(f"""
    <div class='card' style='display:grid;grid-template-columns:110px 1fr;gap:18px;align-items:center'>
      <div style='width:92px;height:92px;border-radius:24px;background:#fff3f0;border:1px solid #f3d7d2;display:flex;align-items:center;justify-content:center;overflow:hidden'>
        <img src='{esc(icon)}' style='max-width:76px;max-height:76px;object-fit:contain' onerror="this.style.display='none'">
      </div>
      <div>
        <h2 style='margin:0 0 6px'>Clomar Store</h2>
        <p style='margin:0;color:#64748b'>POS, inventario, caja, créditos y reportes en modo app instalable.</p>
        <div style='margin-top:10px'><span class='chip chip-ok'>PWA Force RAW V27.1</span><span class='chip chip-dark'>Android / Windows</span></div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("""
        <div class='card'>
          <h3>📱 Android / Chrome</h3>
          <ol>
            <li>Abre <b>Clomar Store</b> en Chrome.</li>
            <li>Toca los tres puntos del navegador.</li>
            <li>Elige <b>Agregar a pantalla principal</b> o <b>Instalar app</b>.</li>
            <li>Confirma el nombre <b>Clomar Store</b>.</li>
            <li>Abre el ícono desde la pantalla principal.</li>
          </ol>
          <p class='small-note'>Si no sale “Instalar app”, usa “Agregar a pantalla principal”.</p>
        </div>
        """, unsafe_allow_html=True)
    with c2:
        st.markdown("""
        <div class='card'>
          <h3>💻 Windows / Chrome o Edge</h3>
          <ol>
            <li>Abre la app en Chrome o Edge.</li>
            <li>Menú de tres puntos.</li>
            <li>Busca <b>Instalar esta página como app</b> o <b>Apps → Instalar</b>.</li>
            <li>Confirma y abre Clomar Store como ventana independiente.</li>
          </ol>
          <p class='small-note'>Esto no requiere pagar Play Store ni App Store.</p>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("""
    <div class='card'>
      <h3>✅ Lista de prueba V27</h3>
      <p>Después de instalar, verifica: login, ventas, carrito, caja, créditos, productos y reportes. En celular la vista debe acomodarse en una columna y no debe cortar los botones principales.</p>
      <p><b>Nota:</b> Esta versión usa íconos y manifest desde RAW de GitHub para evitar fallas de /app/static/ en Streamlit Cloud. La PWA completa con trabajo offline total queda para una etapa posterior.</p>
    </div>
    """, unsafe_allow_html=True)


def page_estado_nube():
    hero("Estado nube", "Verifica si la app trabaja local o conectada a PostgreSQL Neon.", "☁️")
    if IS_POSTGRES:
        st.success("Modo nube PostgreSQL activo.")
    else:
        st.warning("Modo local SQLite activo. En Streamlit Cloud debes configurar NEON_DATABASE_URL en Secrets.")
    st.write("Versión:", APP_VERSION)
    st.code("NEON_DATABASE_URL = postgresql://...?...sslmode=require", language="toml")



# ============================================================
# V25.5 OVERRIDES: impresión limpia y PDF ticket térmico
# ============================================================
def print_button_component(label="🖨️ Imprimir comprobante"):
    components.html(f"""
    <button onclick="printReceiptOnly()" style="width:100%;height:44px;border:0;border-radius:12px;background:#0f172a;color:white;font-weight:900;font-size:15px;cursor:pointer;">{label}</button>
    <script>
    function printReceiptOnly() {{
      const parentDoc = window.parent.document;
      const el = parentDoc.getElementById('ticket-print-area');
      if (!el) {{ alert('No se encontró el comprobante para imprimir.'); return; }}
      const w = window.open('', '_blank', 'width=420,height=720');
      if (!w) {{ alert('El navegador bloqueó la ventana de impresión. Permite ventanas emergentes para esta app.'); return; }}
      w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Comprobante</title>
      <style>
        @page {{ size: 80mm auto; margin: 3mm; }}
        body {{ margin:0; background:white; font-family: Arial, Helvetica, sans-serif; color:#111827; }}
        .ticket-pro {{ width:76mm !important; max-width:76mm !important; margin:0 auto !important; border:0 !important; box-shadow:none !important; border-radius:0 !important; overflow:visible !important; background:white !important; }}
        .ticket-top {{ background:#111827 !important; color:white !important; text-align:center !important; padding:8mm 4mm !important; }}
        .ticket-top * {{ color:white !important; }}
        .ticket-icon {{ width:17mm !important; height:17mm !important; object-fit:contain !important; background:white !important; border-radius:5mm !important; padding:2mm !important; }}
        .ticket-store {{ font-size:18pt !important; margin:3mm 0 1mm !important; letter-spacing:.04em !important; }}
        .ticket-kind {{ font-size:9pt !important; }}
        .ticket-body {{ padding:4mm !important; }}
        .ticket-logo-wide {{ max-width:34mm !important; max-height:18mm !important; object-fit:contain !important; }}
        .chip {{ display:inline-block; border-radius:999px; padding:2mm 3mm; font-weight:800; font-size:8pt; background:#111827; color:white !important; }}
        .ticket-grid, .ticket-payment-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:2mm; }}
        .ticket-box-mini {{ border:1px solid #e5e7eb; border-radius:3mm; padding:3mm; background:#f8fafc; }}
        .ticket-label {{ color:#64748b !important; font-size:7pt; text-transform:uppercase; font-weight:800; }}
        .ticket-value {{ color:#111827 !important; font-size:9pt; font-weight:900; }}
        .ticket-section-title {{ color:#64748b !important; font-size:8pt; text-transform:uppercase; font-weight:900; border-bottom:1px solid #e5e7eb; padding-bottom:1.5mm; margin-top:4mm; }}
        .ticket-info-row {{ display:flex; justify-content:space-between; gap:4mm; font-size:8.5pt; padding:1mm 0; }}
        table {{ width:100%; border-collapse:collapse; }}
        th {{ background:#f8fafc; color:#475569; font-size:7pt; text-align:left; padding:2mm 1.2mm; }}
        td {{ border-top:1px solid #e5e7eb; font-size:8pt; padding:2mm 1.2mm; vertical-align:top; }}
        .ticket-total-pro {{ background:#111827 !important; color:white !important; border-radius:4mm; padding:4mm; margin-top:4mm; display:flex; justify-content:space-between; font-size:15pt; font-weight:900; }}
        .ticket-total-pro * {{ color:white !important; }}
        .ticket-footer {{ text-align:center; color:#64748b !important; font-size:8pt; margin-top:4mm; }}
      </style></head><body>${{el.outerHTML}}</body></html>`);
      w.document.close(); w.focus(); setTimeout(() => {{ w.print(); }}, 350);
    }}
    </script>
    """, height=52)


def generate_receipt_pdf(id_venta: int):
    """Genera comprobante tipo ticket 80 mm, más compatible con impresoras térmicas."""
    if colors is None:
        return None
    venta = query_df("""
        SELECT v.*, COALESCE(c.nombre_cliente,'Cliente general') AS cliente,
               COALESCE(c.telefono,'') AS cliente_telefono,
               COALESCE(c.documento,'') AS cliente_documento,
               COALESCE(c.direccion,'') AS cliente_direccion
        FROM ventas v LEFT JOIN clientes c ON c.id_cliente=v.id_cliente
        WHERE v.id_venta=:id
    """, {"id": id_venta})
    if venta.empty:
        return None
    v = venta.iloc[0]
    det = query_df("SELECT * FROM detalle_ventas WHERE id_venta=:id", {"id": id_venta})
    cfg = cached_settings()
    buffer = io.BytesIO()
    height_cm = max(18, 12 + len(det) * 1.3)
    doc = SimpleDocTemplate(buffer, pagesize=(8.0*cm, height_cm*cm), rightMargin=.35*cm, leftMargin=.35*cm, topMargin=.35*cm, bottomMargin=.35*cm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle('TicketTitle', parent=styles['Title'], fontSize=16, leading=18, textColor=colors.white, alignment=1, fontName='Helvetica-Bold')
    white_small = ParagraphStyle('WhiteSmall', parent=styles['Normal'], fontSize=7.2, leading=9, textColor=colors.white, alignment=1)
    normal = ParagraphStyle('TicketNormal', parent=styles['Normal'], fontSize=7.5, leading=9, textColor=colors.HexColor('#111827'))
    small = ParagraphStyle('TicketSmall', parent=styles['Normal'], fontSize=6.5, leading=8, textColor=colors.HexColor('#64748b'))
    label = ParagraphStyle('TicketLabel', parent=styles['Normal'], fontSize=6.5, leading=8, textColor=colors.HexColor('#64748b'), fontName='Helvetica-Bold')
    story = []
    header = Table([
        [Paragraph(str(cfg.get('store_name') or APP_NAME_DEFAULT).upper(), title)],
        [Paragraph('Comprobante de venta', white_small)],
        [Paragraph(str(cfg.get('direccion') or ''), white_small)],
        [Paragraph('WhatsApp: ' + str(cfg.get('telefono') or '-'), white_small)],
    ], colWidths=[7.2*cm])
    header.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#111827')),
        ('ALIGN',(0,0),(-1,-1),'CENTER'),
        ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),3),
    ]))
    story.append(header); story.append(Spacer(1, .22*cm))
    meta = Table([
        [Paragraph('<b>N°</b>', label), Paragraph(str(v['comprobante']), normal)],
        [Paragraph('<b>Fecha</b>', label), Paragraph(fmt_dt(v['fecha']), normal)],
        [Paragraph('<b>Cliente</b>', label), Paragraph(str(v['cliente']), normal)],
        [Paragraph('<b>Vendedor</b>', label), Paragraph(str(v.get('vendedor_nombre') or '-'), normal)],
        [Paragraph('<b>Pago</b>', label), Paragraph(str(v.get('metodo_pago') or '-'), normal)],
    ], colWidths=[1.8*cm, 5.4*cm])
    meta.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
    story.append(meta); story.append(Spacer(1, .18*cm))
    story.append(Paragraph('DETALLE', label))
    data = [[Paragraph('Cant.', label), Paragraph('Producto', label), Paragraph('Importe', label)]]
    for _, r in det.iterrows():
        data.append([Paragraph(num(r['cantidad']), normal), Paragraph(str(r['producto_nombre'])[:55], normal), Paragraph(money(r['subtotal']), normal)])
    tbl = Table(data, colWidths=[1.0*cm, 4.35*cm, 1.85*cm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f8fafc')),
        ('GRID',(0,0),(-1,-1),0.25,colors.HexColor('#e5e7eb')),
        ('ALIGN',(0,1),(0,-1),'CENTER'),('ALIGN',(2,1),(2,-1),'RIGHT'),
        ('VALIGN',(0,0),(-1,-1),'TOP'),('PADDING',(0,0),(-1,-1),3),
    ]))
    story.append(tbl); story.append(Spacer(1, .22*cm))
    total_tbl = Table([[Paragraph('<b>TOTAL</b>', title), Paragraph('<b>'+money(v['total_venta'])+'</b>', title)]], colWidths=[3.1*cm, 4.1*cm])
    total_tbl.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#111827')),('ALIGN',(1,0),(1,0),'RIGHT'),('PADDING',(0,0),(-1,-1),6)]))
    story.append(total_tbl); story.append(Spacer(1,.15*cm))
    story.append(Paragraph('Pagado: '+money(v.get('monto_pagado',0))+'    Saldo: '+money(v.get('saldo_pendiente',0)), normal))
    story.append(Spacer(1,.18*cm))
    story.append(Paragraph(str(cfg.get('mensaje_comprobante','Gracias por su compra.')), small))
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def inject_css_v25_5():
    st.markdown("""
    <style>
      div[data-testid="stTabs"] button[aria-selected="true"], div[data-testid="stTabs"] button[aria-selected="true"] * { color:#C87966 !important; }
      .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] { background:#E49A86 !important; border-color:#C87966 !important; color:#fff !important; }
      .stButton > button[kind="primary"] *, .stDownloadButton > button[kind="primary"] * { color:#fff !important; }
      .stAlert, .stAlert * { color:#111827 !important; opacity:1 !important; }
      .js-plotly-plot, .plotly, .plot-container { background:#fff !important; color:#111827 !important; }
    </style>
    """, unsafe_allow_html=True)


# ============================================================
# V25.6 OVERRIDES: contraste cálido, panel por vendedor, responsive y reportes claros
# ============================================================
def inject_css_v25_6():
    st.markdown("""
    <style>
      :root { --warm:#E8A06D; --warm-dark:#B96D45; --warm-soft:#FFF3EA; }
      .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {
        background:linear-gradient(135deg,#F2B37F,#E8A06D) !important;
        border-color:#B96D45 !important;
        color:#111827 !important;
        box-shadow:0 8px 18px rgba(232,160,109,.28) !important;
      }
      .stButton > button[kind="primary"] *, .stDownloadButton > button[kind="primary"] * { color:#111827 !important; opacity:1 !important; }
      .stLinkButton a, .stLinkButton a *, div[data-testid="stLinkButton"] a, div[data-testid="stLinkButton"] a * {
        color:#111827 !important; opacity:1 !important; font-weight:900 !important;
      }
      .stLinkButton a, div[data-testid="stLinkButton"] a { background:#ffffff !important; border:1px solid #cbd5e1 !important; border-radius:12px !important; min-height:44px; }
      .chip-dark, .chip-dark * { color:#fff !important; background:#111827 !important; }
      .chip-red { background:#fff3ed !important; color:#9a3412 !important; border-color:#fed7aa !important; }
      .product-card { min-height:265px !important; padding:14px !important; border-radius:18px !important; }
      .product-img-wrap { height:145px !important; border-radius:14px !important; }
      .product-name { font-size:16px !important; min-height:42px !important; }
      .product-price { font-size:24px !important; }
      .product-desc { max-height:38px; overflow:hidden; }
      .clomar-table td, .clomar-table th { color:#111827 !important; }
      .js-plotly-plot .plotly, .js-plotly-plot .main-svg { background:#fff !important; }
      .clomar-hero { background:linear-gradient(135deg,#111827,#1e293b) !important; }
      .clomar-hero, .clomar-hero * { color:#fff !important; opacity:1 !important; }
      @media (max-width: 900px) {
        .block-container { padding-left:.55rem !important; padding-right:.55rem !important; }
        .clomar-hero { padding:14px 16px !important; border-radius:0 0 16px 16px !important; }
        .clomar-hero h1 { font-size:24px !important; }
        .clomar-hero p { font-size:13px !important; }
        .kpi-card { padding:14px !important; }
        .kpi-value { font-size:26px !important; }
        .product-card { min-height:auto !important; }
        .product-img-wrap { height:150px !important; }
      }
      @media print {
        @page { size:80mm auto; margin:2mm; }
        header[data-testid="stHeader"], section[data-testid="stSidebar"], div[data-testid="stToolbar"], .no-print, .stButton, .stDownloadButton, iframe { display:none !important; }
        #ticket-print-area { width:76mm !important; max-width:76mm !important; margin:0 auto !important; }
        .ticket-pro { box-shadow:none !important; border:0 !important; border-radius:0 !important; }
        .ticket-top { padding:6mm 4mm !important; }
        .ticket-body { padding:4mm !important; }
      }
    </style>
    """, unsafe_allow_html=True)


def _vendor_summary(ventas: pd.DataFrame, detalle: pd.DataFrame) -> pd.DataFrame:
    if ventas.empty:
        return pd.DataFrame()
    base = ventas.copy()
    base["vendedor_nombre"] = base["vendedor_nombre"].fillna("Sin vendedor")
    res = base.groupby("vendedor_nombre", as_index=False).agg(
        comprobantes=("id_venta", "count"),
        total_venta=("total_venta", "sum"),
        cobrado=("monto_pagado", "sum"),
        saldo=("saldo_pendiente", "sum"),
    )
    if detalle is not None and not detalle.empty:
        util = detalle.groupby("vendedor", as_index=False).agg(utilidad=("utilidad", "sum"), productos=("cantidad", "sum"))
        res = res.merge(util, left_on="vendedor_nombre", right_on="vendedor", how="left").drop(columns=["vendedor"], errors="ignore")
    if "utilidad" not in res.columns:
        res["utilidad"] = 0
    if "productos" not in res.columns:
        res["productos"] = 0
    res["ticket_promedio"] = res["total_venta"] / res["comprobantes"].replace(0, 1)
    return res.sort_values("total_venta", ascending=False)


def page_panel_dueno():
    hero("Panel del dueño", "Control rápido de ventas, caja, stock y rendimiento por vendedor.", "📊")
    c1, c2 = st.columns(2)
    with c1:
        fecha_desde = st.date_input("Desde", peru_today(), key="pd_desde_v256")
    with c2:
        fecha_hasta = st.date_input("Hasta", peru_today(), key="pd_hasta_v256")
    ventas = ventas_periodo(fecha_desde, fecha_hasta)
    detalle = detalle_productos_vendidos(fecha_desde, fecha_hasta)
    productos = productos_con_stock()
    caja = query_df("SELECT * FROM caja WHERE DATE(fecha) BETWEEN :d AND :h AND COALESCE(anulada,0)=0", {"d": str(fecha_desde), "h": str(fecha_hasta)})
    total_ventas = float(ventas["total_venta"].sum()) if not ventas.empty else 0
    utilidad = float(detalle["utilidad"].sum()) if not detalle.empty else 0
    ticket = total_ventas / len(ventas) if len(ventas) else 0
    ingresos = caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum() if not caja.empty else 0
    egresos = caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum() if not caja.empty else 0
    a,b,c,d = st.columns(4)
    with a: kpi("Ventas", money(total_ventas), f"{len(ventas)} comprobantes")
    with b: kpi("Utilidad estimada", money(utilidad), "Según costo registrado")
    with c: kpi("Ticket promedio", money(ticket), "Promedio por venta")
    with d: kpi("Caja neta", money(ingresos-egresos), "Ingresos - egresos")

    resumen = _vendor_summary(ventas, detalle)
    st.subheader("Ventas por vendedor")
    if resumen.empty:
        st.info("No hay ventas por vendedor en el período.")
    else:
        show = resumen.copy()
        show["total_fmt"] = show["total_venta"].apply(money)
        show["cobrado_fmt"] = show["cobrado"].apply(money)
        show["saldo_fmt"] = show["saldo"].apply(money)
        show["utilidad_fmt"] = show["utilidad"].apply(money)
        html_table(show, ["vendedor_nombre","comprobantes","productos","total_fmt","cobrado_fmt","saldo_fmt","utilidad_fmt"], ["Vendedor","Ventas","Unid.","Total","Cobrado","Crédito","Utilidad"], 20)
        figv = px.bar(resumen, x="vendedor_nombre", y="total_venta", text="total_venta", title="Total vendido por vendedor")
        figv.update_traces(texttemplate="S/ %{text:,.0f}", textposition="outside", marker_color="#E8A06D")
        figv.update_layout(template="plotly_white", plot_bgcolor="white", paper_bgcolor="white", font_color="#111827", title_font_color="#111827", margin=dict(l=20,r=20,t=50,b=20))
        figv.update_xaxes(color="#111827", gridcolor="#e5e7eb")
        figv.update_yaxes(color="#111827", gridcolor="#e5e7eb")
        st.plotly_chart(figv, use_container_width=True)

    x1,x2 = st.columns([1.35,1])
    with x1:
        st.subheader("Ventas recientes")
        if ventas.empty:
            st.info("Todavía no hay ventas en el período.")
        else:
            vv = ventas.copy()
            vv["fecha_fmt"] = vv["fecha"].apply(fmt_dt)
            vv["total_fmt"] = vv["total_venta"].apply(money)
            html_table(vv, ["comprobante","fecha_fmt","cliente","vendedor_nombre","metodo_pago","total_fmt"], ["Comprobante","Fecha Perú","Cliente","Vendedor","Pago","Total"], 10)
    with x2:
        st.subheader("Stock crítico")
        crit = productos[productos["stock_actual"].astype(float) <= productos["stock_minimo"].astype(float)] if not productos.empty else pd.DataFrame()
        if crit.empty:
            st.success("Sin productos críticos.")
        else:
            for _, r in crit.head(10).iterrows():
                st.markdown(f"<span class='chip chip-red'>⚠️ {esc(r['nombre_producto'])} · Stock {num(r['stock_actual'])}</span>", unsafe_allow_html=True)



def _bar_report_html(df: pd.DataFrame, label_col: str, value_col: str, title: str, limit: int = 10, money_values: bool = True):
    if df is None or df.empty or value_col not in df.columns:
        st.markdown(f"<div class='report-card'><h3>{esc(title)}</h3><div class='empty-chart'>Sin datos para mostrar.</div></div>", unsafe_allow_html=True)
        return
    data = df.copy().sort_values(value_col, ascending=False).head(limit)
    maxv = float(data[value_col].max() or 0)
    rows = []
    for _, r in data.iterrows():
        val = float(r.get(value_col) or 0)
        pct = 0 if maxv <= 0 else max(3, min(100, (val / maxv) * 100))
        label = esc(r.get(label_col) if pd.notna(r.get(label_col)) else "Sin dato")
        value = money(val) if money_values else num(val)
        rows.append(f"""
        <div class='bar-row'>
            <div class='bar-line'><span>{label}</span><span>{value}</span></div>
            <div class='bar-track'><div class='bar-fill' style='width:{pct:.1f}%'></div></div>
        </div>
        """)
    st.markdown(f"<div class='report-card'><h3>{esc(title)}</h3>{''.join(rows)}</div>", unsafe_allow_html=True)


def page_reportes():
    hero("Reportes", "Control claro por vendedor, producto, fecha, método de pago y créditos.", "📈")
    if not is_admin():
        st.warning("Solo administrador puede ver reportes.")
        return
    c1, c2 = st.columns(2)
    with c1:
        desde = st.date_input("Desde", peru_today() - timedelta(days=7), key="repd_v257")
    with c2:
        hasta = st.date_input("Hasta", peru_today(), key="reph_v257")

    ventas = ventas_periodo(desde, hasta)
    detalle = detalle_productos_vendidos(desde, hasta)
    if ventas.empty:
        st.info("No hay ventas en el rango seleccionado.")
        return

    total = float(ventas["total_venta"].sum()) if "total_venta" in ventas.columns else 0
    cobrado = float(ventas["monto_pagado"].sum()) if "monto_pagado" in ventas.columns else 0
    credito = float(ventas["saldo_pendiente"].sum()) if "saldo_pendiente" in ventas.columns else 0
    utilidad = float(detalle["utilidad"].sum()) if detalle is not None and not detalle.empty and "utilidad" in detalle.columns else 0
    ticket = total / max(len(ventas), 1)

    k1, k2, k3, k4 = st.columns(4)
    with k1: kpi("Ventas", money(total), f"{len(ventas)} comprobantes")
    with k2: kpi("Cobrado", money(cobrado), "Ingreso recibido")
    with k3: kpi("Crédito", money(credito), "Saldo pendiente")
    with k4: kpi("Ticket prom.", money(ticket), "Promedio por venta")

    resumen = _vendor_summary(ventas, detalle)
    st.subheader("Ventas por vendedor")
    if not resumen.empty:
        show = resumen.copy()
        show["total_fmt"] = show["total_venta"].apply(money)
        show["cobrado_fmt"] = show["cobrado"].apply(money)
        show["saldo_fmt"] = show["saldo"].apply(money)
        show["utilidad_fmt"] = show["utilidad"].apply(money)
        show["ticket_fmt"] = show["ticket_promedio"].apply(money)
        html_table(show, ["vendedor_nombre", "comprobantes", "total_fmt", "cobrado_fmt", "saldo_fmt", "utilidad_fmt", "ticket_fmt"], ["Vendedor", "Ventas", "Total", "Cobrado", "Crédito", "Utilidad", "Ticket"], 50)
        _bar_report_html(resumen, "vendedor_nombre", "total_venta", "Ranking visual por vendedor", 8, True)

    ventas2 = ventas.copy()
    ventas2["dia"] = ventas2["fecha"].apply(lambda x: _to_peru_dt(x).strftime("%d/%m") if _to_peru_dt(x) else str(x)[:10])
    diario = ventas2.groupby("dia", as_index=False)["total_venta"].sum()
    metodo = ventas2.groupby("metodo_pago", as_index=False)["total_venta"].sum() if "metodo_pago" in ventas2.columns else pd.DataFrame()
    col_a, col_b = st.columns(2)
    with col_a:
        _bar_report_html(diario, "dia", "total_venta", "Ventas por día", 10, True)
    with col_b:
        _bar_report_html(metodo, "metodo_pago", "total_venta", "Métodos de pago", 8, True)

    st.subheader("Productos vendidos")
    if detalle is not None and not detalle.empty:
        top = detalle.groupby("producto", as_index=False).agg(total_vendido=("total_vendido", "sum"), cantidad=("cantidad", "sum"), utilidad=("utilidad", "sum")).sort_values("total_vendido", ascending=False)
        _bar_report_html(top, "producto", "total_vendido", "Productos con mayor venta", 10, True)
        top_show = top.copy()
        top_show["total_fmt"] = top_show["total_vendido"].apply(money)
        top_show["utilidad_fmt"] = top_show["utilidad"].apply(money)
        html_table(top_show, ["producto", "cantidad", "total_fmt", "utilidad_fmt"], ["Producto", "Cantidad", "Total vendido", "Utilidad"], 100)
    else:
        st.info("No hay detalle de productos vendidos.")

    st.subheader("Detalle de comprobantes")
    detv = ventas.copy()
    detv["fecha_fmt"] = detv["fecha"].apply(lambda x: fmt_dt_peru(x))
    detv["total_fmt"] = detv["total_venta"].apply(money)
    detv["pagado_fmt"] = detv["monto_pagado"].apply(money)
    detv["saldo_fmt"] = detv["saldo_pendiente"].apply(money)
    html_table(detv, ["comprobante", "fecha_fmt", "cliente", "vendedor_nombre", "metodo_pago", "total_fmt", "pagado_fmt", "saldo_fmt", "estado_pago"], ["Comprobante", "Fecha", "Cliente", "Vendedor", "Pago", "Total", "Pagado", "Saldo", "Estado"], 200)



def inject_css_v25_7():
    st.markdown("""
    <style>
      /* V25.7 final override: botones negros y visual compacto */
      .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"], [data-testid="stFormSubmitButton"] button, button[data-testid="baseButton-primary"] {
        background:#0f172a !important;
        color:#ffffff !important;
        border:1px solid #0f172a !important;
        box-shadow:0 8px 18px rgba(15,23,42,.18) !important;
      }
      .stButton > button[kind="primary"] *, .stDownloadButton > button[kind="primary"] *, [data-testid="stFormSubmitButton"] button *, button[data-testid="baseButton-primary"] * {
        color:#ffffff !important; opacity:1 !important;
      }
      .stButton > button[kind="primary"] p, .stDownloadButton > button[kind="primary"] p, [data-testid="stFormSubmitButton"] button p, button[data-testid="baseButton-primary"] p {
        color:#ffffff !important; opacity:1 !important;
      }
      .product-card { min-height:265px !important; padding:14px !important; border-radius:18px !important; gap:6px !important; }
      .product-img-wrap { height:132px !important; margin-bottom:4px !important; border-radius:14px !important; }
      .product-name { font-size:15px !important; line-height:1.25 !important; min-height:38px !important; }
      .product-meta { font-size:12px !important; line-height:1.28 !important; }
      .product-price { font-size:23px !important; margin-top:4px !important; }
      .chip { padding:6px 10px !important; font-size:11px !important; }
      .report-card { background:#fff; border:1px solid #e5e7eb; border-radius:20px; padding:18px; box-shadow:0 8px 25px rgba(15,23,42,.06); }
      .report-card h3 { margin:0 0 12px; color:#111827; font-weight:950; }
      .bar-row { margin:12px 0; }
      .bar-line { display:flex; justify-content:space-between; gap:10px; color:#111827; font-weight:850; font-size:13px; margin-bottom:6px; }
      .bar-track { background:#f1f5f9; height:12px; border-radius:999px; overflow:hidden; border:1px solid #e2e8f0; }
      .bar-fill { background:#0f172a; height:100%; border-radius:999px; }
      .empty-chart { background:#fff; border:1px dashed #cbd5e1; border-radius:18px; padding:26px; color:#64748b; font-weight:800; text-align:center; }
      @media (min-width: 1500px) { .product-img-wrap { height:145px !important; } .product-name { font-size:16px !important; } }
      @media (max-width: 900px) {
        .product-card { min-height:auto !important; }
        .product-img-wrap { height:180px !important; }
        .block-container { padding-left:.5rem !important; padding-right:.5rem !important; }
      }
    </style>
    """, unsafe_allow_html=True)



# ============================================================
# V25.8 - CAJA DIARIA + CRÉDITOS POR CLIENTE + REPORTES FIX
# ============================================================
def fmt_dt_peru(v):
    """Alias estable: evita errores en reportes si una versión previa lo invoca."""
    try:
        return fmt_dt(v)
    except Exception:
        return str(v or "")


def ensure_v25_8_schema():
    """Migraciones suaves para control de cierres y abonos por producto."""
    try:
        safe_alter("pagos_credito", "id_detalle INTEGER")
        safe_alter("pagos_credito", "producto_nombre VARCHAR(240) DEFAULT ''")
    except Exception:
        pass
    try:
        exec_sql(f"""
            CREATE TABLE IF NOT EXISTS cierres_caja (
                id_cierre {id_sql()},
                fecha_cierre DATE,
                total_ingresos NUMERIC(12,2) DEFAULT 0,
                total_egresos NUMERIC(12,2) DEFAULT 0,
                saldo_sistema NUMERIC(12,2) DEFAULT 0,
                efectivo_sistema NUMERIC(12,2) DEFAULT 0,
                efectivo_contado NUMERIC(12,2) DEFAULT 0,
                diferencia NUMERIC(12,2) DEFAULT 0,
                id_usuario INTEGER,
                observacion TEXT DEFAULT '',
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    except Exception:
        pass


def _safe_float(v, default=0.0):
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _bar_report_html(df: pd.DataFrame, label_col: str, value_col: str, title: str, limit: int = 10, money_values: bool = True):
    """Barras HTML sin indentación para que Streamlit no las muestre como código."""
    if df is None or df.empty or value_col not in df.columns:
        st.markdown(f"<div class='report-card'><h3>{esc(title)}</h3><div class='empty-chart'>Sin datos para mostrar.</div></div>", unsafe_allow_html=True)
        return
    data = df.copy()
    data[value_col] = pd.to_numeric(data[value_col], errors='coerce').fillna(0)
    data = data.sort_values(value_col, ascending=False).head(limit)
    maxv = float(data[value_col].max() or 0)
    rows = []
    for _, r in data.iterrows():
        val = _safe_float(r.get(value_col))
        pct = 0 if maxv <= 0 else max(3, min(100, (val / maxv) * 100))
        raw_label = r.get(label_col)
        label = esc(raw_label if pd.notna(raw_label) and str(raw_label).strip() else "Sin dato")
        value = money(val) if money_values else num(val)
        rows.append("<div class='bar-row'>"
                    f"<div class='bar-line'><span>{label}</span><span>{value}</span></div>"
                    f"<div class='bar-track'><div class='bar-fill' style='width:{pct:.1f}%'></div></div>"
                    "</div>")
    html = f"<div class='report-card'><h3>{esc(title)}</h3>{''.join(rows)}</div>"
    st.markdown(html, unsafe_allow_html=True)


def _format_fecha_col(df: pd.DataFrame, col: str = "fecha"):
    out = df.copy()
    if col in out.columns:
        out[col + "_fmt"] = out[col].apply(fmt_dt)
    return out


def _venta_detalle(id_venta: int) -> pd.DataFrame:
    return query_df("""
        SELECT dv.id_detalle, dv.id_venta, dv.id_producto, dv.producto_nombre, dv.cantidad,
               dv.precio_unitario, dv.subtotal,
               COALESCE(SUM(pc.monto),0) AS abonado_producto
        FROM detalle_ventas dv
        LEFT JOIN pagos_credito pc ON pc.id_detalle=dv.id_detalle
        WHERE dv.id_venta=:idv
        GROUP BY dv.id_detalle, dv.id_venta, dv.id_producto, dv.producto_nombre, dv.cantidad, dv.precio_unitario, dv.subtotal
        ORDER BY dv.id_detalle
    """, {"idv": int(id_venta)})


def _creditos_pendientes_df() -> pd.DataFrame:
    return query_df("""
        SELECT v.id_venta, v.id_cliente, v.comprobante, v.fecha, v.fecha_vencimiento, v.tipo_venta, v.metodo_pago,
               v.total_venta, v.monto_pagado, v.saldo_pendiente, v.estado_pago,
               COALESCE(c.nombre_cliente,'Cliente general') AS cliente,
               COALESCE(c.telefono,'') AS telefono,
               COALESCE(c.documento,'') AS documento
        FROM ventas v
        LEFT JOIN clientes c ON c.id_cliente=v.id_cliente
        WHERE COALESCE(v.anulada,0)=0 AND COALESCE(v.saldo_pendiente,0) > 0
        ORDER BY COALESCE(v.fecha_vencimiento, DATE(v.fecha)) ASC, v.fecha DESC
    """)


def page_panel_dueno():
    hero("Panel del dueño", "Control rápido de ventas, caja, stock y rendimiento por vendedor.", "📊")
    c1, c2 = st.columns(2)
    with c1:
        fecha_desde = st.date_input("Desde", peru_today(), key="pd_desde_v258")
    with c2:
        fecha_hasta = st.date_input("Hasta", peru_today(), key="pd_hasta_v258")

    ventas = ventas_periodo(fecha_desde, fecha_hasta)
    detalle = detalle_productos_vendidos(fecha_desde, fecha_hasta)
    productos = productos_con_stock()
    caja = query_df("SELECT * FROM caja WHERE DATE(fecha) BETWEEN :d AND :h AND COALESCE(anulada,0)=0", {"d": str(fecha_desde), "h": str(fecha_hasta)})

    total_ventas = _safe_float(ventas["total_venta"].sum()) if not ventas.empty else 0
    utilidad = _safe_float(detalle["utilidad"].sum()) if detalle is not None and not detalle.empty and "utilidad" in detalle.columns else 0
    ticket = total_ventas / max(len(ventas), 1)
    ingresos = _safe_float(caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum()) if not caja.empty else 0
    egresos = _safe_float(caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum()) if not caja.empty else 0

    a,b,c,d = st.columns(4)
    with a: kpi("Ventas", money(total_ventas), f"{len(ventas)} comprobantes")
    with b: kpi("Utilidad estimada", money(utilidad), "Según costo registrado")
    with c: kpi("Ticket promedio", money(ticket), "Promedio por venta")
    with d: kpi("Caja neta", money(ingresos-egresos), "Ingresos - egresos")

    resumen = _vendor_summary(ventas, detalle)
    st.subheader("Ventas por vendedor")
    if resumen.empty:
        st.info("No hay ventas por vendedor en el período.")
    else:
        show = resumen.copy()
        show["total_fmt"] = show["total_venta"].apply(money)
        show["cobrado_fmt"] = show["cobrado"].apply(money)
        show["saldo_fmt"] = show["saldo"].apply(money)
        show["utilidad_fmt"] = show["utilidad"].apply(money)
        html_table(show, ["vendedor_nombre","comprobantes","productos","total_fmt","cobrado_fmt","saldo_fmt","utilidad_fmt"], ["Vendedor","Ventas","Unid.","Total","Cobrado","Crédito","Utilidad"], 20)
        _bar_report_html(resumen, "vendedor_nombre", "total_venta", "Ranking visual por vendedor", 8, True)

    x1,x2 = st.columns([1.35,1])
    with x1:
        st.subheader("Ventas recientes")
        if ventas.empty:
            st.info("Todavía no hay ventas en el período.")
        else:
            vv = ventas.copy()
            vv["fecha_fmt"] = vv["fecha"].apply(fmt_dt)
            vv["total_fmt"] = vv["total_venta"].apply(money)
            html_table(vv, ["comprobante","fecha_fmt","cliente","vendedor_nombre","metodo_pago","total_fmt"], ["Comprobante","Fecha Perú","Cliente","Vendedor","Pago","Total"], 10)
    with x2:
        st.subheader("Stock crítico")
        crit = productos[productos["stock_actual"].astype(float) <= productos["stock_minimo"].astype(float)] if not productos.empty else pd.DataFrame()
        if crit.empty:
            st.success("Sin productos críticos.")
        else:
            for _, r in crit.head(10).iterrows():
                st.markdown(f"<span class='chip chip-red'>⚠️ {esc(r['nombre_producto'])} · Stock {num(r['stock_actual'])}</span>", unsafe_allow_html=True)


def page_reportes():
    hero("Reportes", "Ventas por vendedor, producto, método de pago, crédito y comprobantes.", "📈")
    if not is_admin():
        st.warning("Solo administrador puede ver reportes.")
        return
    c1, c2 = st.columns(2)
    with c1:
        desde = st.date_input("Desde", peru_today() - timedelta(days=7), key="repd_v258")
    with c2:
        hasta = st.date_input("Hasta", peru_today(), key="reph_v258")

    ventas = ventas_periodo(desde, hasta)
    detalle = detalle_productos_vendidos(desde, hasta)
    if ventas.empty:
        st.info("No hay ventas en el rango seleccionado.")
        return

    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        if col in ventas.columns:
            ventas[col] = pd.to_numeric(ventas[col], errors="coerce").fillna(0)
    total = _safe_float(ventas["total_venta"].sum())
    cobrado = _safe_float(ventas["monto_pagado"].sum())
    credito = _safe_float(ventas["saldo_pendiente"].sum())
    utilidad = _safe_float(detalle["utilidad"].sum()) if detalle is not None and not detalle.empty and "utilidad" in detalle.columns else 0
    ticket = total / max(len(ventas), 1)

    k1, k2, k3, k4 = st.columns(4)
    with k1: kpi("Ventas", money(total), f"{len(ventas)} comprobantes")
    with k2: kpi("Cobrado", money(cobrado), "Ingreso recibido")
    with k3: kpi("Crédito", money(credito), "Saldo pendiente")
    with k4: kpi("Ticket prom.", money(ticket), "Promedio por venta")

    resumen = _vendor_summary(ventas, detalle)
    st.subheader("Ventas por vendedor")
    if not resumen.empty:
        show = resumen.copy()
        show["total_fmt"] = show["total_venta"].apply(money)
        show["cobrado_fmt"] = show["cobrado"].apply(money)
        show["saldo_fmt"] = show["saldo"].apply(money)
        show["utilidad_fmt"] = show["utilidad"].apply(money)
        show["ticket_fmt"] = show["ticket_promedio"].apply(money)
        html_table(show, ["vendedor_nombre", "comprobantes", "productos", "total_fmt", "cobrado_fmt", "saldo_fmt", "utilidad_fmt", "ticket_fmt"], ["Vendedor", "Ventas", "Unid.", "Total", "Cobrado", "Crédito", "Utilidad", "Ticket"], 50)
        _bar_report_html(resumen, "vendedor_nombre", "total_venta", "Ranking visual por vendedor", 8, True)

    ventas2 = ventas.copy()
    ventas2["dia"] = ventas2["fecha"].apply(lambda x: _to_peru_dt(x).strftime("%d/%m") if _to_peru_dt(x) else str(x)[:10])
    diario = ventas2.groupby("dia", as_index=False)["total_venta"].sum()
    metodo = ventas2.groupby("metodo_pago", as_index=False)["total_venta"].sum() if "metodo_pago" in ventas2.columns else pd.DataFrame()
    c1, c2 = st.columns(2)
    with c1:
        _bar_report_html(diario, "dia", "total_venta", "Ventas por día", 12, True)
    with c2:
        _bar_report_html(metodo, "metodo_pago", "total_venta", "Métodos de pago", 8, True)

    st.subheader("Productos vendidos")
    if detalle is not None and not detalle.empty:
        for col in ["total_vendido", "cantidad", "utilidad"]:
            if col in detalle.columns:
                detalle[col] = pd.to_numeric(detalle[col], errors="coerce").fillna(0)
        top = detalle.groupby("producto", as_index=False).agg(total_vendido=("total_vendido", "sum"), cantidad=("cantidad", "sum"), utilidad=("utilidad", "sum")).sort_values("total_vendido", ascending=False)
        _bar_report_html(top, "producto", "total_vendido", "Productos con mayor venta", 10, True)
        top_show = top.copy()
        top_show["total_fmt"] = top_show["total_vendido"].apply(money)
        top_show["utilidad_fmt"] = top_show["utilidad"].apply(money)
        html_table(top_show, ["producto", "cantidad", "total_fmt", "utilidad_fmt"], ["Producto", "Cantidad", "Total vendido", "Utilidad"], 100)
    else:
        st.info("No hay detalle de productos vendidos.")

    st.subheader("Detalle de comprobantes")
    detv = ventas.copy()
    detv["fecha_fmt"] = detv["fecha"].apply(fmt_dt)
    detv["total_fmt"] = detv["total_venta"].apply(money)
    detv["pagado_fmt"] = detv["monto_pagado"].apply(money)
    detv["saldo_fmt"] = detv["saldo_pendiente"].apply(money)
    html_table(detv, ["comprobante", "fecha_fmt", "cliente", "vendedor_nombre", "metodo_pago", "total_fmt", "pagado_fmt", "saldo_fmt", "estado_pago"], ["Comprobante", "Fecha", "Cliente", "Vendedor", "Pago", "Total", "Pagado", "Saldo", "Estado"], 200)


def page_caja():
    hero("Caja", "Cierre diario, ingresos, egresos, pagos por método y diferencias.", "💰")
    if not is_admin():
        st.warning("Solo administrador puede ver caja.")
        return
    ensure_v25_8_schema()
    f = st.date_input("Fecha de caja", peru_today(), key="caja_fecha_v258")
    caja = query_df("SELECT * FROM caja WHERE DATE(fecha)=:f AND COALESCE(anulada,0)=0 ORDER BY fecha DESC", {"f": str(f)})
    ventas = ventas_periodo(f, f)
    ingresos = _safe_float(caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum()) if not caja.empty else 0
    egresos = _safe_float(caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum()) if not caja.empty else 0
    neto = ingresos - egresos
    venta_total = _safe_float(ventas["total_venta"].sum()) if not ventas.empty else 0
    cobrado = _safe_float(ventas["monto_pagado"].sum()) if not ventas.empty else 0
    credito = _safe_float(ventas["saldo_pendiente"].sum()) if not ventas.empty else 0

    a,b,c,d = st.columns(4)
    with a: kpi("Ventas del día", money(venta_total), f"{len(ventas)} comprobantes")
    with b: kpi("Cobrado", money(cobrado), "Ventas + abonos")
    with c: kpi("Crédito generado", money(credito), "Pendiente de cobro")
    with d: kpi("Caja neta", money(neto), "Ingresos - egresos")

    st.subheader("Cierre diario recomendado")
    if caja.empty:
        st.info("No hay movimientos de caja para la fecha.")
    else:
        mov = caja.copy()
        mov["monto"] = pd.to_numeric(mov["monto"], errors="coerce").fillna(0)
        por_metodo = mov.groupby(["metodo_pago", "tipo"], as_index=False)["monto"].sum()
        piv = por_metodo.pivot_table(index="metodo_pago", columns="tipo", values="monto", aggfunc="sum", fill_value=0).reset_index()
        if "Ingreso" not in piv.columns: piv["Ingreso"] = 0
        if "Egreso" not in piv.columns: piv["Egreso"] = 0
        piv["Neto"] = piv["Ingreso"] - piv["Egreso"]
        piv_show = piv.copy()
        for col in ["Ingreso", "Egreso", "Neto"]:
            piv_show[col + "_fmt"] = piv_show[col].apply(money)
        html_table(piv_show, ["metodo_pago", "Ingreso_fmt", "Egreso_fmt", "Neto_fmt"], ["Método", "Ingresos", "Egresos", "Neto"], 20)

    efectivo_sistema = 0
    if not caja.empty:
        tmp = caja.copy()
        tmp["monto"] = pd.to_numeric(tmp["monto"], errors="coerce").fillna(0)
        efectivo_ing = tmp[(tmp["metodo_pago"].astype(str).str.lower().eq("efectivo")) & (tmp["tipo"].astype(str).str.lower().eq("ingreso"))]["monto"].sum()
        efectivo_egr = tmp[(tmp["metodo_pago"].astype(str).str.lower().eq("efectivo")) & (tmp["tipo"].astype(str).str.lower().eq("egreso"))]["monto"].sum()
        efectivo_sistema = _safe_float(efectivo_ing - efectivo_egr)

    with st.form("form_cierre_caja_v258"):
        x1,x2 = st.columns(2)
        with x1:
            efectivo_contado = st.number_input("Efectivo contado físico", min_value=0.0, value=float(max(efectivo_sistema,0)), step=1.0)
        with x2:
            obs = st.text_input("Observación de cierre", placeholder="Ej: cierre correcto, faltante, sobrante")
        diferencia = efectivo_contado - efectivo_sistema
        st.markdown(f"<div class='card'><b>Efectivo sistema:</b> {money(efectivo_sistema)} &nbsp; <b>Diferencia:</b> <span class='chip {'chip-ok' if abs(diferencia) < 0.01 else 'chip-red'}'>{money(diferencia)}</span></div>", unsafe_allow_html=True)
        guardar = st.form_submit_button("Guardar cierre diario", type="primary", use_container_width=True)
    if guardar:
        exec_sql("""
            INSERT INTO cierres_caja (fecha_cierre,total_ingresos,total_egresos,saldo_sistema,efectivo_sistema,efectivo_contado,diferencia,id_usuario,observacion)
            VALUES (:f,:ing,:egr,:neto,:efs,:efc,:dif,:uid,:obs)
        """, {"f": str(f), "ing": ingresos, "egr": egresos, "neto": neto, "efs": efectivo_sistema, "efc": efectivo_contado, "dif": diferencia, "uid": current_user()["id_usuario"], "obs": obs})
        st.success("Cierre diario guardado.")

    st.subheader("Movimientos de caja")
    if not caja.empty:
        ctab = caja.copy()
        ctab["fecha_fmt"] = ctab["fecha"].apply(fmt_dt)
        ctab["monto_fmt"] = ctab["monto"].apply(money)
        html_table(ctab, ["fecha_fmt","tipo","concepto","metodo_pago","monto_fmt","referencia"], ["Fecha","Tipo","Concepto","Pago","Monto","Ref"], 200)

    with st.expander("Registrar egreso / salida"):
        with st.form("egreso_v258"):
            concepto = st.text_input("Concepto")
            monto = st.number_input("Monto", min_value=0.0, step=1.0)
            metodo = st.selectbox("Método", ["Efectivo","Yape","Plin","Transferencia","Tarjeta"])
            if st.form_submit_button("Registrar egreso", type="primary"):
                exec_sql("INSERT INTO caja (tipo,concepto,metodo_pago,monto,id_usuario) VALUES ('Egreso',:c,:m,:mo,:u)", {"c":concepto,"m":metodo,"mo":monto,"u":current_user()["id_usuario"]})
                clear_report_cache(); st.success("Egreso registrado."); st.rerun()

    with st.expander("Historial de cierres guardados", expanded=False):
        cierres = query_df("SELECT * FROM cierres_caja ORDER BY creado_en DESC LIMIT 30")
        if cierres.empty:
            st.info("Todavía no hay cierres guardados.")
        else:
            cc = cierres.copy()
            for col in ["total_ingresos", "total_egresos", "saldo_sistema", "efectivo_sistema", "efectivo_contado", "diferencia"]:
                cc[col + "_fmt"] = cc[col].apply(money)
            html_table(cc, ["fecha_cierre", "total_ingresos_fmt", "total_egresos_fmt", "saldo_sistema_fmt", "efectivo_sistema_fmt", "efectivo_contado_fmt", "diferencia_fmt", "observacion"], ["Fecha", "Ingresos", "Egresos", "Saldo", "Efec. sistema", "Efec. contado", "Dif.", "Observación"], 30)


def page_creditos():
    hero("Créditos / cuentas por cobrar", "Saldos por cliente, abonos parciales y control por producto.", "💳")
    if not is_admin():
        st.warning("Solo administrador puede ver créditos y cuentas por cobrar.")
        return
    ensure_v25_8_schema()
    pendientes = _creditos_pendientes_df()
    total_pendiente = _safe_float(pendientes["saldo_pendiente"].sum()) if not pendientes.empty else 0
    vencidas = 0
    if not pendientes.empty and "fecha_vencimiento" in pendientes.columns:
        fv = pd.to_datetime(pendientes["fecha_vencimiento"], errors="coerce").dt.date
        vencidas = int(((fv < peru_today()) & pendientes["saldo_pendiente"].astype(float).gt(0)).sum())
    c1,c2,c3 = st.columns(3)
    with c1: kpi("Total por cobrar", money(total_pendiente), "Saldo pendiente")
    with c2: kpi("Ventas pendientes", str(len(pendientes)), "Créditos abiertos")
    with c3: kpi("Vencidas", str(vencidas), "Requieren seguimiento")

    st.subheader("Resumen por cliente")
    if pendientes.empty:
        st.success("No hay cuentas por cobrar pendientes.")
        return
    grp = pendientes.copy()
    grp["total_venta"] = pd.to_numeric(grp["total_venta"], errors="coerce").fillna(0)
    grp["monto_pagado"] = pd.to_numeric(grp["monto_pagado"], errors="coerce").fillna(0)
    grp["saldo_pendiente"] = pd.to_numeric(grp["saldo_pendiente"], errors="coerce").fillna(0)
    cli = grp.groupby(["cliente", "telefono"], as_index=False).agg(comprobantes=("id_venta","count"), total=("total_venta","sum"), pagado=("monto_pagado","sum"), saldo=("saldo_pendiente","sum"))
    cli_show = cli.copy()
    cli_show["total_fmt"] = cli_show["total"].apply(money)
    cli_show["pagado_fmt"] = cli_show["pagado"].apply(money)
    cli_show["saldo_fmt"] = cli_show["saldo"].apply(money)
    html_table(cli_show, ["cliente", "telefono", "comprobantes", "total_fmt", "pagado_fmt", "saldo_fmt"], ["Cliente", "Teléfono", "Ventas", "Total", "Pagado", "Saldo"], 100)

    st.subheader("Registrar abono / pago parcial")
    clientes_opts = list(cli.sort_values("cliente")["cliente"])
    cliente_sel = st.selectbox("Cliente", clientes_opts, key="credito_cliente_v258")
    pend_cli = pendientes[pendientes["cliente"].astype(str).eq(str(cliente_sel))].copy()
    opts = {f"{r['comprobante']} · Saldo {money(r['saldo_pendiente'])} · {fmt_dt(r['fecha'])}": int(r["id_venta"]) for _, r in pend_cli.iterrows()}
    venta_label = st.selectbox("Comprobante pendiente", list(opts.keys()), key="credito_venta_v258")
    idv = opts[venta_label]
    venta_sel = pend_cli[pend_cli["id_venta"].eq(idv)].iloc[0]
    saldo_actual = _safe_float(venta_sel["saldo_pendiente"])
    detalles = _venta_detalle(idv)
    if not detalles.empty:
        det_show = detalles.copy()
        det_show["subtotal_fmt"] = det_show["subtotal"].apply(money)
        det_show["abonado_fmt"] = det_show["abonado_producto"].apply(money)
        det_show["saldo_producto"] = pd.to_numeric(det_show["subtotal"], errors="coerce").fillna(0) - pd.to_numeric(det_show["abonado_producto"], errors="coerce").fillna(0)
        det_show["saldo_producto_fmt"] = det_show["saldo_producto"].apply(money)
        html_table(det_show, ["producto_nombre", "cantidad", "subtotal_fmt", "abonado_fmt", "saldo_producto_fmt"], ["Producto", "Cant.", "Subtotal", "Abonado aplicado", "Saldo producto"], 50)
    product_options = {"Abono general al comprobante": None}
    if not detalles.empty:
        for _, d in detalles.iterrows():
            prod = str(d.get("producto_nombre") or "Producto")
            product_options[f"{prod} · Subtotal {money(d.get('subtotal'))}"] = int(d.get("id_detalle"))

    with st.form("form_pago_credito_producto_v258"):
        aplicar_label = st.selectbox("Aplicar pago a", list(product_options.keys()))
        col1, col2, col3 = st.columns([.8,.8,1.2])
        with col1:
            monto = st.number_input("Monto recibido", min_value=0.0, max_value=float(saldo_actual), value=float(saldo_actual), step=1.0)
        with col2:
            metodo = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto"])
        with col3:
            obs = st.text_input("Observación", placeholder="Ej: abono parcial por producto")
        registrar = st.form_submit_button("Registrar pago y actualizar deuda", type="primary", use_container_width=True)

    if registrar:
        if monto <= 0:
            st.error("El monto debe ser mayor a cero.")
        else:
            id_detalle = product_options[aplicar_label]
            prod_nombre = ""
            if id_detalle is not None and not detalles.empty:
                rowp = detalles[detalles["id_detalle"].eq(id_detalle)]
                if not rowp.empty:
                    prod_nombre = str(rowp.iloc[0].get("producto_nombre") or "")
            nuevo_pagado = _safe_float(venta_sel["monto_pagado"]) + monto
            nuevo_saldo = max(_safe_float(venta_sel["total_venta"]) - nuevo_pagado, 0)
            nuevo_estado = "Pagada" if nuevo_saldo <= 0 else "Parcial"
            with ENGINE.begin() as conn:
                conn.execute(text("UPDATE ventas SET monto_pagado=:pagado, saldo_pendiente=:saldo, estado_pago=:estado WHERE id_venta=:id"), {"pagado": nuevo_pagado, "saldo": nuevo_saldo, "estado": nuevo_estado, "id": idv})
                conn.execute(text("""
                    INSERT INTO pagos_credito (id_venta,id_cliente,metodo_pago,monto,referencia,id_usuario,observacion,id_detalle,producto_nombre)
                    VALUES (:idv,:cli,:metodo,:monto,:ref,:uid,:obs,:idd,:prod)
                """), {"idv": idv, "cli": int(venta_sel["id_cliente"]) if pd.notna(venta_sel.get("id_cliente")) else None, "metodo": metodo, "monto": monto, "ref": venta_sel["comprobante"], "uid": current_user()["id_usuario"], "obs": obs, "idd": id_detalle, "prod": prod_nombre})
                conn.execute(text("""
                    INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion)
                    VALUES ('Ingreso',:concepto,:metodo,:monto,:ref,:uid,:obs)
                """), {"concepto": f"Pago crédito {venta_sel['comprobante']}", "metodo": metodo, "monto": monto, "ref": venta_sel["comprobante"], "uid": current_user()["id_usuario"], "obs": obs})
            clear_report_cache()
            st.success(f"Pago registrado. Nuevo saldo: {money(nuevo_saldo)}")
            st.rerun()

    st.subheader("Pendientes por cobrar")
    df = pendientes.copy()
    df["fecha_fmt"] = df["fecha"].apply(fmt_dt)
    df["total_fmt"] = df["total_venta"].apply(money)
    df["pagado_fmt"] = df["monto_pagado"].apply(money)
    df["saldo_fmt"] = df["saldo_pendiente"].apply(money)
    html_table(df, ["comprobante","fecha_fmt","fecha_vencimiento","cliente","telefono","total_fmt","pagado_fmt","saldo_fmt","estado_pago"], ["Comprobante","Fecha","Vence","Cliente","Teléfono","Total","Pagado","Saldo","Estado"], 300)

    with st.expander("Historial de pagos registrados", expanded=False):
        pagos = query_df("""
            SELECT pc.fecha, pc.referencia, COALESCE(c.nombre_cliente,'Cliente') AS cliente, pc.metodo_pago, pc.monto, pc.producto_nombre, pc.observacion
            FROM pagos_credito pc
            LEFT JOIN clientes c ON c.id_cliente=pc.id_cliente
            ORDER BY pc.fecha DESC
        """)
        if pagos.empty:
            st.info("Todavía no hay pagos de crédito registrados.")
        else:
            pagos["fecha_fmt"] = pagos["fecha"].apply(fmt_dt)
            pagos["monto_fmt"] = pagos["monto"].apply(money)
            html_table(pagos, ["fecha_fmt","referencia","cliente","producto_nombre","metodo_pago","monto_fmt","observacion"], ["Fecha","Comprobante","Cliente","Producto aplicado","Método","Monto","Observación"], 300)


def inject_css_v25_8():
    st.markdown("""
    <style>
      .report-card, .report-card * { color:#111827 !important; }
      .bar-line span { color:#111827 !important; }
      .bar-fill { background:#0f172a !important; }
      .empty-chart { background:#ffffff !important; color:#64748b !important; }
      .stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] button, button[data-testid="baseButton-primary"] {
        background:#0f172a !important; color:#ffffff !important; border-color:#0f172a !important;
      }
      .stButton > button[kind="primary"] *, [data-testid="stFormSubmitButton"] button *, button[data-testid="baseButton-primary"] * { color:#ffffff !important; opacity:1 !important; }
      .stButton > button[kind="primary"] p, [data-testid="stFormSubmitButton"] button p { color:#ffffff !important; }
      .chip-red { background:#fff7ed !important; color:#9a3412 !important; border-color:#fed7aa !important; }
      .credit-client-card { background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:16px; box-shadow:0 8px 22px rgba(15,23,42,.05); }
    </style>
    """, unsafe_allow_html=True)




# ============================================================
# V25.9 - CAJA POR MÉTODO + REINICIO SEGURO + MENÚ AGRUPADO
# ============================================================
def inject_css_v25_9():
    st.markdown("""
    <style>
      :root { --clomar-dark:#0f172a; --clomar-warm:#E4A39A; --clomar-warm2:#F4C2B8; }
      .nav-section { font-size:11px; font-weight:950; color:#94a3b8; letter-spacing:.08em; text-transform:uppercase; margin:16px 0 7px 0; }
      section[data-testid="stSidebar"] .stButton button { justify-content:flex-start !important; border-radius:14px !important; padding:9px 12px !important; font-weight:850 !important; }
      section[data-testid="stSidebar"] .stButton button[kind="primary"] { background:#0f172a !important; color:#fff !important; border-color:#0f172a !important; }
      section[data-testid="stSidebar"] .stButton button[kind="primary"] * { color:#fff !important; }
      .stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] button, button[data-testid="baseButton-primary"] {
        background:#0f172a !important; color:#ffffff !important; border-color:#0f172a !important;
      }
      .stButton > button[kind="primary"] *, [data-testid="stFormSubmitButton"] button * { color:#ffffff !important; opacity:1 !important; }
      .method-grid { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; margin:14px 0; }
      .method-card { background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:14px; box-shadow:0 8px 22px rgba(15,23,42,.05); }
      .method-card .m-label { color:#64748b; font-size:12px; font-weight:950; text-transform:uppercase; letter-spacing:.05em; }
      .method-card .m-value { color:#0f172a; font-size:22px; font-weight:950; margin-top:6px; }
      .report-card, .report-card * { color:#111827 !important; }
      .bar-track { background:#e5e7eb !important; }
      .bar-fill { background:#0f172a !important; }
      .bar-line span { color:#111827 !important; }
      .clomar-table td, .clomar-table th { color:#111827 !important; }
      .chip-red { background:#fff7ed !important; color:#9a3412 !important; border-color:#fed7aa !important; }
      .product-card { min-height:390px !important; padding:16px !important; }
      .product-img-wrap { height:145px !important; }
      .product-desc { display:none !important; }
      @media (min-width: 1500px) { .products-grid { grid-template-columns:repeat(5,minmax(0,1fr)) !important; gap:14px !important; } }
      @media (max-width: 1200px) { .method-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
      @media (max-width: 700px) { .method-grid { grid-template-columns:1fr; } }
    </style>
    """, unsafe_allow_html=True)


def sidebar_nav():
    u = current_user()
    cfg = cached_settings()
    logo = cfg.get("icon_url") or cfg.get("logo_url")
    store = cfg.get("store_name") or APP_NAME_DEFAULT
    if logo:
        st.sidebar.markdown(f"<div class='sidebar-logo'><img src='{esc(logo)}'><div class='sidebar-title'>{esc(store)}</div></div>", unsafe_allow_html=True)
    else:
        st.sidebar.markdown(f"### 🛍️ {store}")
    st.sidebar.caption(f"{u['nombre']} · {u['rol']}")

    if "selected_nav" not in st.session_state:
        st.session_state.selected_nav = "📊 Panel dueño" if is_admin() else "🧾 Ventas"

    def nav_button(label: str):
        active = st.session_state.selected_nav == label
        if st.sidebar.button(label, key="nav_" + hashlib.md5(label.encode()).hexdigest(), use_container_width=True, type="primary" if active else "secondary"):
            st.session_state.selected_nav = label
            st.rerun()

    if is_admin():
        st.sidebar.markdown("<div class='nav-section'>Gestionar negocio</div>", unsafe_allow_html=True)
        for label in ["📊 Panel dueño", "🧾 Ventas", "💳 Créditos", "💰 Caja", "📈 Reportes"]:
            nav_button(label)
        st.sidebar.markdown("<div class='nav-section'>Productos e inventario</div>", unsafe_allow_html=True)
        for label in ["📦 Productos", "📘 Catálogo clientes", "📊 Inventario", "📥 Ingreso mercadería"]:
            nav_button(label)
        st.sidebar.markdown("<div class='nav-section'>Contactos</div>", unsafe_allow_html=True)
        nav_button("👥 Clientes")
        st.sidebar.markdown("<div class='nav-section'>Control de app</div>", unsafe_allow_html=True)
        for label in ["🔐 Usuarios", "⚙️ Configuración", "💾 Backup", "📲 Instalar app", "☁️ Estado nube"]:
            nav_button(label)
    else:
        st.sidebar.markdown("<div class='nav-section'>Venta y catálogo</div>", unsafe_allow_html=True)
        for label in ["🧾 Ventas", "👥 Clientes", "📦 Productos", "📘 Catálogo clientes", "📲 Instalar app"]:
            nav_button(label)

    st.sidebar.divider()
    if st.sidebar.button("Cerrar sesión", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    return st.session_state.selected_nav


def _money_or_zero(v):
    try:
        return money(float(v or 0))
    except Exception:
        return money(0)


def _method_cards(df: pd.DataFrame, amount_col: str = "monto", label_col: str = "metodo_pago"):
    base_methods = ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto", "Crédito"]
    if df is None or df.empty:
        items = [(m, 0) for m in base_methods]
    else:
        tmp = df.copy()
        tmp[label_col] = tmp[label_col].fillna("Sin método").astype(str).replace("", "Sin método")
        tmp[amount_col] = pd.to_numeric(tmp[amount_col], errors="coerce").fillna(0)
        grouped = tmp.groupby(label_col)[amount_col].sum().to_dict()
        ordered = base_methods + [k for k in grouped.keys() if k not in base_methods]
        items = [(m, grouped.get(m, 0)) for m in ordered if grouped.get(m, 0) != 0 or m in base_methods]
    cards = []
    for m, val in items[:7]:
        cards.append(f"<div class='method-card'><div class='m-label'>{esc(m)}</div><div class='m-value'>{money(val)}</div></div>")
    st.markdown("<div class='method-grid'>" + "".join(cards) + "</div>", unsafe_allow_html=True)


def _bar_report_html(df: pd.DataFrame, label_col: str, value_col: str, title: str, limit: int = 10, money_values: bool = True):
    if df is None or df.empty or value_col not in df.columns:
        st.markdown(f"<div class='report-card'><h3>{esc(title)}</h3><div class='empty-chart'>Sin datos para mostrar.</div></div>", unsafe_allow_html=True)
        return
    data = df.copy()
    data[value_col] = pd.to_numeric(data[value_col], errors="coerce").fillna(0)
    data = data.sort_values(value_col, ascending=False).head(limit)
    maxv = float(data[value_col].max() or 0)
    rows = []
    for _, r in data.iterrows():
        val = float(r.get(value_col) or 0)
        pct = 0 if maxv <= 0 else max(4, min(100, (val / maxv) * 100))
        label_raw = r.get(label_col)
        label = esc(label_raw if pd.notna(label_raw) and str(label_raw).strip() else "Sin dato")
        value = money(val) if money_values else num(val)
        rows.append("<div class='bar-row'>" +
                    f"<div class='bar-line'><span>{label}</span><span>{value}</span></div>" +
                    f"<div class='bar-track'><div class='bar-fill' style='width:{pct:.1f}%'></div></div>" +
                    "</div>")
    st.markdown(f"<div class='report-card'><h3>{esc(title)}</h3>{''.join(rows)}</div>", unsafe_allow_html=True)


def page_panel_dueno():
    hero("Panel del dueño", "Resumen ejecutivo: ventas, caja, créditos, vendedores y stock crítico.", "📊")
    if not is_admin():
        st.warning("Solo administrador puede ver el panel del dueño.")
        return
    d1, d2 = st.columns(2)
    with d1:
        desde = st.date_input("Desde", peru_today(), key="panel_desde_v259")
    with d2:
        hasta = st.date_input("Hasta", peru_today(), key="panel_hasta_v259")
    ventas = ventas_periodo(desde, hasta)
    detalle = detalle_productos_vendidos(desde, hasta)
    if ventas.empty:
        total = cobrado = credito = ticket = utilidad = 0
    else:
        ventas["total_venta"] = pd.to_numeric(ventas["total_venta"], errors="coerce").fillna(0)
        ventas["monto_pagado"] = pd.to_numeric(ventas["monto_pagado"], errors="coerce").fillna(0)
        ventas["saldo_pendiente"] = pd.to_numeric(ventas["saldo_pendiente"], errors="coerce").fillna(0)
        total = ventas["total_venta"].sum(); cobrado = ventas["monto_pagado"].sum(); credito = ventas["saldo_pendiente"].sum(); ticket = total / max(len(ventas), 1)
        utilidad = float(pd.to_numeric(detalle.get("utilidad", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not detalle.empty else 0
    a,b,c,d = st.columns(4)
    with a: kpi("Ventas", money(total), f"{len(ventas)} comprobantes")
    with b: kpi("Cobrado", money(cobrado), "Ingresos efectivos")
    with c: kpi("Crédito", money(credito), "Por cobrar")
    with d: kpi("Utilidad estimada", money(utilidad), "Según costo registrado")

    if not ventas.empty:
        vend = ventas.groupby("vendedor_nombre", as_index=False).agg(ventas=("id_venta","count"), total=("total_venta","sum"), cobrado=("monto_pagado","sum"), credito=("saldo_pendiente","sum"))
        if not detalle.empty:
            util = detalle.groupby("vendedor", as_index=False)["utilidad"].sum().rename(columns={"vendedor":"vendedor_nombre"})
            vend = vend.merge(util, on="vendedor_nombre", how="left")
        else:
            vend["utilidad"] = 0
        st.subheader("Ventas por vendedor")
        _bar_report_html(vend, "vendedor_nombre", "total", "Control por vendedor", 8, True)
        show = vend.copy()
        for col in ["total","cobrado","credito","utilidad"]:
            show[col+"_fmt"] = show[col].apply(money)
        html_table(show, ["vendedor_nombre","ventas","total_fmt","cobrado_fmt","credito_fmt","utilidad_fmt"], ["Vendedor","Ventas","Total","Cobrado","Crédito","Utilidad"], 20)
    else:
        st.info("Aún no hay ventas en el rango seleccionado.")

    c1,c2 = st.columns([1.3, .9])
    with c1:
        st.subheader("Ventas recientes")
        recent = ventas.head(12).copy()
        if not recent.empty:
            recent["fecha_fmt"] = recent["fecha"].apply(fmt_dt)
            recent["total_fmt"] = recent["total_venta"].apply(money)
            html_table(recent, ["comprobante","fecha_fmt","cliente","vendedor_nombre","metodo_pago","total_fmt"], ["Comprobante","Fecha Perú","Cliente","Vendedor","Pago","Total"], 12)
        else:
            st.info("Sin ventas recientes.")
    with c2:
        st.subheader("Stock crítico")
        prod = productos_con_stock()
        if not prod.empty:
            crit = prod[pd.to_numeric(prod["stock_actual"], errors="coerce").fillna(0) <= pd.to_numeric(prod["stock_minimo"], errors="coerce").fillna(0)].head(12)
            if crit.empty:
                st.success("Sin stock crítico.")
            else:
                for _, r in crit.iterrows():
                    st.markdown(f"<span class='chip chip-red'>⚠ {esc(r['nombre_producto'])} · Stock {num(r['stock_actual'])}</span>", unsafe_allow_html=True)
        else:
            st.info("Sin productos registrados.")


def page_reportes():
    hero("Reportes", "Ventas por fecha, vendedor, producto y método de pago.", "📈")
    if not is_admin():
        st.warning("Solo administrador puede ver reportes.")
        return
    d1, d2 = st.columns(2)
    with d1:
        desde = st.date_input("Desde", peru_today() - timedelta(days=7), key="rep_desde_v259")
    with d2:
        hasta = st.date_input("Hasta", peru_today(), key="rep_hasta_v259")
    ventas = ventas_periodo(desde, hasta)
    detalle = detalle_productos_vendidos(desde, hasta)
    if ventas.empty:
        st.info("No hay ventas para el rango seleccionado.")
        return
    ventas["total_venta"] = pd.to_numeric(ventas["total_venta"], errors="coerce").fillna(0)
    ventas["monto_pagado"] = pd.to_numeric(ventas["monto_pagado"], errors="coerce").fillna(0)
    ventas["saldo_pendiente"] = pd.to_numeric(ventas["saldo_pendiente"], errors="coerce").fillna(0)
    a,b,c,d = st.columns(4)
    with a: kpi("Total vendido", money(ventas["total_venta"].sum()), f"{len(ventas)} comprobantes")
    with b: kpi("Cobrado", money(ventas["monto_pagado"].sum()), "Ingresos recibidos")
    with c: kpi("Crédito", money(ventas["saldo_pendiente"].sum()), "Saldo por cobrar")
    with d: kpi("Ticket promedio", money(ventas["total_venta"].sum()/max(len(ventas),1)), "Promedio")

    vend = ventas.groupby("vendedor_nombre", as_index=False).agg(total=("total_venta","sum"), cobrado=("monto_pagado","sum"), credito=("saldo_pendiente","sum"), ventas=("id_venta","count"))
    metodo = ventas.groupby("metodo_pago", as_index=False).agg(total=("total_venta","sum"), cobrado=("monto_pagado","sum"), credito=("saldo_pendiente","sum"), ventas=("id_venta","count"))
    vday = ventas.copy()
    vday["dia"] = pd.to_datetime(vday["fecha"], errors="coerce").dt.strftime("%d/%m")
    dia = vday.groupby("dia", as_index=False)["total_venta"].sum().rename(columns={"total_venta":"total"})

    st.markdown("<div class='report-grid'>", unsafe_allow_html=True)
    _bar_report_html(vend, "vendedor_nombre", "total", "Ventas por vendedor", 10, True)
    _bar_report_html(metodo, "metodo_pago", "total", "Métodos de pago", 10, True)
    _bar_report_html(dia, "dia", "total", "Ventas por día", 14, True)
    if not detalle.empty:
        top = detalle.copy()
        top["total_vendido"] = pd.to_numeric(top["total_vendido"], errors="coerce").fillna(0)
        top = top.groupby("producto", as_index=False)["total_vendido"].sum()
        _bar_report_html(top, "producto", "total_vendido", "Productos vendidos", 10, True)
    else:
        _bar_report_html(pd.DataFrame(), "producto", "total_vendido", "Productos vendidos", 10, True)
    st.markdown("</div>", unsafe_allow_html=True)

    st.subheader("Detalle de comprobantes")
    detv = ventas.copy()
    detv["fecha_fmt"] = detv["fecha"].apply(fmt_dt)
    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        detv[col+"_fmt"] = detv[col].apply(money)
    html_table(detv, ["comprobante","fecha_fmt","cliente","vendedor_nombre","metodo_pago","total_venta_fmt","monto_pagado_fmt","saldo_pendiente_fmt","estado_pago"], ["Comprobante","Fecha","Cliente","Vendedor","Método","Total","Pagado","Saldo","Estado"], 200)


def page_caja():
    hero("Caja", "Cierre diario, ingresos, egresos, pagos por método y diferencias.", "💰")
    if not is_admin():
        st.warning("Solo administrador puede ver caja.")
        return
    ensure_v25_8_schema()
    c1,c2,c3 = st.columns([.9,.9,1.2])
    with c1:
        f = st.date_input("Fecha de caja", peru_today(), key="caja_fecha_v259")
    with c2:
        tipo_filtro = st.selectbox("Tipo", ["Todos", "Ingreso", "Egreso"], key="caja_tipo_v259")
    with c3:
        metodo_filtro = st.selectbox("Método de pago", ["Todos", "Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto", "Crédito"], key="caja_metodo_v259")
    caja = query_df("SELECT * FROM caja WHERE DATE(fecha)=:f AND COALESCE(anulada,0)=0 ORDER BY fecha DESC", {"f": str(f)})
    ventas = ventas_periodo(f, f)
    if not caja.empty:
        caja["monto"] = pd.to_numeric(caja["monto"], errors="coerce").fillna(0)
    if not ventas.empty:
        for col in ["total_venta","monto_pagado","saldo_pendiente"]:
            ventas[col] = pd.to_numeric(ventas[col], errors="coerce").fillna(0)

    ingresos = float(caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum()) if not caja.empty else 0
    egresos = float(caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum()) if not caja.empty else 0
    venta_total = float(ventas["total_venta"].sum()) if not ventas.empty else 0
    cobrado = float(ventas["monto_pagado"].sum()) if not ventas.empty else 0
    credito = float(ventas["saldo_pendiente"].sum()) if not ventas.empty else 0
    a,b,c,d = st.columns(4)
    with a: kpi("Ventas del día", money(venta_total), f"{len(ventas)} comprobantes")
    with b: kpi("Cobrado", money(cobrado), "Contado + abonos")
    with c: kpi("Crédito generado", money(credito), "Pendiente")
    with d: kpi("Caja neta", money(ingresos-egresos), "Ingresos - egresos")

    st.subheader("Pagos por método")
    if ventas.empty:
        _method_cards(pd.DataFrame(columns=["metodo_pago","monto_pagado"]), "monto_pagado", "metodo_pago")
    else:
        _method_cards(ventas.rename(columns={"monto_pagado":"monto"}), "monto", "metodo_pago")

    st.subheader("Movimiento de caja por método")
    if caja.empty:
        st.info("No hay movimientos de caja en la fecha.")
    else:
        mov = caja.copy()
        if tipo_filtro != "Todos":
            mov = mov[mov["tipo"].astype(str).eq(tipo_filtro)]
        if metodo_filtro != "Todos":
            mov = mov[mov["metodo_pago"].astype(str).eq(metodo_filtro)]
        if mov.empty:
            st.info("No hay movimientos con esos filtros.")
        else:
            piv = mov.pivot_table(index="metodo_pago", columns="tipo", values="monto", aggfunc="sum", fill_value=0).reset_index()
            for col in ["Ingreso", "Egreso"]:
                if col not in piv.columns: piv[col] = 0
            piv["Neto"] = piv["Ingreso"] - piv["Egreso"]
            show = piv.copy()
            for col in ["Ingreso", "Egreso", "Neto"]:
                show[col+"_fmt"] = show[col].apply(money)
            html_table(show, ["metodo_pago","Ingreso_fmt","Egreso_fmt","Neto_fmt"], ["Método","Ingresos","Egresos","Neto"], 20)

    efectivo_sistema = 0
    if not caja.empty:
        tmp = caja.copy()
        efectivo_ing = tmp[(tmp["metodo_pago"].astype(str).str.lower().eq("efectivo")) & (tmp["tipo"].astype(str).str.lower().eq("ingreso"))]["monto"].sum()
        efectivo_egr = tmp[(tmp["metodo_pago"].astype(str).str.lower().eq("efectivo")) & (tmp["tipo"].astype(str).str.lower().eq("egreso"))]["monto"].sum()
        efectivo_sistema = float(efectivo_ing - efectivo_egr)
    st.subheader("Cierre diario")
    with st.form("form_cierre_caja_v259"):
        cc1, cc2 = st.columns(2)
        with cc1:
            efectivo_contado = st.number_input("Efectivo contado físico", min_value=0.0, value=float(max(efectivo_sistema, 0)), step=1.0)
        with cc2:
            obs = st.text_input("Observación", placeholder="Ej: cierre correcto, faltante, sobrante")
        diferencia = efectivo_contado - efectivo_sistema
        st.markdown(f"<div class='card'><b>Efectivo sistema:</b> {money(efectivo_sistema)} &nbsp; <b>Diferencia:</b> <span class='chip {'chip-ok' if abs(diferencia)<0.01 else 'chip-red'}'>{money(diferencia)}</span></div>", unsafe_allow_html=True)
        guardar = st.form_submit_button("Guardar cierre diario", type="primary", use_container_width=True)
    if guardar:
        exec_sql("""
            INSERT INTO cierres_caja (fecha_cierre,total_ingresos,total_egresos,saldo_sistema,efectivo_sistema,efectivo_contado,diferencia,id_usuario,observacion)
            VALUES (:f,:ing,:egr,:neto,:efs,:efc,:dif,:uid,:obs)
        """, {"f": str(f), "ing": ingresos, "egr": egresos, "neto": ingresos-egresos, "efs": efectivo_sistema, "efc": efectivo_contado, "dif": diferencia, "uid": current_user()["id_usuario"], "obs": obs})
        st.success("Cierre diario guardado.")

    st.subheader("Detalle de movimientos")
    if caja.empty:
        st.info("Sin movimientos.")
    else:
        det = caja.copy()
        if tipo_filtro != "Todos": det = det[det["tipo"].astype(str).eq(tipo_filtro)]
        if metodo_filtro != "Todos": det = det[det["metodo_pago"].astype(str).eq(metodo_filtro)]
        det["fecha_fmt"] = det["fecha"].apply(fmt_dt)
        det["monto_fmt"] = det["monto"].apply(money)
        html_table(det, ["fecha_fmt","tipo","concepto","metodo_pago","monto_fmt","referencia","observacion"], ["Fecha","Tipo","Concepto","Método","Monto","Referencia","Obs."], 200)


def _delete_if_exists(conn, table: str):
    try:
        conn.execute(text(f"DELETE FROM {table}"))
    except Exception:
        pass


def _reset_business_data(delete_categories=False):
    """Reinicia operación, conservando usuarios y configuración. Por defecto conserva categorías."""
    tables = [
        "pagos_credito", "cierres_caja", "detalle_ventas", "ventas", "caja",
        "movimientos_stock", "compras", "productos", "proveedores", "clientes"
    ]
    if delete_categories:
        tables.append("categorias")
    with ENGINE.begin() as conn:
        for t in tables:
            _delete_if_exists(conn, t)
        # Reinicio de identificadores cuando sea posible.
        identity_cols = {
            "pagos_credito":"id_pago", "cierres_caja":"id_cierre", "detalle_ventas":"id_detalle",
            "ventas":"id_venta", "caja":"id_caja", "movimientos_stock":"id_movimiento",
            "compras":"id_compra", "productos":"id_producto", "proveedores":"id_proveedor", "clientes":"id_cliente", "categorias":"id_categoria"
        }
        if IS_POSTGRES:
            for t, col in identity_cols.items():
                if t in tables:
                    try:
                        conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN {col} RESTART WITH 1"))
                    except Exception:
                        pass
        elif IS_LOCAL_SQLITE:
            try:
                for t in tables:
                    conn.execute(text("DELETE FROM sqlite_sequence WHERE name=:t"), {"t": t})
            except Exception:
                pass
    clear_product_cache()
    clear_report_cache()
    try:
        cached_settings.clear()
    except Exception:
        pass


def page_backup():
    hero("Backup / reinicio seguro", "Respalda datos y reinicia la operación cuando quieras empezar desde cero.", "💾")
    if not is_admin():
        st.warning("Solo administrador puede descargar backups o reiniciar datos.")
        return
    tablas=["usuarios","categorias","clientes","proveedores","productos","movimientos_stock","ventas","detalle_ventas","compras","caja","pagos_credito","cierres_caja","ajustes"]
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as z:
        for t in tablas:
            try:
                df=query_df(f"SELECT * FROM {t}")
                z.writestr(f"{t}.csv", df.to_csv(index=False).encode("utf-8-sig"))
            except Exception:
                pass
    buffer.seek(0)
    st.download_button("Descargar backup ZIP antes de reiniciar", data=buffer, file_name=f"clomar_store_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip", mime="application/zip", type="primary", use_container_width=True)

    st.markdown("""
    <div class='card'>
      <h3>Reiniciar negocio desde cero</h3>
      <p>Esta opción elimina ventas, boletas/comprobantes, créditos, caja, productos, stock, clientes y proveedores. Conserva usuarios, configuración y categorías, salvo que marques borrar categorías.</p>
      <p><b>Úsalo solo después de descargar backup.</b></p>
    </div>
    """, unsafe_allow_html=True)
    with st.form("reset_negocio_v259"):
        borrar_categorias = st.checkbox("También borrar categorías", value=False)
        confirm = st.text_input("Para confirmar escribe: REINICIAR_CLOMAR")
        reset = st.form_submit_button("Reiniciar datos operativos", type="primary", use_container_width=True)
    if reset:
        if confirm.strip() != "REINICIAR_CLOMAR":
            st.error("Confirmación incorrecta. No se eliminó nada.")
        else:
            _reset_business_data(delete_categories=borrar_categorias)
            st.success("Datos operativos reiniciados. Puedes cargar productos desde cero.")
            st.rerun()



# ============================================================
# V25.10 - REPORTES EJECUTIVOS + ANULACIÓN + POS CON VISTA PREVIA
# ============================================================
def ensure_v25_10_schema():
    """Migraciones suaves para anulación y auditoría de ventas."""
    try:
        ensure_v25_8_schema()
    except Exception:
        pass
    for col in [
        "motivo_anulacion TEXT DEFAULT ''",
        "anulado_por INTEGER",
        "anulado_en TIMESTAMP",
    ]:
        try:
            safe_alter("ventas", col)
        except Exception:
            pass
    try:
        safe_alter("caja", "id_venta INTEGER")
    except Exception:
        pass
    try:
        exec_sql("CREATE INDEX IF NOT EXISTS idx_ventas_anulada_fecha ON ventas(anulada, fecha)")
        exec_sql("CREATE INDEX IF NOT EXISTS idx_caja_referencia ON caja(referencia)")
    except Exception:
        pass


def inject_css_v25_10():
    st.markdown("""
    <style>
      .report-grid-v2510 {display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin:14px 0;}
      .report-grid-v2510 .report-card {min-height:210px;}
      .preview-product {display:grid; grid-template-columns:120px 1fr; gap:14px; align-items:center; background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:12px; margin:12px 0; box-shadow:0 8px 24px rgba(15,23,42,.05);}
      .preview-img {width:120px; height:110px; object-fit:contain; border-radius:14px; background:#f8fafc; border:1px solid #e5e7eb;}
      .preview-title {font-weight:950; color:#0f172a; font-size:16px; line-height:1.25;}
      .preview-meta {font-weight:700; color:#64748b; font-size:12px; margin-top:4px;}
      .preview-price {font-weight:950; color:#0f172a; font-size:22px; margin-top:7px;}
      .audit-box {background:#fff7ed; border:1px solid #fed7aa; color:#7c2d12; border-radius:16px; padding:14px; font-weight:800;}
      .void-box {background:#fff; border:1px solid #fecaca; border-radius:18px; padding:16px; box-shadow:0 8px 20px rgba(15,23,42,.05);}
      .status-anulada {background:#fee2e2 !important; color:#991b1b !important; border-color:#fecaca !important;}
      .stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] button, button[data-testid="baseButton-primary"], .stDownloadButton button[kind="primary"] {
          background:#0f172a !important; color:#ffffff !important; border-color:#0f172a !important;
      }
      .stButton > button[kind="primary"] *, [data-testid="stFormSubmitButton"] button *, button[data-testid="baseButton-primary"] *, .stDownloadButton button[kind="primary"] * {color:#ffffff !important; opacity:1 !important;}
      @media (max-width: 1200px){.report-grid-v2510{grid-template-columns:1fr}.preview-product{grid-template-columns:90px 1fr}.preview-img{width:90px;height:90px}}
      @media (max-width: 640px){.preview-product{grid-template-columns:1fr}.preview-img{width:100%;height:150px}.report-grid-v2510{grid-template-columns:1fr}}
    </style>
    """, unsafe_allow_html=True)


def _ventas_rango_todas(desde, hasta) -> pd.DataFrame:
    return query_df("""
        SELECT v.*, COALESCE(c.nombre_cliente,'Cliente general') AS cliente,
               COALESCE(c.telefono,'') AS cliente_telefono,
               COALESCE(c.documento,'') AS cliente_documento
        FROM ventas v
        LEFT JOIN clientes c ON c.id_cliente=v.id_cliente
        WHERE DATE(v.fecha) BETWEEN :d AND :h
        ORDER BY v.fecha DESC
    """, {"d": str(desde), "h": str(hasta)})


def _filter_ventas_df(df: pd.DataFrame, vendedor="Todos", metodo="Todos", estado="Vigentes", cliente="Todos", texto="") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
    if vendedor != "Todos" and "vendedor_nombre" in out.columns:
        out = out[out["vendedor_nombre"].astype(str).eq(str(vendedor))]
    if metodo != "Todos" and "metodo_pago" in out.columns:
        out = out[out["metodo_pago"].astype(str).str.contains(str(metodo), case=False, na=False)]
    if cliente != "Todos" and "cliente" in out.columns:
        out = out[out["cliente"].astype(str).eq(str(cliente))]
    if estado == "Vigentes":
        out = out[pd.to_numeric(out.get("anulada", 0), errors="coerce").fillna(0).eq(0)]
    elif estado == "Anuladas":
        out = out[pd.to_numeric(out.get("anulada", 0), errors="coerce").fillna(0).gt(0)]
    elif estado in ["Pagada", "Parcial", "Pendiente"] and "estado_pago" in out.columns:
        out = out[out["estado_pago"].astype(str).eq(estado)]
    if texto.strip():
        t = texto.strip().lower()
        mask = False
        for col in ["comprobante", "cliente", "vendedor_nombre", "metodo_pago", "estado_pago"]:
            if col in out.columns:
                mask = mask | out[col].astype(str).str.lower().str.contains(t, na=False)
        out = out[mask]
    return out


def _ventas_filters(prefix: str, df: pd.DataFrame):
    vendedores = ["Todos"] + sorted([x for x in df.get("vendedor_nombre", pd.Series(dtype=str)).dropna().astype(str).unique()]) if df is not None and not df.empty else ["Todos"]
    metodos_base = ["Todos", "Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto", "Crédito"]
    clientes = ["Todos"] + sorted([x for x in df.get("cliente", pd.Series(dtype=str)).dropna().astype(str).unique()]) if df is not None and not df.empty else ["Todos"]
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1.2])
    with c1:
        vendedor = st.selectbox("Vendedor", vendedores, key=f"{prefix}_vend")
    with c2:
        metodo = st.selectbox("Método", metodos_base, key=f"{prefix}_met")
    with c3:
        estado = st.selectbox("Estado", ["Vigentes", "Pagada", "Parcial", "Pendiente", "Anuladas", "Todos"], key=f"{prefix}_estado")
    with c4:
        cliente = st.selectbox("Cliente", clientes, key=f"{prefix}_cliente")
    texto = st.text_input("Buscar por comprobante, cliente, vendedor o método", key=f"{prefix}_buscar", placeholder="Ej: V2026, Cliente general, Yape...")
    return vendedor, metodo, estado, cliente, texto


def _bar_report_small(df: pd.DataFrame, label_col: str, value_col: str, title: str, limit: int = 6):
    _bar_report_html(df, label_col, value_col, title, limit=limit, money_values=True)


def anular_venta(id_venta: int, motivo: str):
    """Anula una venta sin borrar historial: revierte stock y caja mediante movimientos contrarios."""
    motivo = str(motivo or "").strip()
    if not motivo:
        raise ValueError("Debes indicar el motivo de anulación.")
    venta = query_df("SELECT * FROM ventas WHERE id_venta=:id", {"id": int(id_venta)})
    if venta.empty:
        raise ValueError("No se encontró la venta.")
    v = venta.iloc[0]
    if int(float(v.get("anulada") or 0)) == 1:
        raise ValueError("La venta ya está anulada.")
    det = query_df("SELECT * FROM detalle_ventas WHERE id_venta=:id", {"id": int(id_venta)})
    comp = str(v.get("comprobante") or "")
    uid = current_user()["id_usuario"]
    pagado = float(v.get("monto_pagado") or 0)
    metodo = str(v.get("metodo_pago") or "Efectivo")
    # Si era 'Crédito + Yape', para reverso de caja usamos el método real si está presente.
    metodo_caja = metodo.replace("Crédito + ", "") if metodo.startswith("Crédito + ") else metodo
    if metodo_caja == "Crédito":
        metodo_caja = "Efectivo"
    with ENGINE.begin() as conn:
        for _, r in det.iterrows():
            conn.execute(text("""
                INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion)
                VALUES (:idp,'ENTRADA',:cant,:costo,:ref,:uid,:obs)
            """), {
                "idp": int(r.get("id_producto")), "cant": float(r.get("cantidad") or 0),
                "costo": float(r.get("costo_unitario") or 0), "ref": f"ANULA {comp}",
                "uid": uid, "obs": f"Anulación de venta: {motivo}"
            })
        if pagado > 0:
            conn.execute(text("""
                INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion,id_venta)
                VALUES ('Egreso',:concepto,:metodo,:monto,:ref,:uid,:obs,:idv)
            """), {
                "concepto": f"Anulación venta {comp}", "metodo": metodo_caja, "monto": pagado,
                "ref": f"ANULA {comp}", "uid": uid, "obs": motivo, "idv": int(id_venta)
            })
        conn.execute(text("""
            UPDATE ventas
            SET anulada=1, estado_pago='Anulada', motivo_anulacion=:motivo,
                anulado_por=:uid, anulado_en=CURRENT_TIMESTAMP
            WHERE id_venta=:id
        """), {"motivo": motivo, "uid": uid, "id": int(id_venta)})
    clear_product_cache()
    clear_report_cache()


def page_ventas():
    ensure_v25_10_schema()
    hero("Venta rápida", "Selecciona producto, confirma con vista previa, cobra y genera boleta interna.", "🧾")
    if "cart" not in st.session_state:
        st.session_state.cart = []
    if "pos_step" not in st.session_state:
        st.session_state.pos_step = "carrito"

    if st.session_state.get("show_success_sale") and st.session_state.get("last_sale_id"):
        venta_id = int(st.session_state.last_sale_id)
        vdf = query_df("SELECT comprobante,total_venta,monto_pagado,saldo_pendiente,estado_pago FROM ventas WHERE id_venta=:id", {"id": venta_id})
        saldo = float(vdf.iloc[0]["saldo_pendiente"] or 0) if not vdf.empty else 0
        st.markdown("<div class='success-panel'><div class='success-icon'>✅</div><div class='success-title'>¡Venta registrada!</div><div class='success-sub'>Descarga, imprime o continúa vendiendo.</div></div>", unsafe_allow_html=True)
        render_receipt(venta_id)
        pdf = generate_receipt_pdf(venta_id)
        st.markdown("<div class='no-print'>", unsafe_allow_html=True)
        a,b,c = st.columns(3)
        with a:
            if pdf:
                st.download_button("⬇️ Descargar PDF", data=pdf, file_name=f"boleta_{vdf.iloc[0]['comprobante'] if not vdf.empty else venta_id}.pdf", mime="application/pdf", use_container_width=True)
        with b:
            print_button_component("🖨️ Imprimir boleta")
        with c:
            if st.button("Seguir vendiendo", type="primary", use_container_width=True):
                st.session_state.show_success_sale = False
                st.session_state.pos_step = "carrito"
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        if saldo > 0:
            st.warning(f"Esta venta quedó con saldo pendiente: {money(saldo)}. Registra pagos en 💳 Créditos.")
        return

    total_cart = sum(float(i.get("cantidad", 0)) * float(i.get("precio", 0)) for i in st.session_state.cart)
    c1,c2,c3 = st.columns(3)
    with c1: kpi("Carrito", f"{len(st.session_state.cart)} productos", "Edita antes de cobrar")
    with c2: kpi("Total actual", money(total_cart), "No se registra hasta confirmar")
    with c3: kpi("Paso", "Carrito" if st.session_state.pos_step == "carrito" else "Pago", "POS rápido")

    if st.session_state.pos_step == "carrito":
        productos = productos_con_stock()
        if not productos.empty:
            productos["stock_actual"] = pd.to_numeric(productos["stock_actual"], errors="coerce").fillna(0)
            productos = productos[productos["stock_actual"] > 0].copy()
        left, right = st.columns([1.05,.95])
        with left:
            st.markdown("<div class='pos-panel'><h3>Agregar producto</h3><p class='product-meta'>La imagen solo carga para el producto seleccionado, así el POS se mantiene rápido.</p>", unsafe_allow_html=True)
            if productos.empty:
                st.info("No hay productos con stock disponible.")
            else:
                productos["label_pos"] = productos.apply(lambda r: f"{normalize_code(r.get('codigo'))} · {r.get('nombre_producto')} · Stock {num(r.get('stock_actual'))} · {money(r.get('precio_venta'))}", axis=1)
                with st.form("form_add_pos_v2510", clear_on_submit=False):
                    sel = st.selectbox("Producto", productos["label_pos"].tolist(), key="pos_producto_select_v2510")
                    r = productos[productos["label_pos"].eq(sel)].iloc[0]
                    stock = float(r.get("stock_actual") or 0)
                    img = product_image_candidates(r)[0] if product_image_candidates(r) else ""
                    img_html = f"<img class='preview-img' src='{esc(img)}'>" if img else "<div class='preview-img'></div>"
                    st.markdown(f"""
                    <div class='preview-product'>
                      {img_html}
                      <div>
                        <div class='preview-title'>{esc(r.get('nombre_producto'))}</div>
                        <div class='preview-meta'>Código {esc(r.get('codigo'))} · {esc(r.get('categoria'))} · Stock {num(stock)}</div>
                        <div class='preview-price'>{money(r.get('precio_venta'))}</div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)
                    a,b = st.columns(2)
                    with a:
                        cantidad = st.number_input("Cantidad", min_value=1.0, max_value=max(stock,1.0), value=1.0, step=1.0, key="pos_cantidad_v2510")
                    with b:
                        precio_default = float(r.get("precio_venta") or 0)
                        precio = st.number_input("Precio", min_value=0.0, value=precio_default, step=1.0, key="pos_precio_v2510") if is_admin() else precio_default
                        if not is_admin():
                            st.text_input("Precio", value=money(precio), disabled=True, key="pos_precio_view_v2510")
                    st.markdown(f"<span class='chip chip-ok'>Stock {num(stock)}</span><span class='chip chip-dark'>Subtotal {money(float(cantidad)*float(precio))}</span>", unsafe_allow_html=True)
                    add = st.form_submit_button("🛒 Agregar al carrito", type="primary", use_container_width=True)
                if add:
                    found = False
                    for item in st.session_state.cart:
                        if item["id_producto"] == int(r["id_producto"]):
                            item["cantidad"] = min(float(item["cantidad"]) + float(cantidad), stock)
                            item["precio"] = float(precio)
                            found = True
                            break
                    if not found:
                        st.session_state.cart.append({
                            "id_producto": int(r["id_producto"]), "nombre": r["nombre_producto"], "codigo": r.get("codigo", ""),
                            "precio": float(precio), "costo": float(r.get("costo_unitario") or 0), "stock": stock, "cantidad": float(cantidad)
                        })
                    st.toast("Producto agregado")
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with right:
            st.markdown("<div class='pos-panel'><h3>🛒 Carrito</h3>", unsafe_allow_html=True)
            if not st.session_state.cart:
                st.info("Agrega productos para vender.")
            else:
                total = 0.0
                nuevo = []
                for idx, item in enumerate(st.session_state.cart):
                    st.markdown(f"<div class='cart-item-pro'><strong>{esc(item['nombre'])}</strong><div class='meta'>{esc(item.get('codigo',''))} · Stock {num(item.get('stock',0))}</div></div>", unsafe_allow_html=True)
                    c1,c2,c3,c4 = st.columns([.6,.75,.75,.25])
                    with c1:
                        cant = st.number_input("Cant.", min_value=0.0, max_value=float(item["stock"]), value=float(item["cantidad"]), step=1.0, key=f"cart_cant_v2510_{idx}")
                    with c2:
                        precio = st.number_input("Precio", min_value=0.0, value=float(item["precio"]), step=1.0, key=f"cart_precio_v2510_{idx}") if is_admin() else float(item["precio"])
                        if not is_admin(): st.text_input("Precio", value=money(precio), disabled=True, key=f"cart_precio_view_v2510_{idx}")
                    with c3:
                        st.text_input("Subtotal", value=money(cant*precio), disabled=True, key=f"cart_sub_v2510_{idx}")
                    with c4:
                        if st.button("❌", key=f"cart_del_v2510_{idx}"):
                            cant = 0
                    if cant > 0:
                        item["cantidad"] = cant; item["precio"] = precio
                        total += cant * precio
                        nuevo.append(item)
                st.session_state.cart = nuevo
                st.markdown(f"<div class='pos-total-banner'><b>Total</b><b>{money(total)}</b></div>", unsafe_allow_html=True)
                a,b = st.columns(2)
                with a:
                    if st.button("Vaciar", use_container_width=True):
                        st.session_state.cart = []
                        st.rerun()
                with b:
                    if st.button("Continuar al pago", type="primary", use_container_width=True, disabled=total<=0):
                        st.session_state.pos_step = "pago"
                        st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
    else:
        if not st.session_state.cart:
            st.warning("El carrito está vacío.")
            if st.button("Volver al carrito"):
                st.session_state.pos_step = "carrito"
                st.rerun()
            return
        total = sum(float(i.get("cantidad", 0)) * float(i.get("precio", 0)) for i in st.session_state.cart)
        st.markdown(f"<div class='pos-total-banner'><b>Total a cobrar</b><b>{money(total)}</b></div>", unsafe_allow_html=True)
        left,right = st.columns([1.05,.95])
        with left:
            clientes = query_df("SELECT id_cliente, nombre_cliente, telefono FROM clientes WHERE COALESCE(estado,'Activo')='Activo' ORDER BY nombre_cliente")
            opciones_cliente = {"Cliente general": None}
            if not clientes.empty:
                opciones_cliente.update({f"{r['nombre_cliente']}" + (f" · {r['telefono']}" if str(r.get('telefono') or '').strip() else ""): int(r["id_cliente"]) for _, r in clientes.iterrows()})
            with st.form("form_confirmar_venta_v2510"):
                tipo_venta = st.selectbox("Tipo de venta", ["Contado", "Crédito"])
                cliente_nombre = st.selectbox("Cliente", list(opciones_cliente.keys()))
                metodo_pago = st.selectbox("Medio de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto"])
                if tipo_venta == "Contado":
                    monto_pagado = st.number_input("Monto recibido", min_value=0.0, value=float(total), step=1.0)
                    fecha_venc = None
                else:
                    monto_pagado = st.number_input("Pago inicial", min_value=0.0, max_value=float(total), value=0.0, step=1.0)
                    fecha_venc = st.date_input("Fecha de vencimiento", peru_today() + timedelta(days=15))
                saldo = max(total - monto_pagado, 0)
                vuelto = max(monto_pagado - total, 0)
                obs = st.text_area("Observación", placeholder="Entrega, nota interna, pedido...")
                a,b = st.columns(2)
                with a: volver = st.form_submit_button("← Volver al carrito", use_container_width=True)
                with b: confirmar = st.form_submit_button("Confirmar venta", type="primary", use_container_width=True)
            if volver:
                st.session_state.pos_step = "carrito"; st.rerun()
        with right:
            st.markdown("<div class='pay-box'><h3>Resumen</h3>", unsafe_allow_html=True)
            for item in st.session_state.cart:
                st.write(f"{num(item['cantidad'])} x {item['nombre']} — {money(float(item['cantidad'])*float(item['precio']))}")
            st.divider()
            kpi("Total", money(total)); kpi("Pagado", money(monto_pagado)); kpi("Saldo", money(saldo));
            if tipo_venta == "Contado": st.markdown(f"<span class='chip chip-ok'>Vuelto {money(vuelto)}</span>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)
        if confirmar:
            if total <= 0:
                st.error("El total debe ser mayor a cero."); return
            if tipo_venta == "Contado" and monto_pagado < total:
                st.error("En contado, el monto recibido debe cubrir el total. Si quedará deuda, cambia a Crédito."); return
            if tipo_venta == "Crédito" and opciones_cliente[cliente_nombre] is None:
                st.error("Para vender a crédito debes seleccionar un cliente registrado."); return
            u=current_user(); comprobante=f"V{peru_now().strftime('%Y%m%d%H%M%S')}"
            metodo_final = metodo_pago if tipo_venta == "Contado" else ("Crédito" if monto_pagado <= 0 else f"Crédito + {metodo_pago}")
            try:
                with ENGINE.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO ventas (comprobante,id_cliente,id_usuario,vendedor_nombre,metodo_pago,total_venta,monto_pagado,saldo_pendiente,estado_pago,observacion,fecha_vencimiento,tipo_venta)
                        VALUES (:comp,:cli,:uid,:vend,:metodo,:total,:pagado,:saldo,:estado,:obs,:venc,:tipo)
                    """), {"comp": comprobante, "cli": opciones_cliente[cliente_nombre], "uid": u["id_usuario"], "vend": u["nombre"], "metodo": metodo_final, "total": total, "pagado": monto_pagado, "saldo": saldo, "estado": estado_pago, "obs": obs, "venc": str(fecha_venc) if fecha_venc else None, "tipo": tipo_venta})
                    venta_id = int(conn.execute(text("SELECT id_venta FROM ventas WHERE comprobante=:c"), {"c": comprobante}).scalar())
                    for item in st.session_state.cart:
                        subtotal=float(item["cantidad"])*float(item["precio"])
                        conn.execute(text("""
                            INSERT INTO detalle_ventas (id_venta,id_producto,producto_nombre,cantidad,precio_unitario,costo_unitario,subtotal)
                            VALUES (:idv,:idp,:prod,:cant,:precio,:costo,:sub)
                        """), {"idv": venta_id, "idp": item["id_producto"], "prod": item["nombre"], "cant": item["cantidad"], "precio": item["precio"], "costo": item["costo"], "sub": subtotal})
                        conn.execute(text("""
                            INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion)
                            VALUES (:idp,'SALIDA_VENTA',:cant,:costo,:ref,:uid,'Venta')
                        """), {"idp": item["id_producto"], "cant": item["cantidad"], "costo": item["costo"], "ref": comprobante, "uid": u["id_usuario"]})
                    if monto_pagado > 0:
                        conn.execute(text("""
                            INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion,id_venta)
                            VALUES ('Ingreso',:concepto,:metodo,:monto,:ref,:uid,:obs,:idv)
                        """), {"concepto": f"Venta {comprobante}", "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": obs, "idv": venta_id})
                    if tipo_venta == "Crédito" and monto_pagado > 0:
                        conn.execute(text("""
                            INSERT INTO pagos_credito (id_venta,id_cliente,metodo_pago,monto,referencia,id_usuario,observacion)
                            VALUES (:idv,:cli,:metodo,:monto,:ref,:uid,:obs)
                        """), {"idv": venta_id, "cli": opciones_cliente[cliente_nombre], "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": "Pago inicial"})
                clear_product_cache(); clear_report_cache()
                st.session_state.cart=[]; st.session_state.pos_step="carrito"; st.session_state.last_sale_id=venta_id; st.session_state.show_success_sale=True
                st.rerun()
            except Exception as e:
                st.error("No se pudo registrar la venta."); st.exception(e)


def page_panel_dueno():
    ensure_v25_10_schema()
    hero("Panel del dueño", "Control ejecutivo: ventas, métodos de pago, vendedores, caja y stock crítico.", "📊")
    if not is_admin():
        st.warning("Solo administrador puede ver el panel del dueño.")
        return
    d1, d2 = st.columns(2)
    with d1:
        desde = st.date_input("Desde", peru_today(), key="panel_desde_v2510")
    with d2:
        hasta = st.date_input("Hasta", peru_today(), key="panel_hasta_v2510")
    ventas_all = _ventas_rango_todas(desde, hasta)
    ventas = _filter_ventas_df(ventas_all, estado="Vigentes")
    detalle = detalle_productos_vendidos(desde, hasta)
    caja = query_df("SELECT * FROM caja WHERE DATE(fecha) BETWEEN :d AND :h AND COALESCE(anulada,0)=0", {"d": str(desde), "h": str(hasta)})
    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        if not ventas.empty and col in ventas.columns:
            ventas[col] = pd.to_numeric(ventas[col], errors="coerce").fillna(0)
    total = float(ventas["total_venta"].sum()) if not ventas.empty else 0
    cobrado = float(ventas["monto_pagado"].sum()) if not ventas.empty else 0
    credito = float(ventas["saldo_pendiente"].sum()) if not ventas.empty else 0
    utilidad = float(pd.to_numeric(detalle.get("utilidad", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if detalle is not None and not detalle.empty else 0
    ingresos = float(pd.to_numeric(caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"], errors="coerce").fillna(0).sum()) if not caja.empty else 0
    egresos = float(pd.to_numeric(caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"], errors="coerce").fillna(0).sum()) if not caja.empty else 0
    a,b,c,d = st.columns(4)
    with a: kpi("Ventas", money(total), f"{len(ventas)} comprobantes")
    with b: kpi("Cobrado", money(cobrado), "Ingresos recibidos")
    with c: kpi("Crédito", money(credito), "Por cobrar")
    with d: kpi("Caja neta", money(ingresos-egresos), "Ingresos - egresos")

    vend = ventas.groupby("vendedor_nombre", as_index=False).agg(total=("total_venta","sum")) if not ventas.empty else pd.DataFrame()
    met = ventas.groupby("metodo_pago", as_index=False).agg(total=("total_venta","sum")) if not ventas.empty else pd.DataFrame()
    day = ventas.copy()
    if not day.empty:
        day["dia"] = day["fecha"].apply(fmt_date)
        day = day.groupby("dia", as_index=False).agg(total=("total_venta","sum"))
    g1, g2, g3 = st.columns(3)
    with g1:
        _bar_report_small(vend, "vendedor_nombre", "total", "Ventas por vendedor", 6)
    with g2:
        _bar_report_small(met, "metodo_pago", "total", "Métodos de pago", 7)
    with g3:
        _bar_report_small(day, "dia", "total", "Ventas por día", 7)

    c1,c2 = st.columns([1.3, .9])
    with c1:
        st.subheader("Ventas recientes")
        recent = ventas.head(10).copy()
        if not recent.empty:
            recent["fecha_fmt"] = recent["fecha"].apply(fmt_dt)
            recent["total_fmt"] = recent["total_venta"].apply(money)
            html_table(recent, ["comprobante","fecha_fmt","cliente","vendedor_nombre","metodo_pago","total_fmt"], ["Comprobante","Fecha Perú","Cliente","Vendedor","Pago","Total"], 10)
        else:
            st.info("Sin ventas recientes.")
    with c2:
        st.subheader("Stock crítico")
        prod = productos_con_stock()
        if not prod.empty:
            crit = prod[pd.to_numeric(prod["stock_actual"], errors="coerce").fillna(0) <= pd.to_numeric(prod["stock_minimo"], errors="coerce").fillna(0)].head(12)
            if crit.empty:
                st.success("Sin stock crítico.")
            else:
                for _, r in crit.iterrows():
                    st.markdown(f"<span class='chip chip-red'>⚠ {esc(r['nombre_producto'])} · Stock {num(r['stock_actual'])}</span>", unsafe_allow_html=True)
        else:
            st.info("Sin productos registrados.")


def page_reportes():
    ensure_v25_10_schema()
    hero("Reportes ejecutivos", "Audita ventas por vendedor, método, día, cliente, estado y comprobante.", "📈")
    if not is_admin():
        st.warning("Solo administrador puede ver reportes.")
        return
    d1, d2 = st.columns(2)
    with d1:
        desde = st.date_input("Desde", peru_today() - timedelta(days=7), key="rep_desde_v2510")
    with d2:
        hasta = st.date_input("Hasta", peru_today(), key="rep_hasta_v2510")
    ventas_all = _ventas_rango_todas(desde, hasta)
    if ventas_all.empty:
        st.info("No hay ventas para el rango seleccionado.")
        return
    vendedor, metodo, estado, cliente, texto = _ventas_filters("rep_v2510", ventas_all)
    ventas = _filter_ventas_df(ventas_all, vendedor, metodo, estado, cliente, texto)
    if ventas.empty:
        st.warning("No hay comprobantes con esos filtros.")
        return
    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        ventas[col] = pd.to_numeric(ventas[col], errors="coerce").fillna(0)
    a,b,c,d = st.columns(4)
    with a: kpi("Total vendido", money(ventas["total_venta"].sum()), f"{len(ventas)} comprobantes")
    with b: kpi("Cobrado", money(ventas["monto_pagado"].sum()), "Ingresos recibidos")
    with c: kpi("Crédito", money(ventas["saldo_pendiente"].sum()), "Saldo pendiente")
    with d: kpi("Ticket promedio", money(ventas["total_venta"].sum()/max(len(ventas),1)), "Promedio")

    vend = ventas.groupby("vendedor_nombre", as_index=False).agg(total=("total_venta","sum"), ventas=("id_venta","count"))
    met = ventas.groupby("metodo_pago", as_index=False).agg(total=("total_venta","sum"), ventas=("id_venta","count"))
    vday = ventas.copy(); vday["dia"] = vday["fecha"].apply(fmt_date)
    dia = vday.groupby("dia", as_index=False).agg(total=("total_venta","sum"))
    g1, g2, g3 = st.columns(3)
    with g1:
        _bar_report_small(vend, "vendedor_nombre", "total", "Ventas por vendedor", 10)
    with g2:
        _bar_report_small(met, "metodo_pago", "total", "Métodos de pago", 10)
    with g3:
        _bar_report_small(dia, "dia", "total", "Ventas por día", 14)

    detalle = detalle_productos_vendidos(desde, hasta)
    if detalle is not None and not detalle.empty:
        detalle["total_vendido"] = pd.to_numeric(detalle["total_vendido"], errors="coerce").fillna(0)
        top = detalle.groupby("producto", as_index=False).agg(total=("total_vendido","sum")).sort_values("total", ascending=False)
        _bar_report_html(top, "producto", "total", "Productos vendidos", 12, True)

    st.subheader("Detalle de comprobantes")
    st.caption("Usa los filtros superiores para revisar por día, vendedor, método, cliente, estado o número de comprobante.")
    detv = ventas.copy()
    detv["fecha_fmt"] = detv["fecha"].apply(fmt_dt)
    detv["estado_real"] = detv.apply(lambda r: "Anulada" if int(float(r.get("anulada") or 0)) == 1 else str(r.get("estado_pago") or ""), axis=1)
    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        detv[col+"_fmt"] = detv[col].apply(money)
    html_table(detv, ["comprobante","fecha_fmt","cliente","vendedor_nombre","metodo_pago","total_venta_fmt","monto_pagado_fmt","saldo_pendiente_fmt","estado_real"], ["Comprobante","Fecha Perú","Cliente","Vendedor","Método","Total","Pagado","Saldo","Estado"], 300)

    st.subheader("Anular venta / comprobante")
    st.markdown("<div class='audit-box'>La anulación no borra historial: devuelve stock, revierte caja con un egreso y marca la venta como ANULADA.</div>", unsafe_allow_html=True)
    vigentes = _filter_ventas_df(ventas_all, estado="Vigentes")
    if vigentes.empty:
        st.info("No hay ventas vigentes para anular en este rango.")
    else:
        opts = {f"{r['comprobante']} · {fmt_dt(r['fecha'])} · {r['cliente']} · {money(r['total_venta'])}": int(r["id_venta"]) for _, r in vigentes.iterrows()}
        with st.form("form_anular_v2510"):
            label = st.selectbox("Selecciona comprobante vigente", list(opts.keys()))
            motivo = st.text_area("Motivo obligatorio", placeholder="Ej: error de producto, error de cantidad, venta duplicada, cliente canceló...")
            confirmar = st.checkbox("Confirmo que deseo anular esta venta y revertir stock/caja")
            btn = st.form_submit_button("Anular venta", type="primary", use_container_width=True)
        if btn:
            if not confirmar:
                st.error("Marca la confirmación antes de anular.")
            elif not motivo.strip():
                st.error("Ingresa el motivo de anulación.")
            else:
                try:
                    anular_venta(opts[label], motivo)
                    st.success("Venta anulada. Se revirtió stock y caja según corresponda.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))



# ============================================================
# V25.11 - INVENTARIO POR CATEGORÍA + CRÉDITOS FILTRADOS + BOLETA CONSECUTIVA
# ============================================================
def inject_css_v25_11():
    st.markdown("""
    <style>
      .category-grid {display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin:14px 0 18px;}
      .category-card {background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:16px; box-shadow:0 8px 24px rgba(15,23,42,.05);}
      .category-card .cat-title {font-size:15px; font-weight:950; color:#0f172a; margin-bottom:8px;}
      .category-card .cat-kpi {font-size:24px; font-weight:950; color:#0f172a;}
      .category-card .cat-sub {font-size:12px; color:#64748b; font-weight:750;}
      .info-soft {background:#f8fafc; border:1px solid #e5e7eb; border-radius:18px; padding:16px; color:#0f172a; font-weight:650;}
      .info-soft b {color:#0f172a;}
      .filter-box {background:#fff; border:1px solid #e5e7eb; border-radius:20px; padding:16px; box-shadow:0 8px 24px rgba(15,23,42,.05); margin-bottom:14px;}
      .mini-muted {color:#64748b; font-size:12px; font-weight:750;}
      .products-grid {grid-template-columns:repeat(5,minmax(0,1fr)) !important; gap:14px !important;}
      .product-card {min-height:330px !important; padding:14px !important;}
      .product-img-wrap {height:130px !important;}
      .product-name {font-size:15px !important; line-height:1.18 !important;}
      .product-meta {font-size:12px !important;}
      .product-price, .pos-price {font-size:22px !important;}
      .stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] button, button[data-testid="baseButton-primary"], .stDownloadButton button[kind="primary"] {
          background:#0f172a !important; color:#ffffff !important; border-color:#0f172a !important;
      }
      .stButton > button[kind="primary"] *, [data-testid="stFormSubmitButton"] button *, button[data-testid="baseButton-primary"] *, .stDownloadButton button[kind="primary"] * {color:#ffffff !important; opacity:1 !important;}
      @media (max-width: 1500px){.products-grid{grid-template-columns:repeat(4,minmax(0,1fr)) !important;}}
      @media (max-width: 1100px){.products-grid,.category-grid{grid-template-columns:repeat(2,minmax(0,1fr)) !important;}}
      @media (max-width: 700px){.products-grid,.category-grid{grid-template-columns:1fr !important;} .product-card{min-height:auto !important;} .product-img-wrap{height:160px !important;}}
    </style>
    """, unsafe_allow_html=True)


def ensure_v25_11_schema():
    try:
        ensure_v25_10_schema()
    except Exception:
        pass
    # Campo visible para trazabilidad de comprobantes, sin romper instalaciones anteriores.
    try:
        safe_alter("ventas", "numero_consecutivo INTEGER")
    except Exception:
        pass
    try:
        exec_sql("CREATE INDEX IF NOT EXISTS idx_ventas_comprobante ON ventas(comprobante)")
        exec_sql("CREATE INDEX IF NOT EXISTS idx_mov_stock_fecha ON movimientos_stock(fecha)")
        exec_sql("CREATE INDEX IF NOT EXISTS idx_mov_stock_producto ON movimientos_stock(id_producto)")
    except Exception:
        pass


def next_comprobante_conn(conn):
    """Genera boleta interna consecutiva: B0001, B0002... Después de reiniciar datos, vuelve a B0001."""
    try:
        max_id = conn.execute(text("SELECT COALESCE(MAX(id_venta),0) FROM ventas")).scalar() or 0
    except Exception:
        max_id = 0
    n = int(max_id) + 1
    while True:
        comp = f"B{n:04d}"
        try:
            existe = conn.execute(text("SELECT COUNT(*) FROM ventas WHERE comprobante=:c"), {"c": comp}).scalar() or 0
        except Exception:
            existe = 0
        if int(existe) == 0:
            return comp, n
        n += 1


def _category_summary(productos: pd.DataFrame) -> pd.DataFrame:
    if productos is None or productos.empty:
        return pd.DataFrame(columns=["categoria","productos","stock","valor_costo","valor_venta"])
    df = productos.copy()
    for c in ["stock_actual","costo_unitario","precio_venta"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["valor_costo"] = df["stock_actual"] * df["costo_unitario"]
    df["valor_venta"] = df["stock_actual"] * df["precio_venta"]
    return df.groupby("categoria", as_index=False).agg(productos=("id_producto","count"), stock=("stock_actual","sum"), valor_costo=("valor_costo","sum"), valor_venta=("valor_venta","sum")).sort_values("categoria")


def page_inventario():
    ensure_v25_11_schema()
    hero("Inventario", "Control por categoría, stock actual, valorización y últimos movimientos.", "📊")
    if not is_admin():
        st.warning("Solo administrador puede ver inventario completo.")
        return
    productos = productos_con_stock()
    if productos.empty:
        st.info("No hay productos registrados.")
        return
    for col in ["stock_actual","stock_minimo","costo_unitario","precio_venta"]:
        productos[col] = pd.to_numeric(productos[col], errors="coerce").fillna(0)
    total_val = (productos["stock_actual"] * productos["costo_unitario"]).sum()
    total_venta = (productos["stock_actual"] * productos["precio_venta"]).sum()
    total_stock = productos["stock_actual"].sum()
    criticos = productos[productos["stock_actual"] <= productos["stock_minimo"]]
    a,b,c,d = st.columns(4)
    with a: kpi("Productos", str(len(productos)), "Activos")
    with b: kpi("Unidades", num(total_stock), "Stock total")
    with c: kpi("Valorización costo", money(total_val), "Costo inventario")
    with d: kpi("Valor venta", money(total_venta), "Potencial venta")

    cats = _category_summary(productos)
    st.subheader("Categorías")
    if "inventario_categoria" not in st.session_state:
        st.session_state.inventario_categoria = "Todas"
    cols = st.columns(4)
    for i, (_, r) in enumerate(cats.iterrows()):
        with cols[i % 4]:
            st.markdown(f"<div class='category-card'><div class='cat-title'>{esc(r['categoria'])}</div><div class='cat-kpi'>{int(r['productos'])}</div><div class='cat-sub'>Stock {num(r['stock'])} · Costo {money(r['valor_costo'])}</div></div>", unsafe_allow_html=True)
            if st.button(f"Ver {r['categoria']}", key=f"cat_inv_{i}", use_container_width=True):
                st.session_state.inventario_categoria = str(r['categoria'])
                st.rerun()
    st.divider()
    c1, c2 = st.columns([1.2,.8])
    with c1:
        buscar = st.text_input("Buscar en inventario", placeholder="Código, producto o categoría", key="inv_buscar_v2511")
    with c2:
        cat_opts = ["Todas"] + cats["categoria"].astype(str).tolist()
        selected_cat = st.selectbox("Categoría seleccionada", cat_opts, index=cat_opts.index(st.session_state.inventario_categoria) if st.session_state.inventario_categoria in cat_opts else 0, key="inv_cat_sel_v2511")
        st.session_state.inventario_categoria = selected_cat
    view = productos.copy()
    if selected_cat != "Todas":
        view = view[view["categoria"].astype(str).eq(str(selected_cat))]
    if buscar.strip():
        q = buscar.lower().strip()
        view = view[view["codigo"].astype(str).str.lower().str.contains(q, na=False) | view["nombre_producto"].astype(str).str.lower().str.contains(q, na=False) | view["categoria"].astype(str).str.lower().str.contains(q, na=False)]
    st.subheader("Productos del inventario")
    if view.empty:
        st.info("No hay productos para el filtro seleccionado.")
    else:
        show = view.copy()
        show["stock_fmt"] = show["stock_actual"].apply(num)
        show["min_fmt"] = show["stock_minimo"].apply(num)
        show["costo_fmt"] = show["costo_unitario"].apply(money)
        show["precio_fmt"] = show["precio_venta"].apply(money)
        show["creado_fmt"] = show["creado_en"].apply(fmt_dt) if "creado_en" in show.columns else ""
        html_table(show, ["codigo","nombre_producto","categoria","stock_fmt","min_fmt","costo_fmt","precio_fmt","creado_fmt"], ["Código","Producto","Categoría","Stock","Mín.","Costo","Precio","Creado"], 500)
    if not criticos.empty:
        st.warning(f"Hay {len(criticos)} producto(s) en stock crítico o negativo.")

    st.subheader("Últimos movimientos de stock")
    mov = query_df("""
        SELECT ms.fecha, COALESCE(p.codigo,'') AS codigo, COALESCE(p.nombre_producto,'Producto') AS producto,
               COALESCE(c.nombre_categoria,'Sin categoría') AS categoria, ms.tipo, ms.cantidad, ms.costo_unitario, ms.referencia, ms.observacion
        FROM movimientos_stock ms
        LEFT JOIN productos p ON p.id_producto=ms.id_producto
        LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
        ORDER BY ms.fecha DESC
        LIMIT 80
    """)
    if mov.empty:
        st.info("Todavía no hay movimientos registrados.")
    else:
        mov["fecha_fmt"] = mov["fecha"].apply(fmt_dt)
        mov["cantidad_fmt"] = mov["cantidad"].apply(num)
        mov["costo_fmt"] = mov["costo_unitario"].apply(money)
        html_table(mov, ["fecha_fmt","codigo","producto","categoria","tipo","cantidad_fmt","costo_fmt","referencia","observacion"], ["Fecha","Código","Producto","Categoría","Tipo","Cant.","Costo","Referencia","Obs."], 80)


def page_ingreso_mercaderia():
    ensure_v25_11_schema()
    hero("Ingreso de mercadería", "Registra compras o reposición de stock sobre productos ya creados.", "📥")
    if not is_admin():
        st.warning("Solo administrador puede registrar ingresos de mercadería.")
        return
    st.markdown("""
    <div class='info-soft'>
      <b>Diferencia importante:</b><br>
      <b>Productos</b> sirve para crear la ficha del artículo: código, nombre, categoría, precio, costo base e imagen.<br>
      <b>Ingreso de mercadería</b> sirve para aumentar stock de un producto ya creado, registrar proveedor, costo real, método de pago y dejar historial de entrada.
    </div>
    """, unsafe_allow_html=True)
    productos = productos_con_stock()
    if productos.empty:
        st.info("Primero registra productos en el módulo Productos.")
        return
    with st.form("ingreso_mercaderia_v2511"):
        proveedor = st.text_input("Proveedor", placeholder="Nombre del proveedor")
        opciones = {f"{normalize_code(r['codigo'])} · {r['nombre_producto']} · Stock {num(r['stock_actual'])}": int(r['id_producto']) for _, r in productos.iterrows()}
        prod_sel = st.selectbox("Producto", list(opciones.keys()))
        c1,c2,c3 = st.columns(3)
        with c1:
            cantidad = st.number_input("Cantidad ingresada", min_value=0.0, step=1.0)
        with c2:
            costo = st.number_input("Costo unitario", min_value=0.0, step=1.0)
        with c3:
            metodo = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Crédito"])
        c4,c5 = st.columns(2)
        with c4:
            monto_pagado = st.number_input("Monto pagado", min_value=0.0, step=1.0)
        with c5:
            obs = st.text_input("Observación", placeholder="Factura, guía, nota de compra...")
        total = cantidad * costo
        kpi("Total ingreso", money(total), "Costo de mercadería")
        guardar = st.form_submit_button("Registrar ingreso de mercadería", type="primary", use_container_width=True)
    if guardar:
        if cantidad <= 0:
            st.error("Cantidad debe ser mayor a cero.")
        else:
            idp = opciones[prod_sel]
            u = current_user()
            ref = f"ING{peru_now().strftime('%Y%m%d%H%M%S')}"
            with ENGINE.begin() as conn:
                conn.execute(text("INSERT INTO compras (proveedor,total_compra,monto_pagado,metodo_pago,observacion,id_usuario) VALUES (:p,:t,:mp,:m,:o,:u)"), {"p": proveedor, "t": total, "mp": monto_pagado, "m": metodo, "o": obs, "u": u["id_usuario"]})
                conn.execute(text("INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion) VALUES (:id,'ENTRADA_COMPRA',:cant,:costo,:ref,:u,:obs)"), {"id": idp, "cant": cantidad, "costo": costo, "ref": ref, "u": u["id_usuario"], "obs": f"Proveedor: {proveedor}. {obs}"})
                conn.execute(text("UPDATE productos SET costo_unitario=:c, actualizado_en=CURRENT_TIMESTAMP WHERE id_producto=:id"), {"c": costo, "id": idp})
                if monto_pagado > 0:
                    conn.execute(text("INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion) VALUES ('Egreso',:c,:m,:mo,:r,:u,:o)"), {"c": f"Compra {ref}", "m": metodo, "mo": monto_pagado, "r": ref, "u": u["id_usuario"], "o": obs})
            clear_product_cache(); clear_report_cache()
            st.success("Ingreso registrado. Stock actualizado e historial guardado.")
            st.rerun()

    st.subheader("Historial reciente de ingresos")
    hist = query_df("""
        SELECT ms.fecha, ms.referencia, COALESCE(p.codigo,'') AS codigo, COALESCE(p.nombre_producto,'Producto') AS producto,
               COALESCE(c.nombre_categoria,'Sin categoría') AS categoria, ms.cantidad, ms.costo_unitario,
               (ms.cantidad * ms.costo_unitario) AS total, ms.observacion, COALESCE(u.nombre,'') AS usuario
        FROM movimientos_stock ms
        LEFT JOIN productos p ON p.id_producto=ms.id_producto
        LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
        LEFT JOIN usuarios u ON u.id_usuario=ms.id_usuario
        WHERE ms.tipo IN ('ENTRADA_COMPRA','ENTRADA','AJUSTE_POSITIVO')
        ORDER BY ms.fecha DESC
        LIMIT 100
    """)
    if hist.empty:
        st.info("Todavía no hay ingresos de mercadería registrados.")
    else:
        hist["fecha_fmt"] = hist["fecha"].apply(fmt_dt)
        hist["cant_fmt"] = hist["cantidad"].apply(num)
        hist["costo_fmt"] = hist["costo_unitario"].apply(money)
        hist["total_fmt"] = hist["total"].apply(money)
        html_table(hist, ["fecha_fmt","referencia","codigo","producto","categoria","cant_fmt","costo_fmt","total_fmt","usuario","observacion"], ["Fecha","Referencia","Código","Producto","Categoría","Cant.","Costo","Total","Usuario","Obs."], 100)


def page_creditos():
    ensure_v25_11_schema()
    hero("Créditos / cuentas por cobrar", "Filtra por cliente, comprobante y registra abonos generales o por producto.", "💳")
    if not is_admin():
        st.warning("Solo administrador puede ver créditos y cuentas por cobrar.")
        return
    pendientes = _creditos_pendientes_df()
    if pendientes.empty:
        kpi("Total por cobrar", money(0), "Sin pendientes")
        st.success("No hay cuentas por cobrar pendientes.")
        return
    for col in ["total_venta","monto_pagado","saldo_pendiente"]:
        pendientes[col] = pd.to_numeric(pendientes[col], errors="coerce").fillna(0)
    total_pendiente = _safe_float(pendientes["saldo_pendiente"].sum())
    vencidas = 0
    if "fecha_vencimiento" in pendientes.columns:
        fv = pd.to_datetime(pendientes["fecha_vencimiento"], errors="coerce").dt.date
        vencidas = int(((fv < peru_today()) & pendientes["saldo_pendiente"].gt(0)).sum())
    c1,c2,c3 = st.columns(3)
    with c1: kpi("Total por cobrar", money(total_pendiente), "Saldo pendiente")
    with c2: kpi("Clientes con deuda", str(pendientes["cliente"].nunique()), "Control por cliente")
    with c3: kpi("Vencidas", str(vencidas), "Requieren seguimiento")

    st.markdown("<div class='filter-box'>", unsafe_allow_html=True)
    f1,f2,f3 = st.columns([1,1,.8])
    clientes = ["Todos"] + sorted(pendientes["cliente"].astype(str).unique().tolist())
    with f1:
        cliente_f = st.selectbox("Filtrar cliente", clientes, key="credito_filtro_cliente_v2511")
    with f2:
        buscar = st.text_input("Buscar", placeholder="Comprobante, cliente o teléfono", key="credito_buscar_v2511")
    with f3:
        estado_venc = st.selectbox("Vencimiento", ["Todos", "Vencidos", "Por vencer / vigentes"], key="credito_venc_v2511")
    st.markdown("</div>", unsafe_allow_html=True)
    fil = pendientes.copy()
    if cliente_f != "Todos":
        fil = fil[fil["cliente"].astype(str).eq(str(cliente_f))]
    if buscar.strip():
        q = buscar.lower().strip()
        fil = fil[fil["comprobante"].astype(str).str.lower().str.contains(q, na=False) | fil["cliente"].astype(str).str.lower().str.contains(q, na=False) | fil["telefono"].astype(str).str.lower().str.contains(q, na=False)]
    if estado_venc != "Todos":
        fv = pd.to_datetime(fil["fecha_vencimiento"], errors="coerce").dt.date
        if estado_venc == "Vencidos":
            fil = fil[(fv < peru_today()) & fil["saldo_pendiente"].gt(0)]
        else:
            fil = fil[(fv >= peru_today()) | fv.isna()]
    if fil.empty:
        st.info("No hay créditos con esos filtros.")
        return

    st.subheader("Resumen por cliente")
    cli = fil.groupby(["cliente", "telefono"], as_index=False).agg(comprobantes=("id_venta","count"), total=("total_venta","sum"), pagado=("monto_pagado","sum"), saldo=("saldo_pendiente","sum"))
    cli_show = cli.copy()
    for col in ["total","pagado","saldo"]:
        cli_show[col+"_fmt"] = cli_show[col].apply(money)
    html_table(cli_show, ["cliente", "telefono", "comprobantes", "total_fmt", "pagado_fmt", "saldo_fmt"], ["Cliente", "Teléfono", "Ventas", "Total", "Pagado", "Saldo"], 100)

    st.subheader("Registrar abono / pago")
    clientes_opts = sorted(fil["cliente"].astype(str).unique().tolist())
    cliente_sel = st.selectbox("Cliente", clientes_opts, key="credito_cliente_v2511")
    pend_cli = fil[fil["cliente"].astype(str).eq(str(cliente_sel))].copy()
    opts = {f"{r['comprobante']} · Saldo {money(r['saldo_pendiente'])} · {fmt_dt(r['fecha'])}": int(r["id_venta"]) for _, r in pend_cli.iterrows()}
    venta_label = st.selectbox("Comprobante pendiente", list(opts.keys()), key="credito_venta_v2511")
    idv = opts[venta_label]
    venta_sel = pend_cli[pend_cli["id_venta"].eq(idv)].iloc[0]
    saldo_actual = _safe_float(venta_sel["saldo_pendiente"])
    detalles = _venta_detalle(idv)
    product_options = {"Abono general al comprobante": None}
    if not detalles.empty:
        det_show = detalles.copy()
        det_show["subtotal_fmt"] = det_show["subtotal"].apply(money)
        det_show["abonado_fmt"] = det_show["abonado_producto"].apply(money)
        det_show["saldo_producto"] = pd.to_numeric(det_show["subtotal"], errors="coerce").fillna(0) - pd.to_numeric(det_show["abonado_producto"], errors="coerce").fillna(0)
        det_show["saldo_producto_fmt"] = det_show["saldo_producto"].apply(money)
        html_table(det_show, ["producto_nombre", "cantidad", "subtotal_fmt", "abonado_fmt", "saldo_producto_fmt"], ["Producto", "Cant.", "Subtotal", "Abonado", "Saldo producto"], 50)
        for _, d in detalles.iterrows():
            prod = str(d.get("producto_nombre") or "Producto")
            product_options[f"{prod} · saldo estimado {money(float(d.get('subtotal') or 0)-float(d.get('abonado_producto') or 0))}"] = int(d.get("id_detalle"))
    with st.form("form_pago_credito_producto_v2511"):
        aplicar_label = st.selectbox("Aplicar pago a", list(product_options.keys()))
        col1, col2, col3 = st.columns([.8,.8,1.2])
        with col1:
            monto = st.number_input("Monto recibido", min_value=0.0, max_value=float(saldo_actual), value=float(saldo_actual), step=1.0)
        with col2:
            metodo = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto"])
        with col3:
            obs = st.text_input("Observación", placeholder="Ej: abono parcial")
        registrar = st.form_submit_button("Registrar pago y actualizar deuda", type="primary", use_container_width=True)
    if registrar:
        if monto <= 0:
            st.error("El monto debe ser mayor a cero.")
        else:
            id_detalle = product_options[aplicar_label]
            prod_nombre = ""
            if id_detalle is not None and not detalles.empty:
                rowp = detalles[detalles["id_detalle"].eq(id_detalle)]
                if not rowp.empty:
                    prod_nombre = str(rowp.iloc[0].get("producto_nombre") or "")
            nuevo_pagado = _safe_float(venta_sel["monto_pagado"]) + monto
            nuevo_saldo = max(_safe_float(venta_sel["total_venta"]) - nuevo_pagado, 0)
            nuevo_estado = "Pagada" if nuevo_saldo <= 0 else "Parcial"
            with ENGINE.begin() as conn:
                conn.execute(text("UPDATE ventas SET monto_pagado=:pagado, saldo_pendiente=:saldo, estado_pago=:estado WHERE id_venta=:id"), {"pagado": nuevo_pagado, "saldo": nuevo_saldo, "estado": nuevo_estado, "id": idv})
                conn.execute(text("""
                    INSERT INTO pagos_credito (id_venta,id_cliente,metodo_pago,monto,referencia,id_usuario,observacion,id_detalle,producto_nombre)
                    VALUES (:idv,:cli,:metodo,:monto,:ref,:uid,:obs,:idd,:prod)
                """), {"idv": idv, "cli": int(venta_sel["id_cliente"]) if pd.notna(venta_sel.get("id_cliente")) else None, "metodo": metodo, "monto": monto, "ref": venta_sel["comprobante"], "uid": current_user()["id_usuario"], "obs": obs, "idd": id_detalle, "prod": prod_nombre})
                conn.execute(text("""
                    INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion,id_venta)
                    VALUES ('Ingreso',:concepto,:metodo,:monto,:ref,:uid,:obs,:idv)
                """), {"concepto": f"Pago crédito {venta_sel['comprobante']}", "metodo": metodo, "monto": monto, "ref": venta_sel["comprobante"], "uid": current_user()["id_usuario"], "obs": obs, "idv": idv})
            clear_report_cache()
            st.success(f"Pago registrado. Nuevo saldo: {money(nuevo_saldo)}")
            st.rerun()

    st.subheader("Pendientes por cobrar")
    df = fil.copy()
    df["fecha_fmt"] = df["fecha"].apply(fmt_dt)
    df["total_fmt"] = df["total_venta"].apply(money)
    df["pagado_fmt"] = df["monto_pagado"].apply(money)
    df["saldo_fmt"] = df["saldo_pendiente"].apply(money)
    html_table(df, ["comprobante","fecha_fmt","fecha_vencimiento","cliente","telefono","total_fmt","pagado_fmt","saldo_fmt","estado_pago"], ["Comprobante","Fecha","Vence","Cliente","Teléfono","Total","Pagado","Saldo","Estado"], 300)

    with st.expander("Historial de pagos registrados", expanded=False):
        pagos = query_df("""
            SELECT pc.fecha, pc.referencia, COALESCE(c.nombre_cliente,'Cliente') AS cliente, pc.metodo_pago, pc.monto, pc.producto_nombre, pc.observacion
            FROM pagos_credito pc
            LEFT JOIN clientes c ON c.id_cliente=pc.id_cliente
            ORDER BY pc.fecha DESC
            LIMIT 300
        """)
        if pagos.empty:
            st.info("Todavía no hay pagos de crédito registrados.")
        else:
            pagos["fecha_fmt"] = pagos["fecha"].apply(fmt_dt)
            pagos["monto_fmt"] = pagos["monto"].apply(money)
            html_table(pagos, ["fecha_fmt","referencia","cliente","producto_nombre","metodo_pago","monto_fmt","observacion"], ["Fecha","Comprobante","Cliente","Producto aplicado","Método","Monto","Observación"], 300)


def page_ventas():
    ensure_v25_11_schema()
    hero("Venta rápida", "Selecciona producto, confirma con vista previa, cobra y genera boleta interna consecutiva.", "🧾")
    if "cart" not in st.session_state:
        st.session_state.cart = []
    if "pos_step" not in st.session_state:
        st.session_state.pos_step = "carrito"

    if st.session_state.get("show_success_sale") and st.session_state.get("last_sale_id"):
        venta_id = int(st.session_state.last_sale_id)
        vdf = query_df("SELECT comprobante,total_venta,monto_pagado,saldo_pendiente,estado_pago FROM ventas WHERE id_venta=:id", {"id": venta_id})
        saldo = float(vdf.iloc[0]["saldo_pendiente"] or 0) if not vdf.empty else 0
        st.markdown("<div class='success-panel'><div class='success-icon'>✅</div><div class='success-title'>¡Venta registrada!</div><div class='success-sub'>Descarga, imprime o continúa vendiendo.</div></div>", unsafe_allow_html=True)
        render_receipt(venta_id)
        pdf = generate_receipt_pdf(venta_id)
        st.markdown("<div class='no-print'>", unsafe_allow_html=True)
        a,b,c = st.columns(3)
        with a:
            if pdf:
                st.download_button("⬇️ Descargar PDF", data=pdf, file_name=f"boleta_{vdf.iloc[0]['comprobante'] if not vdf.empty else venta_id}.pdf", mime="application/pdf", use_container_width=True)
        with b:
            print_button_component("🖨️ Imprimir boleta")
        with c:
            if st.button("Seguir vendiendo", type="primary", use_container_width=True):
                st.session_state.show_success_sale = False
                st.session_state.pos_step = "carrito"
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        if saldo > 0:
            st.warning(f"Esta venta quedó con saldo pendiente: {money(saldo)}. Registra pagos en 💳 Créditos.")
        return

    total_cart = sum(float(i.get("cantidad", 0)) * float(i.get("precio", 0)) for i in st.session_state.cart)
    c1,c2,c3 = st.columns(3)
    with c1: kpi("Carrito", f"{len(st.session_state.cart)} productos", "Edita antes de cobrar")
    with c2: kpi("Total actual", money(total_cart), "No se registra hasta confirmar")
    with c3: kpi("Paso", "Carrito" if st.session_state.pos_step == "carrito" else "Pago", "POS rápido")

    if st.session_state.pos_step == "carrito":
        productos = productos_con_stock()
        if not productos.empty:
            productos["stock_actual"] = pd.to_numeric(productos["stock_actual"], errors="coerce").fillna(0)
            productos = productos[productos["stock_actual"] > 0].copy()
        left, right = st.columns([1.05,.95])
        with left:
            st.markdown("<div class='pos-panel'><h3>Agregar producto</h3><p class='product-meta'>La imagen solo carga para el producto seleccionado, así el POS se mantiene rápido.</p>", unsafe_allow_html=True)
            if productos.empty:
                st.info("No hay productos con stock disponible.")
            else:
                productos["label_pos"] = productos.apply(lambda r: f"{normalize_code(r.get('codigo'))} · {r.get('nombre_producto')} · Stock {num(r.get('stock_actual'))} · {money(r.get('precio_venta'))}", axis=1)
                with st.form("form_add_pos_v2511", clear_on_submit=False):
                    sel = st.selectbox("Producto", productos["label_pos"].tolist(), key="pos_producto_select_v2511")
                    r = productos[productos["label_pos"].eq(sel)].iloc[0]
                    stock = float(r.get("stock_actual") or 0)
                    img = product_image_candidates(r)[0] if product_image_candidates(r) else ""
                    img_html = f"<img class='preview-img' src='{esc(img)}'>" if img else "<div class='preview-img'></div>"
                    st.markdown(f"""
                    <div class='preview-product'>
                      {img_html}
                      <div>
                        <div class='preview-title'>{esc(r.get('nombre_producto'))}</div>
                        <div class='preview-meta'>Código {esc(r.get('codigo'))} · {esc(r.get('categoria'))} · Stock {num(stock)}</div>
                        <div class='preview-price'>{money(r.get('precio_venta'))}</div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)
                    a,b = st.columns(2)
                    with a:
                        cantidad = st.number_input("Cantidad", min_value=1.0, max_value=max(stock,1.0), value=1.0, step=1.0, key="pos_cantidad_v2511")
                    with b:
                        precio_default = float(r.get("precio_venta") or 0)
                        precio = st.number_input("Precio", min_value=0.0, value=precio_default, step=1.0, key="pos_precio_v2511") if is_admin() else precio_default
                        if not is_admin():
                            st.text_input("Precio", value=money(precio), disabled=True, key="pos_precio_view_v2511")
                    st.markdown(f"<span class='chip chip-ok'>Stock {num(stock)}</span><span class='chip chip-dark'>Subtotal {money(float(cantidad)*float(precio))}</span>", unsafe_allow_html=True)
                    add = st.form_submit_button("🛒 Agregar al carrito", type="primary", use_container_width=True)
                if add:
                    found = False
                    for item in st.session_state.cart:
                        if item["id_producto"] == int(r["id_producto"]):
                            item["cantidad"] = min(float(item["cantidad"]) + float(cantidad), stock)
                            item["precio"] = float(precio)
                            found = True
                            break
                    if not found:
                        st.session_state.cart.append({"id_producto": int(r["id_producto"]), "nombre": r["nombre_producto"], "codigo": r.get("codigo", ""), "precio": float(precio), "costo": float(r.get("costo_unitario") or 0), "stock": stock, "cantidad": float(cantidad)})
                    st.toast("Producto agregado")
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with right:
            st.markdown("<div class='pos-panel'><h3>🛒 Carrito</h3>", unsafe_allow_html=True)
            if not st.session_state.cart:
                st.info("Agrega productos para vender.")
            else:
                total = 0.0
                nuevo = []
                for idx, item in enumerate(st.session_state.cart):
                    st.markdown(f"<div class='cart-item-pro'><strong>{esc(item['nombre'])}</strong><div class='meta'>{esc(item.get('codigo',''))} · Stock {num(item.get('stock',0))}</div></div>", unsafe_allow_html=True)
                    c1,c2,c3,c4 = st.columns([.6,.75,.75,.25])
                    with c1:
                        cant = st.number_input("Cant.", min_value=0.0, max_value=float(item["stock"]), value=float(item["cantidad"]), step=1.0, key=f"cart_cant_v2511_{idx}")
                    with c2:
                        precio = st.number_input("Precio", min_value=0.0, value=float(item["precio"]), step=1.0, key=f"cart_precio_v2511_{idx}") if is_admin() else float(item["precio"])
                        if not is_admin(): st.text_input("Precio", value=money(precio), disabled=True, key=f"cart_precio_view_v2511_{idx}")
                    with c3:
                        st.text_input("Subtotal", value=money(cant*precio), disabled=True, key=f"cart_sub_v2511_{idx}")
                    with c4:
                        if st.button("❌", key=f"cart_del_v2511_{idx}"):
                            cant = 0
                    if cant > 0:
                        item["cantidad"] = cant; item["precio"] = precio
                        total += cant * precio
                        nuevo.append(item)
                st.session_state.cart = nuevo
                st.markdown(f"<div class='pos-total-banner'><b>Total</b><b>{money(total)}</b></div>", unsafe_allow_html=True)
                a,b = st.columns(2)
                with a:
                    if st.button("Vaciar", use_container_width=True):
                        st.session_state.cart = []
                        st.rerun()
                with b:
                    if st.button("Continuar al pago", type="primary", use_container_width=True, disabled=total<=0):
                        st.session_state.pos_step = "pago"
                        st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
    else:
        if not st.session_state.cart:
            st.warning("El carrito está vacío.")
            if st.button("Volver al carrito"):
                st.session_state.pos_step = "carrito"
                st.rerun()
            return
        total = sum(float(i.get("cantidad", 0)) * float(i.get("precio", 0)) for i in st.session_state.cart)
        st.markdown(f"<div class='pos-total-banner'><b>Total a cobrar</b><b>{money(total)}</b></div>", unsafe_allow_html=True)
        left,right = st.columns([1.05,.95])
        with left:
            clientes = query_df("SELECT id_cliente, nombre_cliente, telefono FROM clientes WHERE COALESCE(estado,'Activo')='Activo' ORDER BY nombre_cliente")
            opciones_cliente = {"Cliente general": None}
            if not clientes.empty:
                opciones_cliente.update({f"{r['nombre_cliente']}" + (f" · {r['telefono']}" if str(r.get('telefono') or '').strip() else ""): int(r["id_cliente"]) for _, r in clientes.iterrows()})
            with st.form("form_confirmar_venta_v2511"):
                tipo_venta = st.selectbox("Tipo de venta", ["Contado", "Crédito"])
                cliente_nombre = st.selectbox("Cliente", list(opciones_cliente.keys()))
                metodo_pago = st.selectbox("Medio de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto"])
                if tipo_venta == "Contado":
                    monto_pagado = st.number_input("Monto recibido", min_value=0.0, value=float(total), step=1.0)
                    fecha_venc = None
                else:
                    monto_pagado = st.number_input("Pago inicial", min_value=0.0, max_value=float(total), value=0.0, step=1.0)
                    fecha_venc = st.date_input("Fecha de vencimiento", peru_today() + timedelta(days=15))
                saldo = max(total - monto_pagado, 0)
                vuelto = max(monto_pagado - total, 0)
                obs = st.text_area("Observación", placeholder="Entrega, nota interna, pedido...")
                a,b = st.columns(2)
                with a: volver = st.form_submit_button("← Volver al carrito", use_container_width=True)
                with b: confirmar = st.form_submit_button("Confirmar venta", type="primary", use_container_width=True)
            if volver:
                st.session_state.pos_step = "carrito"; st.rerun()
        with right:
            st.markdown("<div class='pay-box'><h3>Resumen</h3>", unsafe_allow_html=True)
            for item in st.session_state.cart:
                st.write(f"{num(item['cantidad'])} x {item['nombre']} — {money(float(item['cantidad'])*float(item['precio']))}")
            st.divider()
            kpi("Total", money(total)); kpi("Pagado", money(monto_pagado)); kpi("Saldo", money(saldo));
            if tipo_venta == "Contado": st.markdown(f"<span class='chip chip-ok'>Vuelto {money(vuelto)}</span>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)
        if confirmar:
            if total <= 0:
                st.error("El total debe ser mayor a cero."); return
            if tipo_venta == "Contado" and monto_pagado < total:
                st.error("En contado, el monto recibido debe cubrir el total. Si quedará deuda, cambia a Crédito."); return
            if tipo_venta == "Crédito" and opciones_cliente[cliente_nombre] is None:
                st.error("Para vender a crédito debes seleccionar un cliente registrado."); return
            u=current_user()
            metodo_final = metodo_pago if tipo_venta == "Contado" else ("Crédito" if monto_pagado <= 0 else f"Crédito + {metodo_pago}")
            try:
                with ENGINE.begin() as conn:
                    comprobante, numero = next_comprobante_conn(conn)
                    conn.execute(text("""
                        INSERT INTO ventas (comprobante,numero_consecutivo,id_cliente,id_usuario,vendedor_nombre,metodo_pago,total_venta,monto_pagado,saldo_pendiente,estado_pago,observacion,fecha_vencimiento,tipo_venta)
                        VALUES (:comp,:num,:cli,:uid,:vend,:metodo,:total,:pagado,:saldo,:estado,:obs,:venc,:tipo)
                    """), {"comp": comprobante, "num": numero, "cli": opciones_cliente[cliente_nombre], "uid": u["id_usuario"], "vend": u["nombre"], "metodo": metodo_final, "total": total, "pagado": monto_pagado, "saldo": saldo, "estado": estado_pago, "obs": obs, "venc": str(fecha_venc) if fecha_venc else None, "tipo": tipo_venta})
                    venta_id = int(conn.execute(text("SELECT id_venta FROM ventas WHERE comprobante=:c"), {"c": comprobante}).scalar())
                    for item in st.session_state.cart:
                        subtotal=float(item["cantidad"])*float(item["precio"])
                        conn.execute(text("""
                            INSERT INTO detalle_ventas (id_venta,id_producto,producto_nombre,cantidad,precio_unitario,costo_unitario,subtotal)
                            VALUES (:idv,:idp,:prod,:cant,:precio,:costo,:sub)
                        """), {"idv": venta_id, "idp": item["id_producto"], "prod": item["nombre"], "cant": item["cantidad"], "precio": item["precio"], "costo": item["costo"], "sub": subtotal})
                        conn.execute(text("""
                            INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion)
                            VALUES (:idp,'SALIDA_VENTA',:cant,:costo,:ref,:uid,'Venta')
                        """), {"idp": item["id_producto"], "cant": item["cantidad"], "costo": item["costo"], "ref": comprobante, "uid": u["id_usuario"]})
                    if monto_pagado > 0:
                        conn.execute(text("""
                            INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion,id_venta)
                            VALUES ('Ingreso',:concepto,:metodo,:monto,:ref,:uid,:obs,:idv)
                        """), {"concepto": f"Venta {comprobante}", "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": obs, "idv": venta_id})
                    if tipo_venta == "Crédito" and monto_pagado > 0:
                        conn.execute(text("""
                            INSERT INTO pagos_credito (id_venta,id_cliente,metodo_pago,monto,referencia,id_usuario,observacion)
                            VALUES (:idv,:cli,:metodo,:monto,:ref,:uid,:obs)
                        """), {"idv": venta_id, "cli": opciones_cliente[cliente_nombre], "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": "Pago inicial"})
                clear_product_cache(); clear_report_cache()
                st.session_state.cart=[]; st.session_state.pos_step="carrito"; st.session_state.last_sale_id=venta_id; st.session_state.show_success_sale=True
                st.rerun()
            except Exception as e:
                st.error("No se pudo registrar la venta."); st.exception(e)



# ============================================================
# V26.1 - V25.11 ESTABLE + REPORTES EN FILA + INVENTARIO FILTRABLE + POS SCANNER SAFE
# ============================================================
def inject_css_v26_1():
    st.markdown("""
    <style>
      .v261-note {background:#ecfeff; border:1px solid #a5f3fc; color:#164e63; border-radius:16px; padding:12px 14px; font-weight:800; margin:10px 0;}
      .v261-report-row {display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin:14px 0; align-items:stretch;}
      .v261-section-card {background:#ffffff; border:1px solid #e5e7eb; border-radius:20px; padding:16px; box-shadow:0 12px 28px rgba(15,23,42,.06); min-height:250px;}
      .v261-section-card h3 {margin:0 0 12px !important; color:#0f172a !important; font-size:18px !important; font-weight:950 !important;}
      .scanner-box {background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:14px; box-shadow:0 8px 20px rgba(15,23,42,.05); margin-bottom:12px;}
      .scanner-hint {font-size:12px; color:#64748b; font-weight:750; margin-top:-4px; margin-bottom:8px;}
      .movement-filter-card {background:#fff; border:1px solid #e5e7eb; border-radius:18px; padding:14px; box-shadow:0 8px 20px rgba(15,23,42,.05); margin:10px 0 14px;}
      .stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] button, button[data-testid="baseButton-primary"], .stDownloadButton button[kind="primary"] {
          background:#0f172a !important; color:#ffffff !important; border-color:#0f172a !important;
      }
      .stButton > button[kind="primary"] *, [data-testid="stFormSubmitButton"] button *, button[data-testid="baseButton-primary"] *, .stDownloadButton button[kind="primary"] * {color:#ffffff !important; opacity:1 !important;}
      div[data-baseweb="select"] * {color:#0f172a !important;}
      div[data-baseweb="input"] input, textarea, input {color:#0f172a !important;}
      .preview-product {background:#fff !important;}
      .preview-title, .preview-price {color:#0f172a !important;}
      .preview-meta {color:#64748b !important;}
      @media(max-width:1100px){.v261-report-row{grid-template-columns:1fr}.v261-section-card{min-height:auto}}
    </style>
    """, unsafe_allow_html=True)


def _mini_report_card(df: pd.DataFrame, label_col: str, value_col: str, title: str, limit: int = 8):
    st.markdown("<div class='v261-section-card'>", unsafe_allow_html=True)
    _bar_report_small(df, label_col, value_col, title, limit)
    st.markdown("</div>", unsafe_allow_html=True)


def page_panel_dueno():
    ensure_v25_11_schema()
    hero("Panel del dueño", "Control ejecutivo de ventas, cobros, vendedores, créditos, caja y stock crítico.", "📊")
    if not is_admin():
        st.warning("Solo administrador puede ver el panel del dueño.")
        return
    d1, d2 = st.columns(2)
    with d1:
        desde = st.date_input("Desde", peru_today(), key="panel_desde_v261")
    with d2:
        hasta = st.date_input("Hasta", peru_today(), key="panel_hasta_v261")
    ventas_all = _ventas_rango_todas(desde, hasta)
    ventas = _filter_ventas_df(ventas_all, estado="Vigentes")
    detalle = detalle_productos_vendidos(desde, hasta)
    caja = query_df("SELECT * FROM caja WHERE DATE(fecha) BETWEEN :d AND :h AND COALESCE(anulada,0)=0", {"d": str(desde), "h": str(hasta)})
    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        if not ventas.empty and col in ventas.columns:
            ventas[col] = pd.to_numeric(ventas[col], errors="coerce").fillna(0)
    total = float(ventas["total_venta"].sum()) if not ventas.empty else 0
    cobrado = float(ventas["monto_pagado"].sum()) if not ventas.empty else 0
    credito = float(ventas["saldo_pendiente"].sum()) if not ventas.empty else 0
    utilidad = float(pd.to_numeric(detalle.get("utilidad", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if detalle is not None and not detalle.empty else 0
    ingresos = float(pd.to_numeric(caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"], errors="coerce").fillna(0).sum()) if not caja.empty else 0
    egresos = float(pd.to_numeric(caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"], errors="coerce").fillna(0).sum()) if not caja.empty else 0
    a,b,c,d = st.columns(4)
    with a: kpi("Ventas", money(total), f"{len(ventas)} comprobantes")
    with b: kpi("Cobrado", money(cobrado), "Ingresos recibidos")
    with c: kpi("Crédito", money(credito), "Por cobrar")
    with d: kpi("Caja neta", money(ingresos-egresos), "Ingresos - egresos")

    vend = ventas.groupby("vendedor_nombre", as_index=False).agg(total=("total_venta","sum")) if not ventas.empty else pd.DataFrame()
    met = ventas.groupby("metodo_pago", as_index=False).agg(total=("total_venta","sum")) if not ventas.empty else pd.DataFrame()
    day = ventas.copy()
    if not day.empty:
        day["dia"] = day["fecha"].apply(fmt_date)
        day = day.groupby("dia", as_index=False).agg(total=("total_venta","sum"))
    st.markdown("<div class='v261-report-row'>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    with c1: _mini_report_card(vend, "vendedor_nombre", "total", "Ventas por vendedor", 6)
    with c2: _mini_report_card(met, "metodo_pago", "total", "Métodos de pago", 7)
    with c3: _mini_report_card(day, "dia", "total", "Ventas por día", 7)
    st.markdown("</div>", unsafe_allow_html=True)

    c1,c2 = st.columns([1.3, .9])
    with c1:
        st.subheader("Ventas recientes")
        recent = ventas.head(10).copy()
        if not recent.empty:
            recent["fecha_fmt"] = recent["fecha"].apply(fmt_dt)
            recent["total_fmt"] = recent["total_venta"].apply(money)
            html_table(recent, ["comprobante","fecha_fmt","cliente","vendedor_nombre","metodo_pago","total_fmt"], ["Comprobante","Fecha Perú","Cliente","Vendedor","Pago","Total"], 10)
        else:
            st.info("Sin ventas recientes.")
    with c2:
        st.subheader("Stock crítico")
        prod = productos_con_stock()
        if not prod.empty:
            crit = prod[pd.to_numeric(prod["stock_actual"], errors="coerce").fillna(0) <= pd.to_numeric(prod["stock_minimo"], errors="coerce").fillna(0)].head(12)
            if crit.empty:
                st.success("Sin stock crítico.")
            else:
                for _, r in crit.iterrows():
                    st.markdown(f"<span class='chip chip-red'>⚠ {esc(r['nombre_producto'])} · Stock {num(r['stock_actual'])}</span>", unsafe_allow_html=True)
        else:
            st.info("Sin productos registrados.")


def page_reportes():
    ensure_v25_11_schema()
    hero("Reportes ejecutivos", "Ventas por vendedor, método y día en una sola fila, con filtros para auditar comprobantes.", "📈")
    if not is_admin():
        st.warning("Solo administrador puede ver reportes.")
        return
    d1, d2 = st.columns(2)
    with d1:
        desde = st.date_input("Desde", peru_today() - timedelta(days=7), key="rep_desde_v261")
    with d2:
        hasta = st.date_input("Hasta", peru_today(), key="rep_hasta_v261")
    ventas_all = _ventas_rango_todas(desde, hasta)
    if ventas_all.empty:
        st.info("No hay ventas para el rango seleccionado.")
        return
    vendedor, metodo, estado, cliente, texto = _ventas_filters("rep_v261", ventas_all)
    ventas = _filter_ventas_df(ventas_all, vendedor, metodo, estado, cliente, texto)
    if not ventas.empty:
        ventas["dia_fmt"] = ventas["fecha"].apply(fmt_date)
        dias = ["Todos"] + sorted(ventas["dia_fmt"].astype(str).unique().tolist())
        dia_sel = st.selectbox("Filtro por día específico", dias, key="rep_dia_v261")
        if dia_sel != "Todos":
            ventas = ventas[ventas["dia_fmt"].astype(str).eq(dia_sel)]
    if ventas.empty:
        st.warning("No hay comprobantes con esos filtros.")
        return
    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        ventas[col] = pd.to_numeric(ventas[col], errors="coerce").fillna(0)
    a,b,c,d = st.columns(4)
    with a: kpi("Total vendido", money(ventas["total_venta"].sum()), f"{len(ventas)} comprobantes")
    with b: kpi("Cobrado", money(ventas["monto_pagado"].sum()), "Ingresos recibidos")
    with c: kpi("Crédito", money(ventas["saldo_pendiente"].sum()), "Saldo pendiente")
    with d: kpi("Ticket promedio", money(ventas["total_venta"].sum()/max(len(ventas),1)), "Promedio")

    vend = ventas.groupby("vendedor_nombre", as_index=False).agg(total=("total_venta","sum"), ventas=("id_venta","count"))
    met = ventas.groupby("metodo_pago", as_index=False).agg(total=("total_venta","sum"), ventas=("id_venta","count"))
    vday = ventas.copy(); vday["dia"] = vday["fecha"].apply(fmt_date)
    dia = vday.groupby("dia", as_index=False).agg(total=("total_venta","sum"))
    st.subheader("Resumen visual")
    st.caption("Los tres controles principales se muestran juntos para comparar rápido vendedor, método de pago y día.")
    g1, g2, g3 = st.columns(3)
    with g1: _mini_report_card(vend, "vendedor_nombre", "total", "Ventas por vendedor", 10)
    with g2: _mini_report_card(met, "metodo_pago", "total", "Métodos de pago", 10)
    with g3: _mini_report_card(dia, "dia", "total", "Ventas por día", 14)

    detalle = detalle_productos_vendidos(desde, hasta)
    if detalle is not None and not detalle.empty:
        detalle["total_vendido"] = pd.to_numeric(detalle["total_vendido"], errors="coerce").fillna(0)
        top = detalle.groupby("producto", as_index=False).agg(total=("total_vendido","sum")).sort_values("total", ascending=False)
        _bar_report_html(top, "producto", "total", "Productos vendidos", 12, True)

    st.subheader("Detalle de comprobantes")
    st.caption("Filtra por rango de fecha, día exacto, vendedor, método, cliente, estado o búsqueda de comprobante.")
    detv = ventas.copy()
    detv["fecha_fmt"] = detv["fecha"].apply(fmt_dt)
    detv["estado_real"] = detv.apply(lambda r: "Anulada" if int(float(r.get("anulada") or 0)) == 1 else str(r.get("estado_pago") or ""), axis=1)
    for col in ["total_venta", "monto_pagado", "saldo_pendiente"]:
        detv[col+"_fmt"] = detv[col].apply(money)
    html_table(detv, ["comprobante","fecha_fmt","cliente","vendedor_nombre","metodo_pago","total_venta_fmt","monto_pagado_fmt","saldo_pendiente_fmt","estado_real"], ["Comprobante","Fecha Perú","Cliente","Vendedor","Método","Total","Pagado","Saldo","Estado"], 300)

    st.subheader("Anular venta / comprobante")
    st.markdown("<div class='audit-box'>La anulación no borra historial: devuelve stock, revierte caja con un egreso y marca la venta como ANULADA.</div>", unsafe_allow_html=True)
    vigentes = _filter_ventas_df(ventas_all, estado="Vigentes")
    if vigentes.empty:
        st.info("No hay ventas vigentes para anular en este rango.")
    else:
        opts = {f"{r['comprobante']} · {fmt_dt(r['fecha'])} · {r['cliente']} · {money(r['total_venta'])}": int(r["id_venta"]) for _, r in vigentes.iterrows()}
        with st.form("form_anular_v261"):
            label = st.selectbox("Selecciona comprobante vigente", list(opts.keys()))
            motivo = st.text_area("Motivo obligatorio", placeholder="Ej: error de producto, error de cantidad, venta duplicada, cliente canceló...")
            confirmar = st.checkbox("Confirmo que deseo anular esta venta y revertir stock/caja")
            btn = st.form_submit_button("Anular venta", type="primary", use_container_width=True)
        if btn:
            if not confirmar:
                st.error("Marca la confirmación antes de anular.")
            elif not motivo.strip():
                st.error("Ingresa el motivo de anulación.")
            else:
                try:
                    anular_venta(opts[label], motivo)
                    st.success("Venta anulada. Se revirtió stock y caja según corresponda.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


def page_inventario():
    ensure_v25_11_schema()
    hero("Inventario", "Control por categoría, stock actual, valorización y movimientos filtrables por fecha.", "📊")
    if not is_admin():
        st.warning("Solo administrador puede ver inventario completo.")
        return
    productos = productos_con_stock()
    if productos.empty:
        st.info("No hay productos registrados.")
        return
    for col in ["stock_actual","stock_minimo","costo_unitario","precio_venta"]:
        productos[col] = pd.to_numeric(productos[col], errors="coerce").fillna(0)
    total_val = (productos["stock_actual"] * productos["costo_unitario"]).sum()
    total_venta = (productos["stock_actual"] * productos["precio_venta"]).sum()
    total_stock = productos["stock_actual"].sum()
    criticos = productos[productos["stock_actual"] <= productos["stock_minimo"]]
    a,b,c,d = st.columns(4)
    with a: kpi("Productos", str(len(productos)), "Activos")
    with b: kpi("Unidades", num(total_stock), "Stock total")
    with c: kpi("Valorización costo", money(total_val), "Costo inventario")
    with d: kpi("Valor venta", money(total_venta), "Potencial venta")

    cats = _category_summary(productos)
    st.subheader("Categorías")
    if "inventario_categoria" not in st.session_state:
        st.session_state.inventario_categoria = "Todas"
    cols = st.columns(4)
    for i, (_, r) in enumerate(cats.iterrows()):
        with cols[i % 4]:
            st.markdown(f"<div class='category-card'><div class='cat-title'>{esc(r['categoria'])}</div><div class='cat-kpi'>{int(r['productos'])}</div><div class='cat-sub'>Stock {num(r['stock'])} · Costo {money(r['valor_costo'])}</div></div>", unsafe_allow_html=True)
            if st.button(f"Ver {r['categoria']}", key=f"cat_inv_v261_{i}", use_container_width=True):
                st.session_state.inventario_categoria = str(r['categoria'])
                st.rerun()
    st.divider()
    c1, c2 = st.columns([1.2,.8])
    with c1:
        buscar = st.text_input("Buscar en inventario", placeholder="Código, producto o categoría", key="inv_buscar_v261")
    with c2:
        cat_opts = ["Todas"] + cats["categoria"].astype(str).tolist()
        selected_cat = st.selectbox("Categoría seleccionada", cat_opts, index=cat_opts.index(st.session_state.inventario_categoria) if st.session_state.inventario_categoria in cat_opts else 0, key="inv_cat_sel_v261")
        st.session_state.inventario_categoria = selected_cat
    view = productos.copy()
    if selected_cat != "Todas":
        view = view[view["categoria"].astype(str).eq(str(selected_cat))]
    if buscar.strip():
        q = buscar.lower().strip()
        view = view[view["codigo"].astype(str).str.lower().str.contains(q, na=False) | view["nombre_producto"].astype(str).str.lower().str.contains(q, na=False) | view["categoria"].astype(str).str.lower().str.contains(q, na=False)]
    st.subheader("Productos del inventario")
    if view.empty:
        st.info("No hay productos para el filtro seleccionado.")
    else:
        show = view.copy()
        show["stock_fmt"] = show["stock_actual"].apply(num)
        show["min_fmt"] = show["stock_minimo"].apply(num)
        show["costo_fmt"] = show["costo_unitario"].apply(money)
        show["precio_fmt"] = show["precio_venta"].apply(money)
        show["creado_fmt"] = show["creado_en"].apply(fmt_dt) if "creado_en" in show.columns else ""
        html_table(show, ["codigo","nombre_producto","categoria","stock_fmt","min_fmt","costo_fmt","precio_fmt","creado_fmt"], ["Código","Producto","Categoría","Stock","Mín.","Costo","Precio","Creado"], 500)
    if not criticos.empty:
        st.warning(f"Hay {len(criticos)} producto(s) en stock crítico o negativo.")

    st.subheader("Movimientos de stock")
    st.markdown("<div class='movement-filter-card'>", unsafe_allow_html=True)
    m1,m2,m3,m4 = st.columns([.8,.8,1,1.2])
    with m1:
        mov_desde = st.date_input("Desde", peru_today() - timedelta(days=30), key="mov_desde_v261")
    with m2:
        mov_hasta = st.date_input("Hasta", peru_today(), key="mov_hasta_v261")
    tipos = query_df("SELECT DISTINCT tipo FROM movimientos_stock ORDER BY tipo")
    tipo_opts = ["Todos"] + (tipos["tipo"].dropna().astype(str).tolist() if not tipos.empty else [])
    with m3:
        tipo_f = st.selectbox("Tipo movimiento", tipo_opts, key="mov_tipo_v261")
    with m4:
        mov_buscar = st.text_input("Buscar movimiento", placeholder="Producto, código, referencia u observación", key="mov_buscar_v261")
    st.markdown("</div>", unsafe_allow_html=True)
    mov = query_df("""
        SELECT ms.fecha, COALESCE(p.codigo,'') AS codigo, COALESCE(p.nombre_producto,'Producto') AS producto,
               COALESCE(c.nombre_categoria,'Sin categoría') AS categoria, ms.tipo, ms.cantidad, ms.costo_unitario, ms.referencia, ms.observacion
        FROM movimientos_stock ms
        LEFT JOIN productos p ON p.id_producto=ms.id_producto
        LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
        WHERE DATE(ms.fecha) BETWEEN :d AND :h
        ORDER BY ms.fecha DESC
        LIMIT 500
    """, {"d": str(mov_desde), "h": str(mov_hasta)})
    if not mov.empty:
        if tipo_f != "Todos":
            mov = mov[mov["tipo"].astype(str).eq(tipo_f)]
        if selected_cat != "Todas":
            mov = mov[mov["categoria"].astype(str).eq(str(selected_cat))]
        if mov_buscar.strip():
            q = mov_buscar.strip().lower()
            mov = mov[mov["codigo"].astype(str).str.lower().str.contains(q, na=False) | mov["producto"].astype(str).str.lower().str.contains(q, na=False) | mov["referencia"].astype(str).str.lower().str.contains(q, na=False) | mov["observacion"].astype(str).str.lower().str.contains(q, na=False)]
    if mov.empty:
        st.info("No hay movimientos con esos filtros.")
    else:
        mov["fecha_fmt"] = mov["fecha"].apply(fmt_dt)
        mov["cantidad_fmt"] = mov["cantidad"].apply(num)
        mov["costo_fmt"] = mov["costo_unitario"].apply(money)
        html_table(mov, ["fecha_fmt","codigo","producto","categoria","tipo","cantidad_fmt","costo_fmt","referencia","observacion"], ["Fecha","Código","Producto","Categoría","Tipo","Cant.","Costo","Referencia","Obs."], 500)


def page_ingreso_mercaderia():
    ensure_v25_11_schema()
    hero("Ingreso de mercadería", "Registra compras o reposición de stock sobre productos ya creados, con historial filtrable.", "📥")
    if not is_admin():
        st.warning("Solo administrador puede registrar ingresos de mercadería.")
        return
    st.markdown("""
    <div class='info-soft'>
      <b>Diferencia importante:</b><br>
      <b>Productos</b> crea la ficha del artículo: código, nombre, categoría, precio, costo base e imagen.<br>
      <b>Ingreso de mercadería</b> aumenta el stock de un producto ya creado, registra proveedor, costo real, método de pago y deja historial de entrada.
    </div>
    """, unsafe_allow_html=True)
    productos = productos_con_stock()
    if productos.empty:
        st.info("Primero registra productos en el módulo Productos.")
        return
    # V29: este bloque ya no usa st.form para que cantidad, costo, total y pago se actualicen en vivo.
    proveedor = st.text_input("Proveedor", placeholder="Nombre del proveedor", key="ing_proveedor_v29")
    opciones = {f"{normalize_code(r['codigo'])} · {r['nombre_producto']} · Stock {num(r['stock_actual'])}": int(r['id_producto']) for _, r in productos.iterrows()}
    prod_sel = st.selectbox("Producto", list(opciones.keys()), key="ing_producto_v29")

    c1,c2,c3 = st.columns(3)
    with c1:
        cantidad = st.number_input("Cantidad ingresada", min_value=0.0, step=1.0, key="ing_cantidad_v29")
    with c2:
        costo = st.number_input("Costo unitario", min_value=0.0, step=1.0, key="ing_costo_v29")
    with c3:
        metodo = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Crédito"], key="ing_metodo_v29")

    total = float(cantidad or 0) * float(costo or 0)

    # V29.1: se elimina la caja vacía de "Monto pagado" para pagos al contado.
    # El pago se calcula solo con cantidad × costo unitario y solo se pide adelanto cuando es crédito.
    c4,c5 = st.columns(2)
    with c4:
        if metodo == "Crédito":
            max_pagado = float(total) if total > 0 else 0.0
            monto_pagado = st.number_input(
                "Monto pagado / adelanto",
                min_value=0.0,
                max_value=max_pagado,
                value=0.0,
                step=1.0,
                key="ing_monto_credito_v291",
                help="Solo se llena cuando la compra queda a crédito o con adelanto."
            )
        else:
            monto_pagado = float(total)
            st.markdown(
                f"""
                <div class='info-soft'>
                    <b>Monto pagado automático:</b><br>
                    <span style='font-size:1.55rem;font-weight:900;color:#111827;'>{money(monto_pagado)}</span><br>
                    <span style='color:#64748b;'>Se calcula: cantidad ingresada × costo unitario.</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
    with c5:
        obs = st.text_input("Observación", placeholder="Factura, guía, nota de compra...", key="ing_obs_v291")

    saldo = max(float(total) - float(monto_pagado), 0)
    k1,k2,k3 = st.columns(3)
    with k1: kpi("Total ingreso", money(total), "Cantidad × costo")
    with k2: kpi("Monto pagado", money(monto_pagado), "Calculado en vivo")
    with k3: kpi("Saldo", money(saldo), "Solo queda si es crédito")

    guardar = st.button("Registrar ingreso de mercadería", type="primary", use_container_width=True, key="btn_ing_guardar_v29")
    if guardar:
        if cantidad <= 0:
            st.error("Cantidad debe ser mayor a cero.")
        elif costo <= 0:
            st.error("Costo unitario debe ser mayor a cero.")
        else:
            idp = opciones[prod_sel]
            u = current_user()
            ref = f"ING{peru_now().strftime('%Y%m%d%H%M%S')}"
            with ENGINE.begin() as conn:
                conn.execute(text("""
                    INSERT INTO compras (proveedor,total_compra,monto_pagado,metodo_pago,observacion,id_usuario)
                    VALUES (:p,:t,:mp,:m,:o,:u)
                """), {"p": proveedor, "t": total, "mp": monto_pagado, "m": metodo, "o": obs, "u": u["id_usuario"]})
                conn.execute(text("INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion) VALUES (:id,'ENTRADA_COMPRA',:cant,:costo,:ref,:u,:obs)"), {"id": idp, "cant": cantidad, "costo": costo, "ref": ref, "u": u["id_usuario"], "obs": f"Proveedor: {proveedor}. {obs}"})
                conn.execute(text("UPDATE productos SET costo_unitario=:c, actualizado_en=CURRENT_TIMESTAMP WHERE id_producto=:id"), {"c": costo, "id": idp})
                if monto_pagado > 0:
                    conn.execute(text("INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion) VALUES ('Egreso',:c,:m,:mo,:r,:u,:o)"), {"c": f"Compra {ref}", "m": metodo, "mo": monto_pagado, "r": ref, "u": u["id_usuario"], "o": obs})
            clear_product_cache(); clear_report_cache()
            st.success("Ingreso registrado. Stock actualizado e historial guardado.")
            st.rerun()

    st.subheader("Historial de ingresos de mercadería")
    h1,h2,h3 = st.columns([.8,.8,1.2])
    with h1:
        hist_desde = st.date_input("Desde historial", peru_today() - timedelta(days=30), key="hist_ing_desde_v261")
    with h2:
        hist_hasta = st.date_input("Hasta historial", peru_today(), key="hist_ing_hasta_v261")
    with h3:
        hist_buscar = st.text_input("Buscar historial", placeholder="Producto, código, referencia, proveedor", key="hist_ing_buscar_v261")
    hist = query_df("""
        SELECT ms.fecha, ms.referencia, COALESCE(p.codigo,'') AS codigo, COALESCE(p.nombre_producto,'Producto') AS producto,
               COALESCE(c.nombre_categoria,'Sin categoría') AS categoria, ms.cantidad, ms.costo_unitario,
               (ms.cantidad * ms.costo_unitario) AS total, ms.observacion, COALESCE(u.nombre,'') AS usuario
        FROM movimientos_stock ms
        LEFT JOIN productos p ON p.id_producto=ms.id_producto
        LEFT JOIN categorias c ON c.id_categoria=p.id_categoria
        LEFT JOIN usuarios u ON u.id_usuario=ms.id_usuario
        WHERE ms.tipo IN ('ENTRADA_COMPRA','ENTRADA','AJUSTE_POSITIVO')
          AND DATE(ms.fecha) BETWEEN :d AND :h
        ORDER BY ms.fecha DESC
        LIMIT 500
    """, {"d": str(hist_desde), "h": str(hist_hasta)})
    if not hist.empty and hist_buscar.strip():
        q = hist_buscar.strip().lower()
        hist = hist[hist["referencia"].astype(str).str.lower().str.contains(q, na=False) | hist["codigo"].astype(str).str.lower().str.contains(q, na=False) | hist["producto"].astype(str).str.lower().str.contains(q, na=False) | hist["observacion"].astype(str).str.lower().str.contains(q, na=False)]
    if hist.empty:
        st.info("No hay ingresos de mercadería con esos filtros.")
    else:
        hist["fecha_fmt"] = hist["fecha"].apply(fmt_dt)
        hist["cant_fmt"] = hist["cantidad"].apply(num)
        hist["costo_fmt"] = hist["costo_unitario"].apply(money)
        hist["total_fmt"] = hist["total"].apply(money)
        html_table(hist, ["fecha_fmt","referencia","codigo","producto","categoria","cant_fmt","costo_fmt","total_fmt","usuario","observacion"], ["Fecha","Referencia","Código","Producto","Categoría","Cant.","Costo","Total","Usuario","Obs."], 500)


def page_ventas():
    ensure_v25_11_schema()
    hero("Venta rápida", "POS estable basado en V25.11: escaneo opcional, vista previa, cobro y boleta consecutiva.", "🧾")
    if "cart" not in st.session_state:
        st.session_state.cart = []
    if "pos_step" not in st.session_state:
        st.session_state.pos_step = "carrito"

    if st.session_state.get("show_success_sale") and st.session_state.get("last_sale_id"):
        venta_id = int(st.session_state.last_sale_id)
        vdf = query_df("SELECT comprobante,total_venta,monto_pagado,saldo_pendiente,estado_pago FROM ventas WHERE id_venta=:id", {"id": venta_id})
        saldo = float(vdf.iloc[0]["saldo_pendiente"] or 0) if not vdf.empty else 0
        st.markdown("<div class='success-panel'><div class='success-icon'>✅</div><div class='success-title'>¡Venta registrada!</div><div class='success-sub'>Descarga, imprime o continúa vendiendo.</div></div>", unsafe_allow_html=True)
        render_receipt(venta_id)
        pdf = generate_receipt_pdf(venta_id)
        st.markdown("<div class='no-print'>", unsafe_allow_html=True)
        a,b,c = st.columns(3)
        with a:
            if pdf:
                st.download_button("⬇️ Descargar PDF", data=pdf, file_name=f"boleta_{vdf.iloc[0]['comprobante'] if not vdf.empty else venta_id}.pdf", mime="application/pdf", use_container_width=True)
        with b:
            print_button_component("🖨️ Imprimir boleta")
        with c:
            if st.button("Seguir vendiendo", type="primary", use_container_width=True):
                st.session_state.show_success_sale = False
                st.session_state.pos_step = "carrito"
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        if saldo > 0:
            st.warning(f"Esta venta quedó con saldo pendiente: {money(saldo)}. Registra pagos en 💳 Créditos.")
        return

    total_cart = sum(float(i.get("cantidad", 0)) * float(i.get("precio", 0)) for i in st.session_state.cart)
    c1,c2,c3 = st.columns(3)
    with c1: kpi("Carrito", f"{len(st.session_state.cart)} productos", "Edita antes de cobrar")
    with c2: kpi("Total actual", money(total_cart), "No se registra hasta confirmar")
    with c3: kpi("Paso", "Carrito" if st.session_state.pos_step == "carrito" else "Pago", "POS rápido")

    if st.session_state.pos_step == "carrito":
        productos = productos_con_stock()
        if not productos.empty:
            productos["stock_actual"] = pd.to_numeric(productos["stock_actual"], errors="coerce").fillna(0)
            productos = productos[productos["stock_actual"] > 0].copy()
        left, right = st.columns([1.05,.95])
        with left:
            st.markdown("<div class='pos-panel'><h3>Agregar producto</h3><p class='product-meta'>Busca por nombre, código, marca o categoría. Se muestran productos similares para elegir rápido.</p>", unsafe_allow_html=True)
            if productos.empty:
                st.info("No hay productos con stock disponible.")
            else:
                productos["codigo_norm"] = productos["codigo"].apply(normalize_code)
                productos["label_pos"] = productos.apply(lambda r: f"{normalize_code(r.get('codigo'))} · {r.get('nombre_producto')} · {r.get('categoria')} · Stock {num(r.get('stock_actual'))} · {money(r.get('precio_venta'))}", axis=1)

                st.caption("V29.1: abre el desplegable y escribe. La lista filtra mientras escribes, sin presionar Enter y con menos lag.")
                labels = productos["label_pos"].tolist()
                sel = st.selectbox(
                    "Buscar y seleccionar producto",
                    labels,
                    index=0,
                    key="pos_producto_select_v291",
                    help="Haz clic en la lista y escribe parte del nombre, código, marca o categoría. Streamlit filtra al instante dentro del desplegable."
                )
                r = productos[productos["label_pos"].eq(sel)].iloc[0]

                with st.expander("Escáner por código de barras / código interno", expanded=False):
                    scan = st.text_input("Escanear o escribir código exacto", placeholder="Ejemplo: 0003", key="pos_scan_exact_v291")
                    st.caption("El lector USB normalmente escribe el código y manda Enter. Si coincide con un producto, selecciónalo desde el desplegable o agrégalo con el botón.")
                    if scan.strip():
                        q_norm = normalize_code(scan.strip())
                        exact = productos[productos["codigo_norm"].astype(str).eq(q_norm)]
                        if exact.empty:
                            st.warning("Código no encontrado.")
                        else:
                            rex = exact.iloc[0]
                            st.success(f"Encontrado: {rex.get('nombre_producto')} · Stock {num(rex.get('stock_actual'))} · {money(rex.get('precio_venta'))}")
                            if st.button("Agregar código escaneado al carrito", type="primary", use_container_width=True, key="btn_scan_add_v291"):
                                stock_scan = float(rex.get("stock_actual") or 0)
                                precio_scan = float(rex.get("precio_venta") or 0)
                                found_scan = False
                                for item in st.session_state.cart:
                                    if item["id_producto"] == int(rex["id_producto"]):
                                        item["cantidad"] = min(float(item["cantidad"]) + 1.0, stock_scan)
                                        item["precio"] = precio_scan
                                        found_scan = True
                                        break
                                if not found_scan:
                                    st.session_state.cart.append({"id_producto": int(rex["id_producto"]), "nombre": rex["nombre_producto"], "codigo": rex.get("codigo", ""), "precio": precio_scan, "costo": float(rex.get("costo_unitario") or 0), "stock": stock_scan, "cantidad": 1.0})
                                st.session_state.pos_scan_exact_v291 = ""
                                st.toast("Producto agregado por código")
                                st.rerun()

                if True:
                    stock = float(r.get("stock_actual") or 0)
                    candidates = product_image_candidates(r)
                    img = candidates[0] if candidates else ""
                    img_html = f"<img class='preview-img' src='{esc(img)}'>" if img else "<div class='preview-img'></div>"
                    st.markdown(f"""
                    <div class='preview-product'>
                      {img_html}
                      <div>
                        <div class='preview-title'>{esc(r.get('nombre_producto'))}</div>
                        <div class='preview-meta'>Código {esc(r.get('codigo'))} · {esc(r.get('categoria'))} · Stock {num(stock)}</div>
                        <div class='preview-price'>{money(r.get('precio_venta'))}</div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)
                    a,b = st.columns(2)
                    idp_actual = int(r['id_producto'])
                    with a:
                        cantidad = st.number_input("Cantidad", min_value=1.0, max_value=max(stock,1.0), value=1.0, step=1.0, key=f"pos_cantidad_v29_{idp_actual}")
                    with b:
                        precio_default = float(r.get("precio_venta") or 0)
                        precio = st.number_input("Precio", min_value=0.0, value=precio_default, step=1.0, key=f"pos_precio_v29_{idp_actual}") if is_admin() else precio_default
                        if not is_admin():
                            st.text_input("Precio", value=money(precio), disabled=True, key=f"pos_precio_view_v29_{idp_actual}")
                    st.markdown(f"<span class='chip chip-ok'>Stock {num(stock)}</span><span class='chip chip-dark'>Subtotal {money(float(cantidad)*float(precio))}</span>", unsafe_allow_html=True)
                    add = st.button("🛒 Agregar al carrito", type="primary", use_container_width=True, key=f"btn_add_pos_v29_{idp_actual}")
                    if add:
                        found = False
                        for item in st.session_state.cart:
                            if item["id_producto"] == int(r["id_producto"]):
                                item["cantidad"] = min(float(item["cantidad"]) + float(cantidad), stock)
                                item["precio"] = float(precio)
                                found = True
                                break
                        if not found:
                            st.session_state.cart.append({"id_producto": int(r["id_producto"]), "nombre": r["nombre_producto"], "codigo": r.get("codigo", ""), "precio": float(precio), "costo": float(r.get("costo_unitario") or 0), "stock": stock, "cantidad": float(cantidad)})
                        st.toast("Producto agregado")
                        st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with right:
            st.markdown("<div class='pos-panel'><h3>🛒 Carrito</h3>", unsafe_allow_html=True)
            if not st.session_state.cart:
                st.info("Agrega productos para vender.")
            else:
                total = 0.0
                nuevo = []
                for idx, item in enumerate(st.session_state.cart):
                    st.markdown(f"<div class='cart-item-pro'><strong>{esc(item['nombre'])}</strong><div class='meta'>{esc(item.get('codigo',''))} · Stock {num(item.get('stock',0))}</div></div>", unsafe_allow_html=True)
                    c1,c2,c3,c4 = st.columns([.6,.75,.75,.25])
                    with c1:
                        cant = st.number_input("Cant.", min_value=0.0, max_value=float(item["stock"]), value=float(item["cantidad"]), step=1.0, key=f"cart_cant_v261_{idx}")
                    with c2:
                        precio = st.number_input("Precio", min_value=0.0, value=float(item["precio"]), step=1.0, key=f"cart_precio_v261_{idx}") if is_admin() else float(item["precio"])
                        if not is_admin(): st.text_input("Precio", value=money(precio), disabled=True, key=f"cart_precio_view_v261_{idx}")
                    with c3:
                        st.text_input("Subtotal", value=money(cant*precio), disabled=True, key=f"cart_sub_v261_{idx}")
                    with c4:
                        if st.button("❌", key=f"cart_del_v261_{idx}"):
                            cant = 0
                    if cant > 0:
                        item["cantidad"] = cant; item["precio"] = precio
                        total += cant * precio
                        nuevo.append(item)
                st.session_state.cart = nuevo
                st.markdown(f"<div class='pos-total-banner'><b>Total</b><b>{money(total)}</b></div>", unsafe_allow_html=True)
                a,b = st.columns(2)
                with a:
                    if st.button("Vaciar", use_container_width=True):
                        st.session_state.cart = []
                        st.rerun()
                with b:
                    if st.button("Continuar al pago", type="primary", use_container_width=True, disabled=total<=0):
                        st.session_state.pos_step = "pago"
                        st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
    else:
        if not st.session_state.cart:
            st.warning("El carrito está vacío.")
            if st.button("Volver al carrito"):
                st.session_state.pos_step = "carrito"
                st.rerun()
            return
        total = sum(float(i.get("cantidad", 0)) * float(i.get("precio", 0)) for i in st.session_state.cart)
        st.markdown(f"<div class='pos-total-banner'><b>Total a cobrar</b><b>{money(total)}</b></div>", unsafe_allow_html=True)
        left,right = st.columns([1.05,.95])
        with left:
            clientes = query_df("SELECT id_cliente, nombre_cliente, telefono FROM clientes WHERE COALESCE(estado,'Activo')='Activo' ORDER BY nombre_cliente")
            opciones_cliente = {"Cliente general": None}
            if not clientes.empty:
                opciones_cliente.update({f"{r['nombre_cliente']}" + (f" · {r['telefono']}" if str(r.get('telefono') or '').strip() else ""): int(r["id_cliente"]) for _, r in clientes.iterrows()})
            with st.form("form_confirmar_venta_v261"):
                tipo_venta = st.selectbox("Tipo de venta", ["Contado", "Crédito"])
                cliente_nombre = st.selectbox("Cliente", list(opciones_cliente.keys()))
                metodo_pago = st.selectbox("Medio de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Mixto"])
                if tipo_venta == "Contado":
                    monto_pagado = st.number_input("Monto recibido", min_value=0.0, value=float(total), step=1.0)
                    fecha_venc = None
                else:
                    monto_pagado = st.number_input("Pago inicial", min_value=0.0, max_value=float(total), value=0.0, step=1.0)
                    fecha_venc = st.date_input("Fecha de vencimiento", peru_today() + timedelta(days=15))
                saldo = max(total - monto_pagado, 0)
                vuelto = max(monto_pagado - total, 0)
                obs = st.text_area("Observación", placeholder="Entrega, nota interna, pedido...")
                a,b = st.columns(2)
                with a: volver = st.form_submit_button("← Volver al carrito", use_container_width=True)
                with b: confirmar = st.form_submit_button("Confirmar venta", type="primary", use_container_width=True)
            if volver:
                st.session_state.pos_step = "carrito"; st.rerun()
        with right:
            st.markdown("<div class='pay-box'><h3>Resumen</h3>", unsafe_allow_html=True)
            for item in st.session_state.cart:
                st.write(f"{num(item['cantidad'])} x {item['nombre']} — {money(float(item['cantidad'])*float(item['precio']))}")
            st.divider()
            kpi("Total", money(total)); kpi("Pagado", money(monto_pagado)); kpi("Saldo", money(saldo));
            if tipo_venta == "Contado": st.markdown(f"<span class='chip chip-ok'>Vuelto {money(vuelto)}</span>", unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)
        if confirmar:
            if total <= 0:
                st.error("El total debe ser mayor a cero."); return
            if tipo_venta == "Contado" and monto_pagado < total:
                st.error("En contado, el monto recibido debe cubrir el total. Si quedará deuda, cambia a Crédito."); return
            if tipo_venta == "Crédito" and opciones_cliente[cliente_nombre] is None:
                st.error("Para vender a crédito debes seleccionar un cliente registrado."); return
            u=current_user()
            metodo_final = metodo_pago if tipo_venta == "Contado" else ("Crédito" if monto_pagado <= 0 else f"Crédito + {metodo_pago}")
            try:
                with ENGINE.begin() as conn:
                    comprobante, numero = next_comprobante_conn(conn)
                    conn.execute(text("""
                        INSERT INTO ventas (comprobante,numero_consecutivo,id_cliente,id_usuario,vendedor_nombre,metodo_pago,total_venta,monto_pagado,saldo_pendiente,estado_pago,observacion,fecha_vencimiento,tipo_venta)
                        VALUES (:comp,:num,:cli,:uid,:vend,:metodo,:total,:pagado,:saldo,:estado,:obs,:venc,:tipo)
                    """), {"comp": comprobante, "num": numero, "cli": opciones_cliente[cliente_nombre], "uid": u["id_usuario"], "vend": u["nombre"], "metodo": metodo_final, "total": total, "pagado": monto_pagado, "saldo": saldo, "estado": estado_pago, "obs": obs, "venc": str(fecha_venc) if fecha_venc else None, "tipo": tipo_venta})
                    venta_id = int(conn.execute(text("SELECT id_venta FROM ventas WHERE comprobante=:c"), {"c": comprobante}).scalar())
                    for item in st.session_state.cart:
                        subtotal=float(item["cantidad"])*float(item["precio"])
                        conn.execute(text("""
                            INSERT INTO detalle_ventas (id_venta,id_producto,producto_nombre,cantidad,precio_unitario,costo_unitario,subtotal)
                            VALUES (:idv,:idp,:prod,:cant,:precio,:costo,:sub)
                        """), {"idv": venta_id, "idp": item["id_producto"], "prod": item["nombre"], "cant": item["cantidad"], "precio": item["precio"], "costo": item["costo"], "sub": subtotal})
                        conn.execute(text("""
                            INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,id_usuario,observacion)
                            VALUES (:idp,'SALIDA_VENTA',:cant,:costo,:ref,:uid,'Venta')
                        """), {"idp": item["id_producto"], "cant": item["cantidad"], "costo": item["costo"], "ref": comprobante, "uid": u["id_usuario"]})
                    if monto_pagado > 0:
                        conn.execute(text("""
                            INSERT INTO caja (tipo,concepto,metodo_pago,monto,referencia,id_usuario,observacion,id_venta)
                            VALUES ('Ingreso',:concepto,:metodo,:monto,:ref,:uid,:obs,:idv)
                        """), {"concepto": f"Venta {comprobante}", "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": obs, "idv": venta_id})
                    if tipo_venta == "Crédito" and monto_pagado > 0:
                        conn.execute(text("""
                            INSERT INTO pagos_credito (id_venta,id_cliente,metodo_pago,monto,referencia,id_usuario,observacion)
                            VALUES (:idv,:cli,:metodo,:monto,:ref,:uid,:obs)
                        """), {"idv": venta_id, "cli": opciones_cliente[cliente_nombre], "metodo": metodo_pago, "monto": monto_pagado, "ref": comprobante, "uid": u["id_usuario"], "obs": "Pago inicial"})
                clear_product_cache(); clear_report_cache()
                st.session_state.cart=[]; st.session_state.pos_step="carrito"; st.session_state.last_sale_id=venta_id; st.session_state.show_success_sale=True
                st.rerun()
            except Exception as e:
                st.error("No se pudo registrar la venta."); st.exception(e)


# ============================================================
# ARRANQUE
# ============================================================
try:
    init_db()
except Exception as e:
    st.error("No se pudo conectar con la base de datos.")
    st.exception(e)
    st.stop()

inject_css()
inject_css_v25_5()
inject_css_v25_6()
inject_css_v25_7()
inject_css_v25_8()
inject_css_v25_9()
inject_css_v25_10()
inject_css_v25_11()
inject_css_v26_1()

# Catálogo público por URL
try:
    qp = dict(st.query_params)
except Exception:
    qp = {}
if str(qp.get("catalogo", "")).lower() in ["1", "true", "si", "sí"]:
    cfg = cached_settings()
    st.markdown("<div class='public-wrap'>", unsafe_allow_html=True)
    if cfg.get("logo_url"):
        st.image(cfg.get("logo_url"), width=260)
    hero(cfg.get("store_name") or APP_NAME_DEFAULT, "Catálogo comercial de productos", "🛍️")
    page_catalogo_clientes(public=True)
    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

if "user" not in st.session_state:
    login_screen()
else:
    selected = sidebar_nav()
    if selected == "📊 Panel dueño": page_panel_dueno()
    elif selected == "🧾 Ventas": page_ventas()
    elif selected == "💳 Créditos": page_creditos()
    elif selected == "📦 Productos": page_productos()
    elif selected == "📘 Catálogo clientes": page_catalogo_clientes()
    elif selected == "📊 Inventario": page_inventario()
    elif selected == "📥 Ingreso mercadería": page_ingreso_mercaderia()
    elif selected == "👥 Clientes": page_clientes()
    elif selected == "💰 Caja": page_caja()
    elif selected == "📈 Reportes": page_reportes()
    elif selected == "🔐 Usuarios": page_usuarios()
    elif selected == "⚙️ Configuración": page_config_tienda()
    elif selected == "💾 Backup": page_backup()
    elif selected == "📲 Instalar app": page_instalar_app()
    elif selected == "☁️ Estado nube": page_estado_nube()
