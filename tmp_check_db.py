import sqlite3
import os

db_path = "smp_price_tracker.sqlite3"
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT count(*) FROM observations")
        count = cursor.fetchone()[0]
        print(f"Observations count: {count}")
        
        cursor.execute("SELECT * FROM observations LIMIT 5")
        rows = cursor.fetchall()
        for row in rows:
            print(row)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
