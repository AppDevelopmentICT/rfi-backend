import os
import json
import sqlite3
import random
import string
from sqlalchemy import create_engine, text
import bcrypt
from datetime import datetime

# Load env vars manually for the script if needed or just use defaults
DATABASE_URL = "postgresql://n8n_user:P%40ssw0rd@10.0.80.13:5433/rfi-rfp-agent"
engine = create_engine(DATABASE_URL)

PB_DB_PATH = os.path.join("pocketbase", "pb_data", "data.db")
TEST_DIR = "test"

def generate_pb_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=15))

def generate_pb_token_key():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=50))

def hash_password(password):
    if not password: return ""
    return bcrypt.hashpw(password.encode('utf-8')[:72], bcrypt.gensalt()).decode('utf-8')

def import_all():
    print("Reading files...")
    with open(os.path.join(TEST_DIR, "Users.txt"), "r", encoding="utf-8") as f:
        users_data = json.load(f)["data"]
    with open(os.path.join(TEST_DIR, "Customer.txt"), "r", encoding="utf-8") as f:
        customers_data = json.load(f)["data"]
    with open(os.path.join(TEST_DIR, "Project.txt"), "r", encoding="utf-8") as f:
        projects_data = json.load(f)["data"]
    with open(os.path.join(TEST_DIR, "Product.txt"), "r", encoding="utf-8") as f:
        products_data = json.load(f)["data"]

    print("Connecting to PocketBase SQLite...")
    pb_conn = sqlite3.connect(PB_DB_PATH)
    pb_cursor = pb_conn.cursor()

    print("Connecting to PostgreSQL...")
    with engine.connect() as conn:
        print("Creating master tables in PostgreSQL...")
        # Add new columns to users table
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS original_uuid VARCHAR(255) UNIQUE"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS department_id VARCHAR(255)"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS manager_id VARCHAR(255)"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS roles JSONB"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS status_id INTEGER"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS level JSONB"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS grade JSONB"))
        
        # Create master tables
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS master_customers (
                id VARCHAR(255) PRIMARY KEY,
                customer_code VARCHAR(255),
                name VARCHAR(255)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS master_departments (
                id VARCHAR(255) PRIMARY KEY,
                name VARCHAR(255)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS master_brands (
                id VARCHAR(255) PRIMARY KEY,
                name VARCHAR(255)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS master_product_models (
                id VARCHAR(255) PRIMARY KEY,
                name VARCHAR(255)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS master_products (
                id VARCHAR(255) PRIMARY KEY,
                serial_number VARCHAR(255),
                support_identifier VARCHAR(255),
                warranty_until TIMESTAMP WITH TIME ZONE,
                brand_id VARCHAR(255),
                product_model_id VARCHAR(255)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS master_projects (
                id VARCHAR(255) PRIMARY KEY,
                project_code VARCHAR(255),
                name VARCHAR(500),
                project_type VARCHAR(255),
                status VARCHAR(255),
                customer_id VARCHAR(255)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS master_project_products (
                project_id VARCHAR(255),
                product_id VARCHAR(255),
                PRIMARY KEY (project_id, product_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS master_user_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                original_uuid VARCHAR(255),
                details JSONB
            )
        """))
        conn.commit()
        
        print("Inserting Customers...")
        for c in customers_data:
            conn.execute(text("""
                INSERT INTO master_customers (id, customer_code, name)
                VALUES (:id, :customer_code, :name)
                ON CONFLICT (id) DO NOTHING
            """), {"id": c["id"], "customer_code": c.get("customer_code"), "name": c.get("name")})
        
        print("Extracting Departments, Brands, Product Models...")
        depts = {}
        brands = {}
        models = {}
        for u in users_data:
            d = u.get("department")
            if d and d.get("id"): depts[d["id"]] = d["name"]
        for p in products_data:
            b = p.get("brand")
            if b and b.get("id"): brands[b["id"]] = b["name"]
            pm = p.get("productModel")
            if pm and pm.get("id"): models[pm["id"]] = pm["name"]
        
        for did, dname in depts.items():
            conn.execute(text("INSERT INTO master_departments (id, name) VALUES (:id, :name) ON CONFLICT (id) DO NOTHING"), {"id": did, "name": dname})
        for bid, bname in brands.items():
            conn.execute(text("INSERT INTO master_brands (id, name) VALUES (:id, :name) ON CONFLICT (id) DO NOTHING"), {"id": bid, "name": bname})
        for mid, mname in models.items():
            conn.execute(text("INSERT INTO master_product_models (id, name) VALUES (:id, :name) ON CONFLICT (id) DO NOTHING"), {"id": mid, "name": mname})
        conn.commit()
        
        print("Inserting Products...")
        for p in products_data:
            conn.execute(text("""
                INSERT INTO master_products (id, serial_number, support_identifier, warranty_until, brand_id, product_model_id)
                VALUES (:id, :sn, :si, :wu, :bid, :pmid)
                ON CONFLICT (id) DO NOTHING
            """), {
                "id": p["id"],
                "sn": p.get("serial_number"),
                "si": p.get("support_identifier"),
                "wu": p.get("warranty_until"),
                "bid": p.get("brand", {}).get("id") if p.get("brand") else None,
                "pmid": p.get("productModel", {}).get("id") if p.get("productModel") else None
            })
        conn.commit()

        print("Inserting Projects...")
        for p in projects_data:
            conn.execute(text("""
                INSERT INTO master_projects (id, project_code, name, project_type, status, customer_id)
                VALUES (:id, :pc, :name, :pt, :status, :cid)
                ON CONFLICT (id) DO NOTHING
            """), {
                "id": p["id"],
                "pc": p.get("project_code"),
                "name": p.get("name"),
                "pt": p.get("project_type"),
                "status": p.get("status"),
                "cid": p.get("customer", {}).get("id") if p.get("customer") else None
            })
            for prod in p.get("products", []):
                prodid = prod.get("product_id")
                if prodid:
                    conn.execute(text("""
                        INSERT INTO master_project_products (project_id, product_id)
                        VALUES (:pid, :prodid)
                        ON CONFLICT DO NOTHING
                    """), {"pid": p["id"], "prodid": prodid})
        conn.commit()

        print("Inserting Users into PocketBase and PostgreSQL...")
        for u in users_data:
            # 1. Insert into PocketBase if email doesn't exist
            email = u.get("email")
            if not email: continue
            email = email.lower().strip()
            name = u.get("name", "")
            
            # Check if user already in pocketbase
            pb_cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
            row = pb_cursor.fetchone()
            if row:
                pb_id = row[0]
                # PB User exists, verified = true if they login (user requirement)
                # "And if registered user login just make the email verified."
                # We can just update it here for now
                pb_cursor.execute("UPDATE users SET verified = 1 WHERE id = ?", (pb_id,))
                # Also we only use the decrypted password provided
                # "Password only one decrypted one and only the user can change it"
                if u.get("plain_password"):
                    # We will hash it with bcrypt
                    hashed_pw = hash_password(u["plain_password"])
                    pb_cursor.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_pw, pb_id))
            else:
                pb_id = generate_pb_id()
                token_key = generate_pb_token_key()
                hashed_pw = ""
                if u.get("plain_password"):
                    hashed_pw = hash_password(u["plain_password"])
                
                pb_cursor.execute("""
                    INSERT INTO users (id, email, emailVisibility, verified, name, password, tokenKey, created, updated, avatar)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), '')
                """, (pb_id, email, 0, 1, name, hashed_pw, token_key))
            
            pb_conn.commit()

            # 2. Insert/Update into PostgreSQL
            roles_json = json.dumps(u.get("roles", []))
            level_json = json.dumps(u.get("level")) if u.get("level") else None
            grade_json = json.dumps(u.get("grade")) if u.get("grade") else None
            
            res = conn.execute(text("SELECT id FROM users WHERE pocketbase_id = :pb_id"), {"pb_id": pb_id}).fetchone()
            if res:
                pg_user_id = res[0]
                conn.execute(text("""
                    UPDATE users SET 
                        original_uuid = :uuid,
                        department_id = :dept_id,
                        manager_id = :manager_id,
                        roles = :roles,
                        status_id = :status_id,
                        level = :level,
                        grade = :grade,
                        verified = true
                    WHERE id = :id
                """), {
                    "uuid": u["id"],
                    "dept_id": u.get("department", {}).get("id") if u.get("department") else None,
                    "manager_id": u.get("manager", {}).get("id") if u.get("manager") else None,
                    "roles": roles_json,
                    "status_id": u.get("status_id"),
                    "level": level_json,
                    "grade": grade_json,
                    "id": pg_user_id
                })
            else:
                res = conn.execute(text("""
                    INSERT INTO users (pocketbase_id, email, name, verified, is_admin, original_uuid, department_id, manager_id, roles, status_id, level, grade)
                    VALUES (:pb_id, :email, :name, true, false, :uuid, :dept_id, :manager_id, :roles, :status_id, :level, :grade)
                    RETURNING id
                """), {
                    "pb_id": pb_id,
                    "email": email,
                    "name": name,
                    "uuid": u["id"],
                    "dept_id": u.get("department", {}).get("id") if u.get("department") else None,
                    "manager_id": u.get("manager", {}).get("id") if u.get("manager") else None,
                    "roles": roles_json,
                    "status_id": u.get("status_id"),
                    "level": level_json,
                    "grade": grade_json
                }).fetchone()
                pg_user_id = res[0]
            
            # Profile Details page: Insert into profile table
            conn.execute(text("""
                INSERT INTO master_user_profiles (user_id, original_uuid, details)
                VALUES (:user_id, :uuid, :details)
                ON CONFLICT (user_id) DO UPDATE SET details = :details
            """), {
                "user_id": pg_user_id,
                "uuid": u["id"],
                "details": json.dumps(u)
            })

        conn.commit()
    pb_conn.close()
    print("Import completed successfully!")

if __name__ == "__main__":
    import_all()
