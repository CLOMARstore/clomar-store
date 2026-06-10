import os
import io
import zipfile
import hashlib
import secrets
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

APP_VERSION = "V30 Control real del negocio"
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


try:
    init_db()
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
  --bg:#f5f7fb; --panel:#ffffff; --ink:#0f172a; --muted:#667085;
  --brand:#111827; --accent:#f5c542; --green:#16a34a; --red:#dc2626; --line:#e5e7eb;
}
.stApp { background: var(--bg); color: var(--ink); }
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1500px; }
[data-testid="stSidebar"] { background: #ffffff; border-right: 1px solid var(--line); }
[data-testid="stSidebar"] * { color: #111827; }
h1,h2,h3 { letter-spacing:-.02em; color:#0f172a; }
.clomar-hero {background:linear-gradient(135deg,#111827,#1f2937);color:white;border-radius:24px;padding:24px 28px;box-shadow:0 18px 45px rgba(15,23,42,.18);}
.clomar-hero h1{color:white;margin:0;font-size:34px}.clomar-hero p{color:#d1d5db;margin:.4rem 0 0}
.kpi-card {background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:0 14px 32px rgba(15,23,42,.07);min-height:116px;}
.kpi-label{font-size:13px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.04em}.kpi-value{font-size:30px;font-weight:900;color:#0f172a;margin-top:6px}.kpi-sub{font-size:13px;color:var(--muted);margin-top:4px}
.card {background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:0 10px 28px rgba(15,23,42,.06);}
.product-card {background:#fff;border:1px solid #e6e8ef;border-radius:22px;padding:16px;box-shadow:0 10px 22px rgba(15,23,42,.06);height:100%;}
.product-name{font-size:17px;font-weight:900;color:#101828;min-height:48px}.product-price{font-size:26px;font-weight:900;color:#111827;margin-top:8px}.product-meta{font-size:13px;color:#667085}.chip{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:800;border:1px solid #e5e7eb;background:#f9fafb;color:#344054;margin:3px 4px 3px 0}.chip-ok{background:#ecfdf3;color:#027a48;border-color:#abefc6}.chip-warn{background:#fffaeb;color:#b54708;border-color:#fedf89}.chip-red{background:#fef3f2;color:#b42318;border-color:#fecdca}.chip-dark{background:#111827;color:#fff;border-color:#111827}
.clean-table {width:100%;border-collapse:separate;border-spacing:0;background:#fff;border:1px solid #e5e7eb;border-radius:18px;overflow:hidden;box-shadow:0 10px 28px rgba(15,23,42,.05)}.clean-table th{background:#f8fafc;color:#475467;text-align:left;padding:12px 14px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid #e5e7eb}.clean-table td{padding:12px 14px;border-bottom:1px solid #f0f2f5;color:#111827;font-size:14px}.clean-table tr:hover td{background:#f9fafb}.clean-table tr:last-child td{border-bottom:none}
.success-panel {background:#ffffff;border:1px solid #d0d5dd;border-radius:28px;padding:26px;text-align:center;box-shadow:0 25px 50px rgba(15,23,42,.16);}.success-icon{font-size:52px;color:#16a34a}.success-title{font-size:28px;font-weight:900;color:#111827}.success-sub{color:#667085;font-size:16px;margin-top:6px}.receipt {background:white;border:1px solid #d0d5dd;border-radius:20px;padding:22px;max-width:440px;margin:auto;color:#111827}.receipt h2{text-align:center;margin:0}.receipt-line{display:flex;justify-content:space-between;border-bottom:1px dashed #d0d5dd;padding:8px 0;font-size:14px}.receipt-total{font-size:24px;font-weight:900;text-align:right;margin-top:12px}
.no-print {}
@media print { body * { visibility:hidden !important; } #printable-receipt, #printable-receipt * { visibility:visible !important; } #printable-receipt { position:absolute; left:0; top:0; width:100%; } .no-print { display:none !important; } }
button[kind="primary"] {background:#111827!important;border-radius:14px!important;border:1px solid #111827!important;}
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
        cells = "".join([f"<td>{row.get(c, '')}</td>" for c in columns])
        rows.append(f"<tr>{cells}</tr>")
    th = "".join([f"<th>{h}</th>" for h in headers])
    st.markdown(f"<table class='clean-table'><thead><tr>{th}</tr></thead><tbody>{''.join(rows)}</tbody></table>", unsafe_allow_html=True)


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
                 p.marca, p.unidad, p.costo_unitario, p.precio_venta, p.stock_minimo, p.estado
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
    st.markdown("""
    <div class="clomar-hero">
      <h1>🛍️ Clomar Store Cloud</h1>
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
        st.caption("Credenciales iniciales: admin/admin123 y vendedor/venta123. Cámbialas antes de uso real.")
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
    html = f"""
    <div id="printable-receipt" class="receipt">
      <h2>Clomar Store</h2>
      <div style="text-align:center;color:#667085;font-size:13px;">Comprobante de venta</div>
      <div class="receipt-line"><span>N°</span><b>{v['comprobante']}</b></div>
      <div class="receipt-line"><span>Fecha</span><b>{v['fecha']}</b></div>
      <div class="receipt-line"><span>Cliente</span><b>{v['cliente']}</b></div>
      <div class="receipt-line"><span>Vendedor</span><b>{v['vendedor_nombre']}</b></div>
      <div class="receipt-line"><span>Pago</span><b>{v['metodo_pago']}</b></div>
      {lines}
      <div class="receipt-total">TOTAL {money(v['total_venta'])}</div>
      <div style="text-align:center;color:#667085;margin-top:14px;font-size:12px;">Gracias por su compra.</div>
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
    c1, c2, c3 = st.columns(3)
    with c1:
        st.subheader("Ventas por vendedor")
        if not ventas.empty:
            vend = ventas.groupby("vendedor_nombre", as_index=False)["total_venta"].sum().sort_values("total_venta", ascending=False)
            st.bar_chart(vend.set_index("vendedor_nombre")["total_venta"])
        else:
            st.info("Sin datos.")
    with c2:
        st.subheader("Métodos de pago")
        if not ventas.empty:
            met = ventas.groupby("metodo_pago", as_index=False)["total_venta"].sum().sort_values("total_venta", ascending=False)
            st.bar_chart(met.set_index("metodo_pago")["total_venta"])
        else:
            st.info("Sin datos.")
    with c3:
        st.subheader("Ventas por día")
        if not ventas.empty:
            ventas["dia"] = pd.to_datetime(ventas["fecha"]).dt.date
            diario = ventas.groupby("dia", as_index=False)["total_venta"].sum().sort_values("dia")
            st.line_chart(diario.set_index("dia")["total_venta"])
        else:
            st.info("Sin datos.")

    c1, c2 = st.columns([1.35, 1])
    with c1:
        st.subheader("Ventas recientes")
        if ventas.empty:
            st.info("Todavía no hay ventas en el período.")
        else:
            vtab = ventas.copy()
            vtab["fecha"] = vtab["fecha"].apply(fmt_dt_pe)
            vtab["total"] = vtab["total_venta"].apply(money)
            vtab["saldo"] = vtab["saldo_pendiente"].apply(money)
            html_table(vtab, ["comprobante", "fecha", "cliente", "vendedor_nombre", "metodo_pago", "estado_pago", "total", "saldo"], ["Comprobante", "Fecha", "Cliente", "Vendedor", "Pago", "Estado", "Total", "Saldo"], 12)
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
        dtab = detalle.copy()
        dtab["total"] = dtab["total_vendido"].apply(money)
        dtab["utilidad_fmt"] = dtab["utilidad"].apply(money)
        html_table(dtab, ["vendedor", "producto", "cantidad", "total", "utilidad_fmt"], ["Vendedor", "Producto", "Cantidad", "Total", "Utilidad"], 20)
    else:
        st.info("No hay detalle de productos vendidos.")


def page_ventas():
    st.markdown("<div class='clomar-hero'><h1>🧾 Nueva venta</h1><p>Busca un producto, selecciónalo de la lista y registra el pago.</p></div>", unsafe_allow_html=True)
    if "cart" not in st.session_state:
        st.session_state.cart = []

    productos = productos_con_stock()
    categorias = query_df("SELECT * FROM categorias WHERE estado='Activo' ORDER BY nombre_categoria")
    col_main, col_cart = st.columns([1.55, 1])

    with col_main:
        s1, s2 = st.columns([2, 1])
        with s1:
            buscar = st.text_input("Buscar producto", placeholder="Escribe nombre, código, marca o categoría...")
        with s2:
            cats = ["Todas"] + categorias["nombre_categoria"].tolist() if not categorias.empty else ["Todas"]
            cat = st.selectbox("Categoría", cats)

        fil = productos.copy()
        if buscar.strip():
            txt = buscar.lower().strip()
            fil = fil[
                fil["nombre_producto"].astype(str).str.lower().str.contains(txt, na=False) |
                fil["codigo"].astype(str).str.lower().str.contains(txt, na=False) |
                fil["marca"].astype(str).str.lower().str.contains(txt, na=False) |
                fil["categoria"].astype(str).str.lower().str.contains(txt, na=False)
            ]
        if cat != "Todas":
            fil = fil[fil["categoria"] == cat]

        if fil.empty:
            st.info("No hay productos que coincidan.")
        else:
            fil = fil.head(25).copy()
            opciones = []
            mapa = {}
            for _, r in fil.iterrows():
                stock = float(r["stock_actual"] or 0)
                label = f"{r['codigo']} · {r['nombre_producto']} · {r['categoria']} · Stock {num(stock)} · {money(r['precio_venta'])}"
                opciones.append(label)
                mapa[label] = r
            seleccion = st.selectbox("Producto sugerido", opciones, index=0)
            selected = mapa.get(seleccion)

            if selected is not None:
                stock = float(selected["stock_actual"] or 0)
                chip = "chip-ok" if stock > float(selected["stock_minimo"] or 0) else "chip-red"
                prev1, prev2 = st.columns([1, 1.2])
                with prev1:
                    img_url = str(selected.get("imagen_url", "") or "").strip()
                    if img_url:
                        st.image(img_url, use_container_width=True)
                    else:
                        st.markdown("<div class='card' style='min-height:240px;display:flex;align-items:center;justify-content:center;'>🛍️</div>", unsafe_allow_html=True)
                with prev2:
                    st.markdown(f"""
                    <div class="product-card">
                        <div class="product-name">{selected['nombre_producto']}</div>
                        <div class="product-meta">{selected.get('codigo','')} · {selected.get('categoria','')}</div>
                        <div class="product-price">{money(selected['precio_venta'])}</div>
                        <span class="chip {chip}">Stock {num(stock)}</span>
                        <span class="chip chip-dark">Costo {money(selected['costo_unitario'])}</span>
                    </div>
                    """, unsafe_allow_html=True)
                cantidad = st.number_input("Cantidad", min_value=1.0, max_value=max(stock, 1.0), step=1.0, value=1.0)
                precio = st.number_input("Precio unitario", min_value=0.0, value=float(selected["precio_venta"] or 0), step=1.0)
                if st.button("Agregar al carrito", type="primary", use_container_width=True, disabled=stock <= 0):
                    item = {
                        "id_producto": int(selected["id_producto"]), "nombre": selected["nombre_producto"],
                        "cantidad": float(cantidad), "precio": float(precio),
                        "costo": float(selected["costo_unitario"] or 0), "stock": stock
                    }
                    found = False
                    for c in st.session_state.cart:
                        if c["id_producto"] == item["id_producto"]:
                            c["cantidad"] = min(float(c["cantidad"]) + float(cantidad), stock)
                            c["precio"] = float(precio)
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
                    venta_num = int(scalar("SELECT COALESCE(COUNT(*),0)+1 FROM ventas"))
                    comprobante = f"B{venta_num:04d}"
                    estado_pago = "Pagada" if saldo <= 0 else ("Parcial" if monto_pagado > 0 else "Pendiente")
                    try:
                        with ENGINE.begin() as conn:
                            conn.execute(text("""
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


def page_productos():
    st.markdown("<div class='clomar-hero'><h1>📦 Productos</h1><p>Catálogo visual, costos, precios y stock mínimo.</p></div>", unsafe_allow_html=True)
    productos = productos_con_stock()
    categorias = query_df("SELECT * FROM categorias WHERE estado='Activo' ORDER BY nombre_categoria")
    tab1, tab2 = st.tabs(["Catálogo", "Crear / editar"])
    with tab1:
        buscar = st.text_input("Buscar", key="buscar_productos")
        fil = productos.copy()
        if buscar.strip():
            txt = buscar.lower()
            fil = fil[fil["nombre_producto"].astype(str).str.lower().str.contains(txt, na=False) | fil["codigo"].astype(str).str.lower().str.contains(txt, na=False)]
        if fil.empty:
            st.info("No hay productos.")
        else:
            cols = st.columns(5)
            for i, (_, r) in enumerate(fil.head(50).iterrows()):
                with cols[i % 5]:
                    stock = float(r["stock_actual"] or 0)
                    chip = "chip-ok" if stock > float(r["stock_minimo"] or 0) else "chip-red"
                    img = str(r.get("imagen_url", "") or "").strip()
                    if img:
                        st.image(img, use_container_width=True)
                    st.markdown(f"""
                    <div class="product-card" style="padding:12px;">
                        <div class="product-name" style="min-height:36px;font-size:15px;">{r['nombre_producto']}</div>
                        <div class="product-meta">{r.get('codigo','')} · {r.get('categoria','')}</div>
                        <div class="product-price" style="font-size:20px;">{money(r['precio_venta'])}</div>
                        <span class="chip {chip}">Stock {num(stock)}</span>
                    </div>
                    """, unsafe_allow_html=True)
                    if is_admin():
                        st.caption(f"Costo {money(r['costo_unitario'])}")
    with tab2:
        with st.form("form_producto"):
            c1, c2, c3 = st.columns(3)
            with c1:
                codigo = st.text_input("Código")
                nombre = st.text_input("Nombre")
                categoria = st.selectbox("Categoría", ["Sin categoría"] + (categorias["nombre_categoria"].tolist() if not categorias.empty else []))
            with c2:
                marca = st.text_input("Marca")
                unidad = st.text_input("Unidad", value="Unidad")
                stock_minimo = st.number_input("Stock mínimo", min_value=0.0, step=1.0)
            with c3:
                costo = st.number_input("Costo", min_value=0.0, step=1.0)
                precio = st.number_input("Precio", min_value=0.0, step=1.0)
                imagen_url = st.text_input("Imagen URL", placeholder="URL directa de imagen (opcional)")
            if st.form_submit_button("Guardar producto", type="primary", use_container_width=True):
                if not nombre.strip():
                    st.error("Ingresa el nombre del producto.")
                else:
                    id_cat = None
                    if categoria != "Sin categoría":
                        row = query_df("SELECT id_categoria FROM categorias WHERE nombre_categoria=:n", {"n": categoria})
                        if row.empty:
                            exec_sql("INSERT INTO categorias (nombre_categoria, estado) VALUES (:n,'Activo')", {"n": categoria})
                            row = query_df("SELECT id_categoria FROM categorias WHERE nombre_categoria=:n", {"n": categoria})
                        id_cat = int(row.iloc[0]["id_categoria"])
                    existing = query_df("SELECT id_producto FROM productos WHERE codigo=:c", {"c": codigo.strip()}) if codigo.strip() else pd.DataFrame()
                    try:
                        if not existing.empty:
                            exec_sql("""
                                UPDATE productos
                                SET nombre_producto=:n, id_categoria=:cat, marca=:m, unidad=:u, costo_unitario=:co, precio_venta=:pr, stock_minimo=:sm, imagen_url=:img, actualizado_en=CURRENT_TIMESTAMP
                                WHERE id_producto=:id
                            """, {"n": nombre.strip(), "cat": id_cat, "m": marca, "u": unidad, "co": costo, "pr": precio, "sm": stock_minimo, "img": imagen_url, "id": int(existing.iloc[0]["id_producto"])})
                            st.success("Producto actualizado.")
                        else:
                            exec_sql("""
                                INSERT INTO productos (codigo, nombre_producto, id_categoria, marca, unidad, costo_unitario, precio_venta, stock_minimo, imagen_url, estado)
                                VALUES (:c,:n,:cat,:m,:u,:co,:pr,:sm,:img,'Activo')
                            """, {"c": codigo.strip() or None, "n": nombre.strip(), "cat": id_cat, "m": marca, "u": unidad, "co": costo, "pr": precio, "sm": stock_minimo, "img": imagen_url})
                            st.success("Producto creado.")
                        st.rerun()
                    except Exception as e:
                        st.error("No se pudo guardar el producto.")
                        st.exception(e)


def page_inventario():
    st.markdown("<div class='clomar-hero'><h1>📊 Inventario</h1><p>Stock actual, categorías, valorización y movimientos recientes.</p></div>", unsafe_allow_html=True)
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

    categorias = ["Todas"] + sorted([x for x in productos["categoria"].dropna().astype(str).unique().tolist() if x])
    tabs = st.tabs(categorias)
    for tab, cat in zip(tabs, categorias):
        with tab:
            fil = productos if cat == "Todas" else productos[productos["categoria"] == cat]
            st.caption(f"{len(fil)} productos")
            if not fil.empty:
                tab_df = fil.copy()
                tab_df["precio"] = tab_df["precio_venta"].apply(money)
                tab_df["costo"] = tab_df["costo_unitario"].apply(money)
                cols = ["codigo", "nombre_producto", "stock_actual", "stock_minimo", "precio"]
                heads = ["Código", "Producto", "Stock", "Mínimo", "Precio"]
                if is_admin():
                    cols.insert(-1, "costo"); heads.insert(-1, "Costo")
                html_table(tab_df, cols, heads, 200)

    st.subheader("Últimos movimientos de stock")
    d1, d2 = st.columns(2)
    with d1:
        mov_desde = st.date_input("Desde", date.today()-timedelta(days=7), key="mov_desde")
    with d2:
        mov_hasta = st.date_input("Hasta", date.today(), key="mov_hasta")
    movimientos = query_df(
        """SELECT ms.fecha, p.codigo, p.nombre_producto AS producto, ms.tipo, ms.cantidad, ms.costo_unitario, ms.referencia, ms.observacion
           FROM movimientos_stock ms
           LEFT JOIN productos p ON p.id_producto = ms.id_producto
           WHERE date(ms.fecha) BETWEEN :d AND :h
           ORDER BY ms.fecha DESC
        """,
        {"d": str(mov_desde), "h": str(mov_hasta)},
    )
    if movimientos.empty:
        st.info("No hay movimientos en ese rango.")
    else:
        movimientos = movimientos.head(100).copy()
        movimientos["fecha"] = movimientos["fecha"].apply(fmt_dt_pe)
        movimientos["costo"] = movimientos["costo_unitario"].apply(money)
        html_table(movimientos, ["fecha", "codigo", "producto", "tipo", "cantidad", "costo", "referencia"], ["Fecha", "Código", "Producto", "Tipo", "Cant.", "Costo", "Ref"], 100)


def page_ingreso_mercaderia():
    st.markdown("<div class='clomar-hero'><h1>📥 Ingreso de mercadería</h1><p>Registra compras, aumenta stock y controla deudas a proveedores.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede ingresar mercadería.")
        return
    productos = productos_con_stock()
    if productos.empty:
        st.info("Crea productos antes de registrar mercadería.")
        return
    with st.form("form_compra"):
        c1, c2 = st.columns(2)
        with c1:
            proveedor = st.text_input("Proveedor", placeholder="Nombre del proveedor")
            producto_label = st.selectbox("Producto", [f"{r['id_producto']} - {r['nombre_producto']} | Stock {num(r['stock_actual'])}" for _, r in productos.iterrows()])
            idp = int(producto_label.split(" - ")[0])
            cantidad = st.number_input("Cantidad", min_value=0.0, step=1.0)
            costo = st.number_input("Costo unitario", min_value=0.0, step=1.0)
        with c2:
            metodo = st.selectbox("Método de pago", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Crédito"])
            total = cantidad * costo
            pagado_default = float(total if metodo != "Crédito" else 0)
            pagado = st.number_input("Monto pagado", min_value=0.0, value=pagado_default, step=1.0)
            obs = st.text_area("Observación")
            saldo = max(total - pagado, 0)
            estado = "Pagada" if saldo <= 0 else ("Parcial" if pagado > 0 else "Pendiente")
            st.markdown(f"### Total compra: {money(total)} · Saldo: {money(saldo)}")
            st.caption(f"Estado: {estado}")
        if st.form_submit_button("Registrar ingreso", type="primary", use_container_width=True):
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


def page_creditos():
    st.markdown("<div class='clomar-hero'><h1>💳 Créditos</h1><p>Control por cliente, por comprobante y abonos parciales.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede ver créditos.")
        return

    ventas = query_df("""
        SELECT v.id_venta, v.comprobante, v.fecha, v.total_venta, v.monto_pagado, v.saldo_pendiente, v.estado_pago,
               v.metodo_pago, v.vendedor_nombre, COALESCE(c.nombre_cliente,'Cliente general') AS cliente,
               COALESCE(c.telefono,'') AS telefono
        FROM ventas v
        LEFT JOIN clientes c ON c.id_cliente = v.id_cliente
        WHERE COALESCE(v.anulada,0)=0 AND COALESCE(v.saldo_pendiente,0) > 0
        ORDER BY v.fecha DESC
    """)
    clientes = query_df("SELECT id_cliente, nombre_cliente FROM clientes ORDER BY nombre_cliente")

    c1, c2, c3 = st.columns(3)
    with c1: kpi("Créditos pendientes", money(ventas['saldo_pendiente'].sum() if not ventas.empty else 0), f"{len(ventas)} comprobantes")
    with c2: kpi("Clientes con deuda", str(ventas['cliente'].nunique() if not ventas.empty else 0), "Resumen general")
    with c3: kpi("Ventas a crédito", money(ventas['total_venta'].sum() if not ventas.empty else 0), "Total vendido pendiente")

    st.subheader("Pendientes por cliente")
    cliente_filtro = st.selectbox("Filtrar cliente", ["Todos"] + (clientes['nombre_cliente'].tolist() if not clientes.empty else []))
    credito_filtro = ventas.copy()
    if cliente_filtro != "Todos":
        credito_filtro = credito_filtro[credito_filtro['cliente'] == cliente_filtro]
    if credito_filtro.empty:
        st.info("No hay créditos pendientes con ese filtro.")
    else:
        credito_filtro['fecha'] = credito_filtro['fecha'].apply(fmt_dt_pe)
        credito_filtro['total'] = credito_filtro['total_venta'].apply(money)
        credito_filtro['pagado'] = credito_filtro['monto_pagado'].apply(money)
        credito_filtro['saldo'] = credito_filtro['saldo_pendiente'].apply(money)
        html_table(credito_filtro, ['comprobante','fecha','cliente','telefono','vendedor_nombre','metodo_pago','total','pagado','saldo','estado_pago'], ['Comprobante','Fecha','Cliente','Teléfono','Vendedor','Pago','Total','Pagado','Saldo','Estado'], 100)

    st.subheader("Registrar abono")
    if ventas.empty:
        st.info("No hay saldos pendientes.")
        return
    with st.form("form_abono_credito"):
        opc = [f"{r['comprobante']} · {r['cliente']} · Saldo {money(r['saldo_pendiente'])}" for _, r in ventas.iterrows()]
        sel = st.selectbox("Venta pendiente", opc)
        vrow = ventas.iloc[opc.index(sel)]
        monto = st.number_input("Monto a abonar", min_value=0.0, step=1.0, max_value=float(vrow['saldo_pendiente']))
        metodo = st.selectbox("Método de abono", ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta"])
        obs = st.text_area("Observación")
        st.markdown(f"**Saldo actual:** {money(vrow['saldo_pendiente'])}")
        if st.form_submit_button("Registrar abono", type="primary", use_container_width=True):
            if monto <= 0:
                st.error("El monto debe ser mayor a cero.")
            else:
                nuevo_pagado = float(vrow['monto_pagado']) + float(monto)
                nuevo_saldo = max(float(vrow['total_venta']) - nuevo_pagado, 0)
                nuevo_estado = "Pagada" if nuevo_saldo <= 0 else "Parcial"
                with ENGINE.begin() as conn:
                    conn.execute(text("""
                        UPDATE ventas SET monto_pagado=:pag, saldo_pendiente=:saldo, estado_pago=:estado
                        WHERE id_venta=:idv
                    """), {"pag": nuevo_pagado, "saldo": nuevo_saldo, "estado": nuevo_estado, "idv": int(vrow['id_venta'])})
                    u = current_user()
                    conn.execute(text("""
                        INSERT INTO caja (tipo, concepto, metodo_pago, monto, referencia, id_usuario, observacion)
                        VALUES ('Ingreso', :concepto, :metodo, :monto, :ref, :uid, :obs)
                    """), {"concepto": f"Abono crédito {vrow['comprobante']}", "metodo": metodo, "monto": monto, "ref": vrow['comprobante'], "uid": u['id_usuario'], "obs": obs})
                st.success("Abono registrado.")
                st.rerun()

def page_caja():
    st.markdown("<div class='clomar-hero'><h1>💰 Caja</h1><p>Movimientos, ingresos, egresos y cierre del día.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede ver caja.")
        return
    f = st.date_input("Fecha", date.today())
    tipo_f = st.selectbox("Tipo", ["Todos", "Ingreso", "Egreso"], index=0)
    metodo_f = st.selectbox("Método", ["Todos", "Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Crédito"], index=0)
    caja = query_df("SELECT * FROM caja WHERE date(fecha)=:f AND COALESCE(anulada,0)=0 ORDER BY fecha DESC", {"f": str(f)})
    if caja.empty:
        st.info("No hay movimientos en la fecha.")
        ingresos = egresos = 0
    else:
        if tipo_f != "Todos":
            caja = caja[caja["tipo"].astype(str).str.lower().eq(tipo_f.lower())]
        if metodo_f != "Todos":
            caja = caja[caja["metodo_pago"].astype(str).str.lower().eq(metodo_f.lower())]
        ingresos = caja[caja["tipo"].astype(str).str.lower().eq("ingreso")]["monto"].sum() if not caja.empty else 0
        egresos = caja[caja["tipo"].astype(str).str.lower().eq("egreso")]["monto"].sum() if not caja.empty else 0

    a,b,c,d = st.columns(4)
    with a: kpi("Ingresos", money(ingresos))
    with b: kpi("Egresos", money(egresos))
    with c: kpi("Saldo neto", money(ingresos-egresos))
    with d: kpi("Movimientos", str(len(caja) if not caja.empty else 0))

    if not caja.empty:
        resumen_met = caja.groupby("metodo_pago", as_index=False)["monto"].sum().sort_values("monto", ascending=False)
        c1, c2 = st.columns([1, 1.2])
        with c1:
            st.subheader("Resumen por método")
            st.bar_chart(resumen_met.set_index("metodo_pago")["monto"])
        with c2:
            st.subheader("Movimientos del día")
            ctab = caja.copy()
            ctab["fecha"] = ctab["fecha"].apply(fmt_dt_pe)
            ctab["monto_fmt"] = ctab["monto"].apply(money)
            html_table(ctab, ["fecha", "tipo", "concepto", "metodo_pago", "monto_fmt", "referencia"], ["Fecha", "Tipo", "Concepto", "Pago", "Monto", "Ref"], 200)
    with st.expander("Cierre diario"):
        with st.form("form_cierre"):
            m_inicial = st.number_input("Monto inicial", min_value=0.0, step=1.0, value=0.0)
            contado = st.number_input("Efectivo contado físico", min_value=0.0, step=1.0, value=0.0)
            obs = st.text_area("Observación cierre")
            efectivo_esperado = max(float(ingresos) - float(egresos) + float(m_inicial), 0)
            diferencia = float(contado) - efectivo_esperado
            st.markdown(f"**Efectivo esperado:** {money(efectivo_esperado)} · **Diferencia:** {money(diferencia)}")
            if st.form_submit_button("Guardar cierre diario", type="primary"):
                u = current_user()
                exec_sql("""
                    INSERT INTO cierres_caja (fecha, hora, id_usuario, monto_inicial, efectivo_esperado, efectivo_contado, diferencia, observacion)
                    VALUES (:f, :h, :u, :mi, :ee, :ec, :dif, :obs)
                """, {"f": str(f), "h": now_peru().strftime("%I:%M %p"), "u": u["id_usuario"], "mi": m_inicial, "ee": efectivo_esperado, "ec": contado, "dif": diferencia, "obs": obs})
                st.success("Cierre diario guardado.")
                st.rerun()
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
    st.markdown("<div class='clomar-hero'><h1>📈 Reportes</h1><p>Ventas por vendedor, métodos de pago, días y comprobantes.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede ver reportes.")
        return
    c1,c2 = st.columns(2)
    with c1: desde=st.date_input("Desde", date.today()-timedelta(days=7), key="rep_d")
    with c2: hasta=st.date_input("Hasta", date.today(), key="rep_h")
    ventas=ventas_periodo(desde,hasta)
    detalle=detalle_productos_vendidos(desde,hasta)
    if ventas.empty:
        st.info("No hay ventas en el rango.")
        return
    vendedor_f = st.selectbox("Vendedor", ["Todos"] + sorted(ventas["vendedor_nombre"].fillna("Sin vendedor").astype(str).unique().tolist()))
    metodo_f = st.selectbox("Método de pago", ["Todos"] + sorted(ventas["metodo_pago"].fillna("Sin método").astype(str).unique().tolist()))
    estado_f = st.selectbox("Estado", ["Todos", "Pagada", "Parcial", "Pendiente", "Anulada"])
    cliente_f = st.text_input("Cliente / comprobante", placeholder="Busca por cliente o comprobante")

    fil = ventas.copy()
    if vendedor_f != "Todos":
        fil = fil[fil["vendedor_nombre"].astype(str) == vendedor_f]
    if metodo_f != "Todos":
        fil = fil[fil["metodo_pago"].astype(str) == metodo_f]
    if estado_f != "Todos":
        fil = fil[fil["estado_pago"].astype(str) == estado_f]
    if cliente_f.strip():
        q = cliente_f.lower().strip()
        fil = fil[fil["cliente"].astype(str).str.lower().str.contains(q, na=False) | fil["comprobante"].astype(str).str.lower().str.contains(q, na=False)]

    total_ventas = fil["total_venta"].sum() if not fil.empty else 0
    total_pagado = fil["monto_pagado"].sum() if not fil.empty else 0
    total_saldo = fil["saldo_pendiente"].sum() if not fil.empty else 0
    utilidad = detalle["utilidad"].sum() if not detalle.empty else 0

    a,b,c,d = st.columns(4)
    with a: kpi("Ventas", money(total_ventas), f"{len(fil)} comprobantes")
    with b: kpi("Cobrado", money(total_pagado), "Ingresos confirmados")
    with c: kpi("Crédito pendiente", money(total_saldo), "Saldo por cobrar")
    with d: kpi("Utilidad estimada", money(utilidad), "Según costo registrado")

    if not fil.empty:
        fil["dia"] = pd.to_datetime(fil["fecha"]).dt.date
        v1, v2, v3 = st.columns(3)
        with v1:
            st.subheader("Ventas por vendedor")
            vend = fil.groupby("vendedor_nombre", as_index=False)["total_venta"].sum().sort_values("total_venta", ascending=False)
            st.bar_chart(vend.set_index("vendedor_nombre")["total_venta"])
        with v2:
            st.subheader("Métodos de pago")
            met = fil.groupby("metodo_pago", as_index=False)["total_venta"].sum().sort_values("total_venta", ascending=False)
            st.bar_chart(met.set_index("metodo_pago")["total_venta"])
        with v3:
            st.subheader("Ventas por día")
            diario = fil.groupby("dia", as_index=False)["total_venta"].sum().sort_values("dia")
            st.line_chart(diario.set_index("dia")["total_venta"])

        st.subheader("Detalle de comprobantes")
        fil = fil.copy()
        fil["fecha"] = fil["fecha"].apply(fmt_dt_pe)
        fil["total"] = fil["total_venta"].apply(money)
        fil["pagado"] = fil["monto_pagado"].apply(money)
        fil["saldo"] = fil["saldo_pendiente"].apply(money)
        html_table(fil, ["comprobante", "fecha", "cliente", "vendedor_nombre", "metodo_pago", "estado_pago", "total", "pagado", "saldo"], ["Comprobante", "Fecha", "Cliente", "Vendedor", "Pago", "Estado", "Total", "Pagado", "Saldo"], 100)

        st.subheader("Detalle de productos vendidos")
        detalle_fil = detalle.copy()
        if not detalle_fil.empty:
            detalle_fil["total"] = detalle_fil["total_vendido"].apply(money)
            detalle_fil["utilidad_fmt"] = detalle_fil["utilidad"].apply(money)
            html_table(detalle_fil, ["vendedor","producto","cantidad","total","utilidad_fmt"], ["Vendedor","Producto","Cantidad","Total","Utilidad"], 100)


def page_usuarios():
    st.markdown("<div class='clomar-hero'><h1>🔐 Usuarios</h1><p>Control de accesos: dueño, administrador y vendedor.</p></div>", unsafe_allow_html=True)
    if not is_admin():
        st.warning("Solo administrador puede gestionar usuarios.")
        return
    users=query_df("SELECT id_usuario, usuario, nombre, rol, estado, creado_en FROM usuarios ORDER BY id_usuario")
    html_table(users, ["usuario","nombre","rol","estado","creado_en"], ["Usuario","Nombre","Rol","Estado","Creado"], 100)
    with st.expander("Crear usuario"):
        with st.form("form_user"):
            usuario=st.text_input("Usuario")
            nombre=st.text_input("Nombre")
            clave=st.text_input("Contraseña", type="password")
            rol=st.selectbox("Rol", ["Vendedor", "Administrador", "Supervisor"])
            if st.form_submit_button("Crear usuario", type="primary"):
                if not usuario or not clave:
                    st.error("Usuario y contraseña son obligatorios.")
                else:
                    try:
                        exec_sql("INSERT INTO usuarios (usuario,password_hash,nombre,rol,estado) VALUES (:u,:p,:n,:r,'Activo')", {"u":usuario,"p":hash_password(clave),"n":nombre or usuario,"r":rol})
                        st.success("Usuario creado.")
                        st.rerun()
                    except Exception as e:
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


def page_configuracion():
    st.markdown("<div class='clomar-hero'><h1>☁️ Estado nube</h1><p>Verifica si la app trabaja local o conectada a PostgreSQL Neon.</p></div>", unsafe_allow_html=True)
    if IS_POSTGRES:
        st.success("Modo nube PostgreSQL activo.")
    else:
        st.warning("Modo local SQLite activo. En Streamlit Cloud debes configurar NEON_DATABASE_URL en Secrets.")
    st.code("NEON_DATABASE_URL = postgresql://...?...sslmode=require", language="toml")
    st.write("Versión:", APP_VERSION)


# ============================================================
# NAVEGACIÓN
# ============================================================
def sidebar_nav():
    u = current_user()
    st.sidebar.markdown(f"### 🛍️ {APP_NAME}")
    st.sidebar.caption(f"{u['nombre']} · {u['rol']}")
    st.sidebar.markdown("**Gestionar tu negocio**")
    if is_admin():
        opciones = [
            "📊 Panel dueño", "🧾 Ventas", "💳 Créditos", "💰 Caja", "📈 Reportes",
            "📦 Productos", "📊 Inventario", "📥 Ingreso mercadería",
            "👥 Clientes", "🔐 Usuarios", "💾 Backup", "☁️ Estado nube"
        ]
    else:
        opciones = ["🧾 Ventas", "💳 Créditos", "👥 Clientes", "📦 Productos"]
    selected = st.sidebar.radio("Menú", opciones, label_visibility="collapsed")
    st.sidebar.divider()
    if st.sidebar.button("Cerrar sesión", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    return selected


if "user" not in st.session_state:
    login_screen()
else:
    selected = sidebar_nav()
    if selected == "📊 Panel dueño": page_panel_dueno()
    elif selected == "🧾 Ventas": page_ventas()
    elif selected == "💳 Créditos": page_creditos()
    elif selected == "💰 Caja": page_caja()
    elif selected == "📈 Reportes": page_reportes()
    elif selected == "📦 Productos": page_productos()
    elif selected == "📊 Inventario": page_inventario()
    elif selected == "📥 Ingreso mercadería": page_ingreso_mercaderia()
    elif selected == "👥 Clientes": page_clientes()
    elif selected == "🔐 Usuarios": page_usuarios()
    elif selected == "💾 Backup": page_backup()
    elif selected == "☁️ Estado nube": page_configuracion()
