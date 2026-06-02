import os
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import psycopg2
from contextlib import asynccontextmanager

DATABASE_URL = os.environ.get("DATABASE_URL")

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
            cur.close()
            conn.close()
            print("✅ Database tables ready")
        except Exception as e:
            print(f"⚠️ Table creation warning: {e}")
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://thethirdspace.vercel.app", "https://thethirdspace-3.onrender.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/{full_path:path}")
async def serve_static(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404)
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    file_path = os.path.join(static_dir, full_path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.get("/")
async def root():
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.post("/api/orders")
async def create_order(order: OrderCreate):
    try:
        total = sum(item.price * item.quantity for item in order.items)
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO orders (customer_name, phone, notes, total_amount)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (order.customer_name, order.phone, order.notes, total))
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