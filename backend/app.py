import os
import re
from datetime import datetime, timezone, timedelta
import secrets
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel
from typing import List
import psycopg2
from contextlib import asynccontextmanager

# ============================================
# Admin Authentication
# ============================================
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
# Simple token storage (in production use JWT or database)
admin_tokens = {}  # token -> expiry

DATABASE_URL = os.environ.get("DATABASE_URL")

# Cambodia timezone (UTC+7)
CAMBODIA_TZ = timezone(timedelta(hours=7))

def utc_to_cambodia(utc_dt: datetime) -> datetime:
    """Convert UTC datetime to Cambodia timezone"""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(CAMBODIA_TZ)

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

class OrderItem(BaseModel):
    name: str
    price: float
    quantity: int

class OrderCreate(BaseModel):
    customer_name: str
    phone: str
    notes: str = ""
    items: List[OrderItem]

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def clean_phone(phone: str) -> str:
    """Keep only digits from phone number"""
    return re.sub(r'\D', '', phone)

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
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
                    product_name VARCHAR(200) NOT NULL,
                    price DECIMAL(10,2) NOT NULL,
                    quantity INTEGER NOT NULL
                );
            """)
            conn.commit()
            cur.execute("SELECT id, phone FROM orders")
            all_orders = cur.fetchall()
            for order_id, old_phone in all_orders:
                cleaned = clean_phone(old_phone)
                if cleaned != old_phone:
                    cur.execute("UPDATE orders SET phone = %s WHERE id = %s", (cleaned, order_id))
            conn.commit()
            cur.close()
            conn.close()
            print("✅ Database tables ready and phone numbers cleaned")
        except Exception as e:
            print(f"⚠️ Table creation warning: {e}")
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://thethirdspace.vercel.app",
        "https://thethirdspace-3.onrender.com",
        "http://localhost:5500",
        "http://127.0.0.1:5500"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.post("/api/orders")
async def create_order(order: OrderCreate):
    try:
        total = sum(item.price * item.quantity for item in order.items)
        conn = get_db_connection()
        cur = conn.cursor()
        clean_phone_number = clean_phone(order.phone)
        try:
            cur.execute("""
                INSERT INTO orders (customer_name, phone, notes, total_amount)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (order.customer_name, clean_phone_number, order.notes, total))
            order_id = cur.fetchone()[0]

            for item in order.items:
                cur.execute("""
                    INSERT INTO order_items (order_id, product_name, price, quantity)
                    VALUES (%s, %s, %s, %s)
                """, (order_id, item.name, item.price, item.quantity))

            conn.commit()
            return {"message": "Order saved successfully", "order_id": order_id}
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/orders/history")
async def get_order_history(phone: str):
    """Get all orders for a customer by phone number (digits only)"""
    try:
        clean_phone_number = clean_phone(phone)
        if not clean_phone_number:
            return {"orders": []}

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, customer_name, phone, notes, total_amount, created_at
            FROM orders
            WHERE phone = %s
            ORDER BY created_at DESC
        """, (clean_phone_number,))
        orders = cur.fetchall()

        result = []
        for order in orders:
            order_id, customer_name, phone_num, notes, total, created_at_utc = order
            # Convert UTC to Cambodia time
            if created_at_utc:
                cambodia_time = utc_to_cambodia(created_at_utc)
                created_at_str = cambodia_time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                created_at_str = None

            cur.execute("""
                SELECT product_name, price, quantity
                FROM order_items
                WHERE order_id = %s
            """, (order_id,))
            items = [{"name": row[0], "price": float(row[1]), "quantity": row[2]}
                     for row in cur.fetchall()]

            result.append({
                "order_id": order_id,
                "customer_name": customer_name,
                "phone": phone_num,
                "notes": notes or "",
                "total_amount": float(total),
                "created_at": created_at_str,          
                "items": items
            })

        cur.close()
        conn.close()
        return {"orders": result}
    except Exception as e:
        print(f"History API error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/admin/orders")
async def get_all_orders(token: str = Depends(verify_admin_token)):
    """Get all orders with items (admin only)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, customer_name, phone, notes, total_amount, created_at
            FROM orders
            ORDER BY created_at DESC
        """)
        orders = cur.fetchall()
        
        result = []
        for order in orders:
            order_id, customer_name, phone_num, notes, total, created_at_utc = order
            created_at = utc_to_cambodia(created_at_utc).strftime("%Y-%m-%d %H:%M:%S")
            
            cur.execute("""
                SELECT product_name, price, quantity
                FROM order_items
                WHERE order_id = %s
            """, (order_id,))
            items = [{"name": row[0], "price": float(row[1]), "quantity": row[2]} for row in cur.fetchall()]
            
            result.append({
                "order_id": order_id,
                "customer_name": customer_name,
                "phone": phone_num,
                "notes": notes or "",
                "total_amount": float(total),
                "created_at": created_at,
                "items": items
            })
        
        cur.close()
        conn.close()
        return {"orders": result}
    except Exception as e:
        print(f"Admin orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
# Admin login endpoint
@app.post("/api/admin/login")
async def admin_login(username: str, password: str):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        token = generate_admin_token()
        return {"token": token, "message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid credentials")

# Admin stats endpoint (protected)
@app.get("/api/admin/stats")
async def get_admin_stats(token: str = Depends(verify_admin_token)):
    """Get aggregated order statistics (requires admin token)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Define cake and drink keywords
        cake_keywords = ['croissant', 'cheesecake', 'chocolate fudge', 'carrot walnut', 
                         'red velvet', 'cupcake', 'tiramisu', 'brownie', 'នំ']
        drink_keywords = ['macchiato', 'latte', 'matcha', 'cold brew', 'mocha', 'affogato',
                          'ការ៉ាមែល', 'ឡាតេ', 'ម៉ាឆា', 'ខូដប្រ៊ូ', 'ម៉ូកា', 'អាហ្វូហ្គាតូ']
        
        # Helper to classify product
        def classify_item(name_lower):
            if any(kw in name_lower for kw in cake_keywords):
                return 'cake'
            elif any(kw in name_lower for kw in drink_keywords):
                return 'drink'
            return 'other'
        
        # Get all order items with their created_at dates
        cur.execute("""
            SELECT oi.product_name, oi.quantity, o.created_at
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            ORDER BY o.created_at
        """)
        rows = cur.fetchall()
        
        # Prepare date ranges
        now = datetime.now(CAMBODIA_TZ)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())  # Monday
        month_start = today_start.replace(day=1)
        year_start = today_start.replace(month=1, day=1)
        
        def in_range(date, start, end):
            return start <= date < end
        
        stats = {
            "day": {"cake": 0, "drink": 0, "total_orders": 0},
            "week": {"cake": 0, "drink": 0, "total_orders": 0},
            "month": {"cake": 0, "drink": 0, "total_orders": 0},
            "year": {"cake": 0, "drink": 0, "total_orders": 0},
            "all_time": {"cake": 0, "drink": 0, "total_orders": 0}
        }
        
        # Track unique order IDs per period
        order_ids_day = set()
        order_ids_week = set()
        order_ids_month = set()
        order_ids_year = set()
        order_ids_all = set()
        
        for product_name, quantity, created_at_utc in rows:
            if created_at_utc is None:
                continue
            created_at = utc_to_cambodia(created_at_utc)
            category = classify_item(product_name.lower())
            order_id = None  # We don't have order_id directly in this SELECT, but we can add it
            # Actually we need order_id to count unique orders. Let's modify the SELECT
            # But for simplicity, we'll use a different query. Let's re-query with order_id.
            # I'll rewrite this section more efficiently below.
            pass
        
        # Better query with order_id
        cur.execute("""
            SELECT oi.order_id, oi.product_name, oi.quantity, o.created_at
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            ORDER BY o.created_at
        """)
        rows = cur.fetchall()
        
        stats = {
            "day": {"cake": 0, "drink": 0, "total_orders": 0},
            "week": {"cake": 0, "drink": 0, "total_orders": 0},
            "month": {"cake": 0, "drink": 0, "total_orders": 0},
            "year": {"cake": 0, "drink": 0, "total_orders": 0},
            "all_time": {"cake": 0, "drink": 0, "total_orders": 0}
        }
        orders_in_period = {
            "day": set(),
            "week": set(),
            "month": set(),
            "year": set(),
            "all_time": set()
        }
        
        for order_id, product_name, quantity, created_at_utc in rows:
            created_at = utc_to_cambodia(created_at_utc)
            category = classify_item(product_name.lower())
            if category not in ('cake', 'drink'):
                continue
            # All time
            stats["all_time"][category] += quantity
            orders_in_period["all_time"].add(order_id)
            # Day
            if in_range(created_at, today_start, today_start + timedelta(days=1)):
                stats["day"][category] += quantity
                orders_in_period["day"].add(order_id)
            # Week
            if in_range(created_at, week_start, week_start + timedelta(days=7)):
                stats["week"][category] += quantity
                orders_in_period["week"].add(order_id)
            # Month
            if in_range(created_at, month_start, month_start + timedelta(days=32)):  # safe upper bound
                stats["month"][category] += quantity
                orders_in_period["month"].add(order_id)
            # Year
            if in_range(created_at, year_start, year_start + timedelta(days=366)):
                stats["year"][category] += quantity
                orders_in_period["year"].add(order_id)
        
        # Add total order counts
        for period in stats:
            stats[period]["total_orders"] = len(orders_in_period[period])
        
        cur.close()
        conn.close()
        return stats
    except Exception as e:
        print(f"Admin stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.get("/{full_path:path}")
async def serve_static(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404)
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    file_path = os.path.join(static_dir, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return FileResponse(os.path.join(static_dir, "index.html"))