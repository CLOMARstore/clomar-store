"""
Migrador Clomar Store V24: SQLite local -> Neon PostgreSQL.

Uso recomendado en CMD:
  cd /d C:\PythonCivil
  civil_env\Scripts\activate
  set NEON_DATABASE_URL=postgresql://USUARIO:CLAVE@HOST/neondb?sslmode=require
  python migrar_sqlite_a_neon.py --sqlite clomar_store.db

No subas este archivo con tu cadena de conexión escrita dentro.
"""
import argparse
import os
import sqlite3
import hashlib
import secrets
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt.encode(), 120_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def engine(url):
    return create_engine(url, pool_pre_ping=True)


def exec_sql(pg, sql, params=None):
    with pg.begin() as conn:
        return conn.execute(text(sql), params or {})


def create_schema(pg):
    id_sql = "BIGSERIAL PRIMARY KEY"
    ddl = [
        f"CREATE TABLE IF NOT EXISTS usuarios (id_usuario {id_sql}, usuario VARCHAR(80) UNIQUE NOT NULL, password_hash TEXT NOT NULL, nombre VARCHAR(160) NOT NULL, rol VARCHAR(40) NOT NULL DEFAULT 'Vendedor', estado VARCHAR(30) NOT NULL DEFAULT 'Activo', creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        f"CREATE TABLE IF NOT EXISTS categorias (id_categoria {id_sql}, nombre_categoria VARCHAR(160) UNIQUE NOT NULL, descripcion TEXT DEFAULT '', estado VARCHAR(30) DEFAULT 'Activo', creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        f"CREATE TABLE IF NOT EXISTS clientes (id_cliente {id_sql}, nombre_cliente VARCHAR(200) NOT NULL, telefono VARCHAR(80) DEFAULT '', documento VARCHAR(80) DEFAULT '', direccion TEXT DEFAULT '', observacion TEXT DEFAULT '', limite_credito NUMERIC(12,2) DEFAULT 0, estado VARCHAR(30) DEFAULT 'Activo', creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        f"CREATE TABLE IF NOT EXISTS proveedores (id_proveedor {id_sql}, nombre_proveedor VARCHAR(200) NOT NULL, telefono VARCHAR(80) DEFAULT '', documento VARCHAR(80) DEFAULT '', direccion TEXT DEFAULT '', estado VARCHAR(30) DEFAULT 'Activo', creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        f"CREATE TABLE IF NOT EXISTS productos (id_producto {id_sql}, codigo VARCHAR(80) UNIQUE, nombre_producto VARCHAR(240) NOT NULL, id_categoria INTEGER, marca VARCHAR(120) DEFAULT '', unidad VARCHAR(60) DEFAULT 'Unidad', costo_unitario NUMERIC(12,2) DEFAULT 0, precio_venta NUMERIC(12,2) DEFAULT 0, stock_minimo NUMERIC(12,2) DEFAULT 0, imagen_url TEXT DEFAULT '', estado VARCHAR(30) DEFAULT 'Activo', creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP, actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        f"CREATE TABLE IF NOT EXISTS ventas (id_venta {id_sql}, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, comprobante VARCHAR(80) UNIQUE, id_cliente INTEGER, id_usuario INTEGER, vendedor_nombre VARCHAR(160), metodo_pago VARCHAR(60), total_venta NUMERIC(12,2) DEFAULT 0, monto_pagado NUMERIC(12,2) DEFAULT 0, saldo_pendiente NUMERIC(12,2) DEFAULT 0, estado_pago VARCHAR(30) DEFAULT 'Pagada', observacion TEXT DEFAULT '', anulada INTEGER DEFAULT 0)",
        f"CREATE TABLE IF NOT EXISTS detalle_ventas (id_detalle {id_sql}, id_venta INTEGER NOT NULL, id_producto INTEGER, producto_nombre VARCHAR(240), cantidad NUMERIC(12,2) DEFAULT 0, precio_unitario NUMERIC(12,2) DEFAULT 0, costo_unitario NUMERIC(12,2) DEFAULT 0, subtotal NUMERIC(12,2) DEFAULT 0)",
        f"CREATE TABLE IF NOT EXISTS movimientos_stock (id_movimiento {id_sql}, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, id_producto INTEGER NOT NULL, tipo VARCHAR(50) NOT NULL, cantidad NUMERIC(12,2) DEFAULT 0, costo_unitario NUMERIC(12,2) DEFAULT 0, referencia VARCHAR(120) DEFAULT '', id_usuario INTEGER, observacion TEXT DEFAULT '')",
        f"CREATE TABLE IF NOT EXISTS compras (id_compra {id_sql}, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, proveedor VARCHAR(200) DEFAULT '', metodo_pago VARCHAR(60) DEFAULT '', total_compra NUMERIC(12,2) DEFAULT 0, monto_pagado NUMERIC(12,2) DEFAULT 0, saldo_pendiente NUMERIC(12,2) DEFAULT 0, estado_pago VARCHAR(30) DEFAULT 'Pagada', id_usuario INTEGER, observacion TEXT DEFAULT '')",
        f"CREATE TABLE IF NOT EXISTS detalle_compras (id_detalle_compra {id_sql}, id_compra INTEGER NOT NULL, id_producto INTEGER, cantidad NUMERIC(12,2) DEFAULT 0, costo_unitario NUMERIC(12,2) DEFAULT 0, subtotal NUMERIC(12,2) DEFAULT 0)",
        f"CREATE TABLE IF NOT EXISTS caja (id_caja {id_sql}, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, tipo VARCHAR(30) NOT NULL, concepto TEXT NOT NULL, metodo_pago VARCHAR(60) DEFAULT '', monto NUMERIC(12,2) DEFAULT 0, referencia VARCHAR(120) DEFAULT '', id_usuario INTEGER, observacion TEXT DEFAULT '', anulada INTEGER DEFAULT 0)",
        f"CREATE TABLE IF NOT EXISTS egresos (id_egreso {id_sql}, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, categoria_gasto VARCHAR(120), concepto TEXT, proveedor VARCHAR(200) DEFAULT '', monto NUMERIC(12,2) DEFAULT 0, metodo_pago VARCHAR(60) DEFAULT '', estado_pago VARCHAR(30) DEFAULT 'Pagada', monto_pagado NUMERIC(12,2) DEFAULT 0, saldo_pendiente NUMERIC(12,2) DEFAULT 0, id_usuario INTEGER, observacion TEXT DEFAULT '')",
    ]
    for sql in ddl:
        exec_sql(pg, sql)


def read_table(sqlite_path, table):
    con = sqlite3.connect(sqlite_path)
    try:
        exists = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", con, params=(table,))
        if exists.empty:
            return pd.DataFrame()
        return pd.read_sql_query(f"SELECT * FROM {table}", con)
    finally:
        con.close()


def val(row, name, default=None):
    return row[name] if name in row.index and pd.notna(row[name]) else default


def insert_defaults(pg):
    for usuario, clave, nombre, rol in [("admin","admin123","Administrador","Administrador"),("vendedor","venta123","Vendedor tienda","Vendedor")]:
        exists = exec_sql(pg, "SELECT COUNT(*) FROM usuarios WHERE usuario=:u", {"u":usuario}).scalar()
        if int(exists or 0) == 0:
            exec_sql(pg, "INSERT INTO usuarios (usuario,password_hash,nombre,rol,estado) VALUES (:u,:p,:n,:r,'Activo')", {"u":usuario,"p":hash_password(clave),"n":nombre,"r":rol})


def migrate(sqlite_path, url):
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"No existe {sqlite_path}")
    pg = engine(url)
    create_schema(pg)

    print("Migrando usuarios...")
    usuarios = read_table(sqlite_path, "usuarios")
    for _, r in usuarios.iterrows():
        usuario = str(val(r, "usuario", "")).strip()
        if not usuario:
            continue
        ph = val(r, "password_hash", None) or val(r, "clave", None) or hash_password("cambiar123")
        exec_sql(pg, """
            INSERT INTO usuarios (id_usuario, usuario, password_hash, nombre, rol, estado)
            VALUES (:id,:u,:p,:n,:r,:e)
            ON CONFLICT (usuario) DO NOTHING
        """, {"id": int(val(r,"id_usuario",0) or 0) or None, "u": usuario, "p": str(ph), "n": str(val(r,"nombre",usuario)), "r": str(val(r,"rol","Vendedor")), "e": str(val(r,"estado","Activo"))})
    insert_defaults(pg)

    print("Migrando categorías...")
    categorias = read_table(sqlite_path, "categorias")
    for _, r in categorias.iterrows():
        nombre = str(val(r, "nombre_categoria", "")).strip()
        if nombre:
            exec_sql(pg, "INSERT INTO categorias (id_categoria,nombre_categoria,descripcion,estado) VALUES (:id,:n,:d,'Activo') ON CONFLICT (nombre_categoria) DO NOTHING", {"id": int(val(r,"id_categoria",0) or 0) or None, "n": nombre, "d": str(val(r,"descripcion",""))})

    print("Migrando clientes...")
    clientes = read_table(sqlite_path, "clientes")
    for _, r in clientes.iterrows():
        nombre = str(val(r,"nombre_cliente","")).strip()
        if nombre:
            exec_sql(pg, """
                INSERT INTO clientes (id_cliente,nombre_cliente,telefono,documento,direccion,observacion,limite_credito,estado)
                VALUES (:id,:n,:t,:doc,:dir,:obs,:lim,:estado) ON CONFLICT (id_cliente) DO NOTHING
            """, {"id": int(val(r,"id_cliente",0) or 0) or None, "n":nombre, "t":str(val(r,"telefono","")), "doc":str(val(r,"documento","")), "dir":str(val(r,"direccion","")), "obs":str(val(r,"observacion","")), "lim":float(val(r,"limite_credito",0) or 0), "estado":str(val(r,"estado","Activo"))})

    print("Migrando proveedores...")
    proveedores = read_table(sqlite_path, "proveedores")
    for _, r in proveedores.iterrows():
        nombre = str(val(r,"nombre_proveedor","")).strip()
        if nombre:
            exec_sql(pg, "INSERT INTO proveedores (id_proveedor,nombre_proveedor,telefono,documento,direccion,estado) VALUES (:id,:n,:t,:doc,:dir,:estado) ON CONFLICT (id_proveedor) DO NOTHING", {"id": int(val(r,"id_proveedor",0) or 0) or None, "n":nombre,"t":str(val(r,"telefono","")),"doc":str(val(r,"documento","")),"dir":str(val(r,"direccion","")),"estado":str(val(r,"estado","Activo"))})

    print("Migrando productos y stock inicial...")
    productos = read_table(sqlite_path, "productos")
    for _, r in productos.iterrows():
        nombre = str(val(r,"nombre_producto","")).strip()
        if not nombre:
            continue
        idp = int(val(r,"id_producto",0) or 0) or None
        codigo = str(val(r,"codigo",f"P{idp}")) if idp else str(val(r,"codigo",""))
        exec_sql(pg, """
            INSERT INTO productos (id_producto,codigo,nombre_producto,id_categoria,marca,unidad,costo_unitario,precio_venta,stock_minimo,estado)
            VALUES (:id,:codigo,:nombre,:cat,:marca,:unidad,:costo,:precio,:min,:estado)
            ON CONFLICT (id_producto) DO NOTHING
        """, {"id":idp,"codigo":codigo,"nombre":nombre,"cat":val(r,"id_categoria",None),"marca":str(val(r,"marca","")),"unidad":str(val(r,"unidad","Unidad")),"costo":float(val(r,"costo_unitario",0) or 0),"precio":float(val(r,"precio_venta",0) or 0),"min":float(val(r,"stock_minimo",0) or 0),"estado":str(val(r,"estado","Activo"))})
        stock_ini = float(val(r,"stock_inicial",0) or 0)
        if idp and stock_ini:
            exec_sql(pg, "INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,observacion) VALUES (:idp,'MIGRACION_INICIAL',:cant,:costo,'SQLite','Stock inicial migrado')", {"idp":idp,"cant":stock_ini,"costo":float(val(r,"costo_unitario",0) or 0)})

    print("Migrando ventas y detalles...")
    ventas = read_table(sqlite_path, "ventas")
    for _, r in ventas.iterrows():
        idv = int(val(r,"id_venta",0) or 0) or None
        comprobante = str(val(r,"comprobante",f"MIG-{idv}"))
        vendedor = str(val(r,"vendedor_nombre", val(r,"usuario","Migrado")))
        total = float(val(r,"total_venta", val(r,"total",0)) or 0)
        pagado = float(val(r,"monto_pagado", total) or 0)
        saldo = float(val(r,"saldo_pendiente", max(total-pagado,0)) or 0)
        exec_sql(pg, """
            INSERT INTO ventas (id_venta,fecha,comprobante,id_cliente,vendedor_nombre,metodo_pago,total_venta,monto_pagado,saldo_pendiente,estado_pago,observacion,anulada)
            VALUES (:id,:fecha,:comp,:cli,:vend,:metodo,:total,:pagado,:saldo,:estado,:obs,0)
            ON CONFLICT (id_venta) DO NOTHING
        """, {"id":idv,"fecha":str(val(r,"fecha","") or pd.Timestamp.now()),"comp":comprobante,"cli":val(r,"id_cliente",None),"vend":vendedor,"metodo":str(val(r,"metodo_pago","")),"total":total,"pagado":pagado,"saldo":saldo,"estado":str(val(r,"estado_pago","Pagada")),"obs":str(val(r,"observacion",""))})
    detalle = read_table(sqlite_path, "detalle_ventas")
    productos_lookup = productos.set_index("id_producto").to_dict("index") if not productos.empty and "id_producto" in productos.columns else {}
    for _, r in detalle.iterrows():
        idv = int(val(r,"id_venta",0) or 0)
        idp = int(val(r,"id_producto",0) or 0)
        prod_row = productos_lookup.get(idp, {})
        nombre = str(val(r,"producto_nombre", prod_row.get("nombre_producto", f"Producto {idp}")))
        cant = float(val(r,"cantidad",0) or 0)
        precio = float(val(r,"precio_unitario", val(r,"precio",0)) or 0)
        costo = float(val(r,"costo_unitario", prod_row.get("costo_unitario",0)) or 0)
        sub = float(val(r,"subtotal", cant*precio) or 0)
        if idv and idp:
            exec_sql(pg, "INSERT INTO detalle_ventas (id_venta,id_producto,producto_nombre,cantidad,precio_unitario,costo_unitario,subtotal) VALUES (:idv,:idp,:prod,:cant,:precio,:costo,:sub)", {"idv":idv,"idp":idp,"prod":nombre,"cant":cant,"precio":precio,"costo":costo,"sub":sub})
            exec_sql(pg, "INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,observacion) VALUES (:idp,'SALIDA_VENTA',:cant,:costo,:ref,'Venta migrada')", {"idp":idp,"cant":cant,"costo":costo,"ref":f"VENTA {idv}"})

    print("Migrando compras y entradas de stock...")
    compras = read_table(sqlite_path, "compras")
    for _, r in compras.iterrows():
        idc = int(val(r,"id_compra",0) or 0) or None
        total = float(val(r,"total_compra",0) or 0)
        pagado = float(val(r,"monto_pagado",total) or 0)
        saldo = float(val(r,"saldo_pendiente",max(total-pagado,0)) or 0)
        exec_sql(pg, "INSERT INTO compras (id_compra,fecha,proveedor,metodo_pago,total_compra,monto_pagado,saldo_pendiente,estado_pago,observacion) VALUES (:id,:fecha,:prov,:metodo,:total,:pagado,:saldo,:estado,:obs) ON CONFLICT (id_compra) DO NOTHING", {"id":idc,"fecha":str(val(r,"fecha","") or pd.Timestamp.now()),"prov":str(val(r,"proveedor", val(r,"nombre_proveedor",""))),"metodo":str(val(r,"metodo_pago","")),"total":total,"pagado":pagado,"saldo":saldo,"estado":str(val(r,"estado_pago","Pagada")),"obs":str(val(r,"observacion",""))})
    dcompras = read_table(sqlite_path, "detalle_compras")
    for _, r in dcompras.iterrows():
        idc=int(val(r,"id_compra",0) or 0); idp=int(val(r,"id_producto",0) or 0)
        cant=float(val(r,"cantidad",0) or 0); costo=float(val(r,"costo_unitario",0) or 0); sub=float(val(r,"subtotal",cant*costo) or 0)
        if idc and idp:
            exec_sql(pg, "INSERT INTO detalle_compras (id_compra,id_producto,cantidad,costo_unitario,subtotal) VALUES (:idc,:idp,:cant,:costo,:sub)", {"idc":idc,"idp":idp,"cant":cant,"costo":costo,"sub":sub})
            exec_sql(pg, "INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,observacion) VALUES (:idp,'ENTRADA_COMPRA',:cant,:costo,:ref,'Compra migrada')", {"idp":idp,"cant":cant,"costo":costo,"ref":f"COMPRA {idc}"})

    print("Migrando ajustes de inventario...")
    ajustes = read_table(sqlite_path, "inventario_ajustes")
    for _, r in ajustes.iterrows():
        idp = int(val(r,"id_producto",0) or 0)
        cant = float(val(r,"cantidad",0) or 0)
        tipo_ajuste = str(val(r,"tipo_ajuste","")).lower()
        tipo = "AJUSTE_POSITIVO" if "aument" in tipo_ajuste or "+" in tipo_ajuste else "AJUSTE_NEGATIVO"
        if idp and cant:
            exec_sql(pg, "INSERT INTO movimientos_stock (id_producto,tipo,cantidad,costo_unitario,referencia,observacion) VALUES (:idp,:tipo,:cant,0,'AJUSTE MIGRADO',:obs)", {"idp":idp,"tipo":tipo,"cant":cant,"obs":str(val(r,"motivo","") or val(r,"observacion",""))})

    print("Migrando caja...")
    caja = read_table(sqlite_path, "caja")
    for _, r in caja.iterrows():
        tipo = str(val(r,"tipo", val(r,"tipo_movimiento","Ingreso"))).title()
        if tipo.upper().startswith("ING"): tipo = "Ingreso"
        elif tipo.upper().startswith("EGR") or tipo.upper().startswith("SAL"): tipo = "Egreso"
        monto=float(val(r,"monto", val(r,"ingreso",0) or val(r,"egreso",0)) or 0)
        exec_sql(pg, "INSERT INTO caja (fecha,tipo,concepto,metodo_pago,monto,referencia,observacion) VALUES (:fecha,:tipo,:concepto,:metodo,:monto,:ref,:obs)", {"fecha":str(val(r,"fecha","") or pd.Timestamp.now()),"tipo":tipo,"concepto":str(val(r,"concepto","Migrado")),"metodo":str(val(r,"metodo_pago","")),"monto":monto,"ref":str(val(r,"referencia","")),"obs":str(val(r,"observacion",""))})

    print("Migrando egresos/salidas...")
    egresos = read_table(sqlite_path, "egresos")
    for _, r in egresos.iterrows():
        monto=float(val(r,"monto",0) or 0)
        pagado=float(val(r,"monto_pagado",monto) or 0)
        saldo=float(val(r,"saldo_pendiente",max(monto-pagado,0)) or 0)
        exec_sql(pg, "INSERT INTO egresos (fecha,categoria_gasto,concepto,proveedor,monto,metodo_pago,estado_pago,monto_pagado,saldo_pendiente,observacion) VALUES (:fecha,:cat,:con,:prov,:monto,:metodo,:estado,:pagado,:saldo,:obs)", {"fecha":str(val(r,"fecha","") or pd.Timestamp.now()),"cat":str(val(r,"categoria_gasto","Otros")),"con":str(val(r,"concepto","")),"prov":str(val(r,"proveedor_gasto", val(r,"proveedor",""))),"monto":monto,"metodo":str(val(r,"metodo_pago","")),"estado":str(val(r,"estado_pago","Pagada")),"pagado":pagado,"saldo":saldo,"obs":str(val(r,"observacion",""))})

    print("Ajustando secuencias...")
    for table, col in [("usuarios","id_usuario"),("categorias","id_categoria"),("clientes","id_cliente"),("proveedores","id_proveedor"),("productos","id_producto"),("ventas","id_venta"),("compras","id_compra")]:
        try:
            exec_sql(pg, f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), COALESCE((SELECT MAX({col}) FROM {table}),1), true)")
        except Exception:
            pass
    print("Migración terminada.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", default="clomar_store.db", help="Ruta del archivo SQLite local")
    parser.add_argument("--url", default=os.getenv("NEON_DATABASE_URL"), help="Connection string de Neon. Mejor usar variable NEON_DATABASE_URL")
    args = parser.parse_args()
    if not args.url:
        args.url = input("Pega aquí tu NEON_DATABASE_URL: ").strip()
    migrate(args.sqlite, args.url)
