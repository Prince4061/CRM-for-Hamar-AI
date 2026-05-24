import sqlite3
import os

DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'crm.db')

def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create clients table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            whatsapp_number TEXT NOT NULL UNIQUE
        )
    ''')
    
    # Create campaigns table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            message_content TEXT NOT NULL,
            media_path TEXT,
            delay INTEGER DEFAULT 5,
            batch_limit INTEGER DEFAULT 20,
            progress INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create message_logs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            campaign_id INTEGER NOT NULL,
            status TEXT NOT NULL, -- 'Pending', 'Sent', 'Failed'
            error_message TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients (id) ON DELETE CASCADE,
            FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
        )
    ''')
    
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    print("Database initialized successfully.")
