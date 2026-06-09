import os
from sqlalchemy import create_engine, text

url = os.getenv("NEON_DATABASE_URL")
if not url:
    url = input("Pega tu NEON_DATABASE_URL: ").strip()

engine = create_engine(url, pool_pre_ping=True)
with engine.connect() as conn:
    version = conn.execute(text("select version()")) .scalar()
    print("Conexión correcta a PostgreSQL")
    print(version)
