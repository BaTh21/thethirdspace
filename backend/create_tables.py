import psycopg2

DATABASE_URL = "postgresql://postgres:Admin123@localhost:5432/thirdspace_db"

def create_tables():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    # SQL to create tables
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
    print("✅ Tables created successfully (or already exist).")

if __name__ == "__main__":
    create_tables()