import sqlite3
import os

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import IntegrityError as PostgresIntegrityError
except ImportError:
    class PostgresIntegrityError(Exception):
        pass

# Shared exception tuple for catching constraint violations in app.py
DbIntegrityError = (sqlite3.IntegrityError, PostgresIntegrityError)

DATABASE_URL = os.environ.get('DATABASE_URL')
DATABASE_PATH = os.environ.get('DATABASE_PATH') or os.path.join(os.path.dirname(__file__), 'crm.db')

class DualCursor:
    def __init__(self, cursor, is_postgres):
        self.cursor = cursor
        self.is_postgres = is_postgres
        self._lastrowid = None
        
    def execute(self, query, params=None):
        if self.is_postgres:
            # Replace SQLite '?' placeholder with PostgreSQL '%s'
            query = query.replace('?', '%s')
            
            # Intercept INSERT queries to emulate SQLite's lastrowid
            if query.strip().upper().startswith('INSERT INTO'):
                query = query.rstrip('; ') + ' RETURNING id'
                if params is not None:
                    self.cursor.execute(query, params)
                else:
                    self.cursor.execute(query)
                res = self.cursor.fetchone()
                if res:
                    # Support dictionary row structures as well as raw tuples
                    self._lastrowid = res[0] if not hasattr(res, 'get') else (res.get('id') or res[0])
                return self
                
        if params is not None:
            self.cursor.execute(query, params)
        else:
            self.cursor.execute(query)
        return self
        
    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        return row
        
    def fetchall(self):
        return self.cursor.fetchall()
        
    def __iter__(self):
        return iter(self.cursor)
        
    @property
    def lastrowid(self):
        if self.is_postgres:
            return self._lastrowid
        return self.cursor.lastrowid

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def __getattr__(self, name):
        return getattr(self.cursor, name)

class DualConnection:
    def __init__(self, conn, is_postgres):
        self.conn = conn
        self.is_postgres = is_postgres
        
    def cursor(self):
        return DualCursor(self.conn.cursor(), self.is_postgres)
        
    def execute(self, query, params=None):
        cursor = self.cursor()
        cursor.execute(query, params)
        return cursor
        
    def commit(self):
        self.conn.commit()
        
    def rollback(self):
        self.conn.rollback()
        
    def close(self):
        self.conn.close()

def get_db_connection():
    if DATABASE_URL:
        # PostgreSQL/Supabase connection
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
        return DualConnection(conn, is_postgres=True)
    else:
        # SQLite local connection
        conn = sqlite3.connect(DATABASE_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        return DualConnection(conn, is_postgres=False)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if conn.is_postgres:
        # Create clients table (PostgreSQL)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                whatsapp_number TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT 'General',
                is_premium INTEGER DEFAULT 0,
                dob TEXT,
                event_name TEXT,
                event_date TEXT,
                pending_amount REAL DEFAULT 0.0,
                pending_reason TEXT
            )
        ''')
        
        # Create campaigns table (PostgreSQL)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS campaigns (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                message_content TEXT NOT NULL,
                media_path TEXT,
                delay INTEGER DEFAULT 5,
                batch_limit INTEGER DEFAULT 20,
                progress INTEGER DEFAULT 0,
                status TEXT DEFAULT 'Pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                target_category TEXT DEFAULT 'All'
            )
        ''')
        
        # Create message_logs table (PostgreSQL)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_logs (
                id SERIAL PRIMARY KEY,
                client_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients (id) ON DELETE CASCADE,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')
        
        # Create hot_leads table (PostgreSQL)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_leads (
                id SERIAL PRIMARY KEY,
                client_id INTEGER NOT NULL UNIQUE,
                last_message TEXT,
                replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'New',
                FOREIGN KEY (client_id) REFERENCES clients (id) ON DELETE CASCADE
            )
        ''')
        
        # Migration logic for PostgreSQL columns
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'clients' AND column_name = 'category'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE clients ADD COLUMN category TEXT DEFAULT 'General'")
            
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'clients' AND column_name = 'is_premium'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE clients ADD COLUMN is_premium INTEGER DEFAULT 0")

        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'clients' AND column_name = 'dob'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE clients ADD COLUMN dob TEXT")

        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'clients' AND column_name = 'event_name'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE clients ADD COLUMN event_name TEXT")

        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'clients' AND column_name = 'event_date'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE clients ADD COLUMN event_date TEXT")
            
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'clients' AND column_name = 'pending_amount'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE clients ADD COLUMN pending_amount REAL DEFAULT 0.0")

        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'clients' AND column_name = 'pending_reason'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE clients ADD COLUMN pending_reason TEXT")
            
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'campaigns' AND column_name = 'target_category'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE campaigns ADD COLUMN target_category TEXT DEFAULT 'All'")
            
    else:
        # Create clients table (SQLite)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                whatsapp_number TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT 'General',
                is_premium INTEGER DEFAULT 0,
                dob TEXT,
                event_name TEXT,
                event_date TEXT,
                pending_amount REAL DEFAULT 0.0,
                pending_reason TEXT
            )
        ''')
        
        # Create campaigns table (SQLite)
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                target_category TEXT DEFAULT 'All'
            )
        ''')
        
        # Create message_logs table (SQLite)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients (id) ON DELETE CASCADE,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')
        
        # Create hot_leads table (SQLite)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL UNIQUE,
                last_message TEXT,
                replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'New',
                FOREIGN KEY (client_id) REFERENCES clients (id) ON DELETE CASCADE
            )
        ''')
        
        # Migration logic for SQLite columns
        cursor.execute("PRAGMA table_info(clients)")
        client_columns = [row[1] for row in cursor.fetchall()]
        if 'category' not in client_columns:
            cursor.execute("ALTER TABLE clients ADD COLUMN category TEXT DEFAULT 'General'")
        if 'is_premium' not in client_columns:
            cursor.execute("ALTER TABLE clients ADD COLUMN is_premium INTEGER DEFAULT 0")
        if 'dob' not in client_columns:
            cursor.execute("ALTER TABLE clients ADD COLUMN dob TEXT")
        if 'event_name' not in client_columns:
            cursor.execute("ALTER TABLE clients ADD COLUMN event_name TEXT")
        if 'event_date' not in client_columns:
            cursor.execute("ALTER TABLE clients ADD COLUMN event_date TEXT")
        if 'pending_amount' not in client_columns:
            cursor.execute("ALTER TABLE clients ADD COLUMN pending_amount REAL DEFAULT 0.0")
        if 'pending_reason' not in client_columns:
            cursor.execute("ALTER TABLE clients ADD COLUMN pending_reason TEXT")
            
        cursor.pragma = cursor.execute("PRAGMA table_info(campaigns)")
        campaign_columns = [row[1] for row in cursor.fetchall()]
        if 'target_category' not in campaign_columns:
            cursor.execute("ALTER TABLE campaigns ADD COLUMN target_category TEXT DEFAULT 'All'")
            
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    print("Database initialized successfully.")
