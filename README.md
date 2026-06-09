# Clomar Store Cloud

Sistema comercial propio para tienda multirrubro.

## Módulos incluidos en V24

- Login con roles: Administrador y Vendedor.
- Nueva venta tipo POS con carrito.
- Catálogo de productos en tarjetas.
- Clientes con ficha comercial.
- Inventario con stock actual y stock crítico.
- Ingreso de mercadería.
- Caja y egresos.
- Reportes del dueño.
- Backup/exportación a ZIP.
- Preparado para PostgreSQL en Neon y despliegue en Streamlit Cloud.

## Archivo principal

En Streamlit Cloud usar:

```text
app.py
```

## Secrets necesarios

En Streamlit Cloud configurar:

```toml
NEON_DATABASE_URL = "postgresql://USUARIO:CONTRASEÑA@HOST/neondb?sslmode=require"
```

No colocar la cadena de conexión dentro del código.

## Usuarios iniciales

```text
admin / admin123
vendedor / venta123
```

Cambiar estas claves antes del uso real.

## Migración desde SQLite local

Ejecutar localmente:

```bat
cd /d C:\PythonCivil
civil_env\Scripts\activate
set NEON_DATABASE_URL=postgresql://USUARIO:CONTRASEÑA@HOST/neondb?sslmode=require
python migrar_sqlite_a_neon.py --sqlite clomar_store.db
```

