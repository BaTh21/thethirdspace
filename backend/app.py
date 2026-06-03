import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import psycopg2
from contextlib import asynccontextmanager

# ============================================
# Configuration
# ============================================
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
CAMBODIA_TZ = timezone(timedelta(hours=7))

admin_tokens = {}  # token -> expiry (datetime)

# ============================================
# Helper Functions
# ============================================
def utc_to_cambodia(utc_dt: datetime) -> datetime:
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(CAMBODIA_TZ)

def clean_phone(phone: str) -> str:
    return re.sub(r'\D', '', phone)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def generate_admin_token():
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(CAMBODIA_TZ) + timedelta(hours=8)
    admin_tokens[token] = expiry
    return token

def verify_admin_token(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    scheme, _, token = authorization.partition(' ')
    if scheme.lower() != 'bearer':
        raise HTTPException(status_code=401, detail="Invalid auth scheme")
    expiry = admin_tokens.get(token)
    if not expiry or expiry < datetime.now(CAMBODIA_TZ):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return token

# ============================================
# Pydantic Models
# ============================================
class OrderItem(BaseModel):
    name: str
    price: float
    quantity: int

class OrderCreate(BaseModel):
    customer_name: str
    phone: str
    notes: str = ""
    items: List[OrderItem]
    
class AdminLogin(BaseModel):
    username: str
    password: str

# ============================================
# Database Initialization
# ============================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    customer_name VARCHAR(100) NOT NULL,
                    phone VARCHAR(20) NOT NULL,
                    notes TEXT,
                    total_amount DECIMAL(10,2) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
                    product_name VARCHAR(200) NOT NULL,
                    price DECIMAL(10,2) NOT NULL,
                    quantity INTEGER NOT NULL
                )
            """)
            conn.commit()
            # Clean existing phone numbers
            cur.execute("SELECT id, phone FROM orders")
            for order_id, old_phone in cur.fetchall():
                cleaned = clean_phone(old_phone)
                if cleaned != old_phone:
                    cur.execute("UPDATE orders SET phone = %s WHERE id = %s", (cleaned, order_id))
            conn.commit()
            cur.close()
            conn.close()
            print("✅ Database ready")
        except Exception as e:
            print(f"⚠️ DB init error: {e}")
    yield

app = FastAPI(lifespan=lifespan)

# ============================================
# CORS
# ============================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, specify your domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ============================================
# API ROUTES (must come before catch-all)
# ============================================

@app.post("/api/orders")
async def create_order(order: OrderCreate):
    try:
        total = sum(item.price * item.quantity for item in order.items)
        conn = get_db_connection()
        cur = conn.cursor()
        clean_phone_number = clean_phone(order.phone)
        cur.execute("""
            INSERT INTO orders (customer_name, phone, notes, total_amount)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (order.customer_name, clean_phone_number, order.notes, total))
        order_id = cur.fetchone()[0]
        for item in order.items:
            cur.execute("""
                INSERT INTO order_items (order_id, product_name, price, quantity)
                VALUES (%s, %s, %s, %s)
            """, (order_id, item.name, item.price, item.quantity))
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Order saved", "order_id": order_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/orders/history")
async def get_order_history(phone: str):
    try:
        clean_phone_number = clean_phone(phone)
        if not clean_phone_number:
            return {"orders": []}
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, customer_name, phone, notes, total_amount, created_at
            FROM orders WHERE phone = %s ORDER BY created_at DESC
        """, (clean_phone_number,))
        orders = cur.fetchall()
        result = []
        for order in orders:
            order_id, customer_name, phone_num, notes, total, created_utc = order
            created = utc_to_cambodia(created_utc).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("SELECT product_name, price, quantity FROM order_items WHERE order_id = %s", (order_id,))
            items = [{"name": row[0], "price": float(row[1]), "quantity": row[2]} for row in cur.fetchall()]
            result.append({
                "order_id": order_id,
                "customer_name": customer_name,
                "phone": phone_num,
                "notes": notes or "",
                "total_amount": float(total),
                "created_at": created,
                "items": items
            })
        cur.close()
        conn.close()
        return {"orders": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Admin login


@app.post("/api/admin/login")
async def admin_login(login: AdminLogin):
    if login.username == ADMIN_USERNAME and login.password == ADMIN_PASSWORD:
        token = generate_admin_token()
        return {"token": token, "message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid credentials")

# Admin stats (protected)
@app.get("/api/admin/stats")
async def get_admin_stats(token: str = Depends(verify_admin_token)):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Classify items
        cake_keywords = ['croissant', 'cheesecake', 'chocolate fudge', 'carrot walnut', 
                         'red velvet', 'cupcake', 'tiramisu', 'brownie', 'នំ']
        drink_keywords = ['macchiato', 'latte', 'matcha', 'cold brew', 'mocha', 'affogato',
                          'ការ៉ាមែល', 'ឡាតេ', 'ម៉ាឆា', 'ខូដប្រ៊ូ', 'ម៉ូកា', 'អាហ្វូហ្គាតូ']
        def classify(name):
            n = name.lower()
            if any(kw in n for kw in cake_keywords): return 'cake'
            if any(kw in n for kw in drink_keywords): return 'drink'
            return 'other'
        # Fetch all order items with order creation time
        cur.execute("""
            SELECT oi.order_id, oi.product_name, oi.quantity, o.created_at
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
        """)
        rows = cur.fetchall()
        now = datetime.now(CAMBODIA_TZ)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)
        year_start = today_start.replace(month=1, day=1)
        stats = {
            "day": {"cake": 0, "drink": 0, "total_orders": 0},
            "week": {"cake": 0, "drink": 0, "total_orders": 0},
            "month": {"cake": 0, "drink": 0, "total_orders": 0},
            "year": {"cake": 0, "drink": 0, "total_orders": 0},
            "all_time": {"cake": 0, "drink": 0, "total_orders": 0}
        }
        order_ids = {"day": set(), "week": set(), "month": set(), "year": set(), "all_time": set()}
        for order_id, product_name, qty, created_utc in rows:
            created = utc_to_cambodia(created_utc)
            cat = classify(product_name)
            if cat not in ('cake', 'drink'):
                continue
            # All time
            stats["all_time"][cat] += qty
            order_ids["all_time"].add(order_id)
            # Day
            if today_start <= created < today_start + timedelta(days=1):
                stats["day"][cat] += qty
                order_ids["day"].add(order_id)
            # Week
            if week_start <= created < week_start + timedelta(days=7):
                stats["week"][cat] += qty
                order_ids["week"].add(order_id)
            # Month
            if month_start <= created < month_start + timedelta(days=32):
                stats["month"][cat] += qty
                order_ids["month"].add(order_id)
            # Year
            if year_start <= created < year_start + timedelta(days=366):
                stats["year"][cat] += qty
                order_ids["year"].add(order_id)
        for p in stats:
            stats[p]["total_orders"] = len(order_ids[p])
        cur.close()
        conn.close()
        return stats
    except Exception as e:
        print(f"Stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Admin all orders (protected)
@app.get("/api/admin/orders")
async def get_all_orders(token: str = Depends(verify_admin_token)):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, customer_name, phone, notes, total_amount, created_at FROM orders ORDER BY created_at DESC")
        orders = cur.fetchall()
        result = []
        for order in orders:
            order_id, customer_name, phone_num, notes, total, created_utc = order
            created = utc_to_cambodia(created_utc).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("SELECT product_name, price, quantity FROM order_items WHERE order_id = %s", (order_id,))
            items = [{"name": row[0], "price": float(row[1]), "quantity": row[2]} for row in cur.fetchall()]
            result.append({
                "order_id": order_id,
                "customer_name": customer_name,
                "phone": phone_num,
                "notes": notes or "",
                "total_amount": float(total),
                "created_at": created,
                "items": items
            })
        cur.close()
        conn.close()
        return {"orders": result}
    except Exception as e:
        print(f"Admin orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================
# SPA FALLBACK ROUTES (must be last)
# ============================================
@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/{full_path:path}")
async def serve_static(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404)
    file_path = os.path.join(STATIC_DIR, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))