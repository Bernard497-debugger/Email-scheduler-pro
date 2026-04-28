#!/usr/bin/env python3
"""
Auto Scheduled Email Sending App - Render Optimized
Fixed for Render's file system permissions
"""

import os
import sys
import sqlite3
import re
import secrets
import json
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, g, jsonify

# Try to import werkzeug, fall back to hashlib if not available
try:
    from werkzeug.security import generate_password_hash, check_password_hash
    USE_WERKZEUG = True
except ImportError:
    import hashlib
    USE_WERKZEUG = False

# ==================== FIX PERMISSIONS - RENDER SPECIFIC ====================
# Render's ONLY writable directories
WRITABLE_DIRS = [
    '/opt/render/project/data',  # Render's persistent disk mount
    '/tmp',                       # Temporary directory (cleared on restart)
    os.getcwd(),                  # Current working directory
]

DATABASE = None

# Try each writable directory
for directory in WRITABLE_DIRS:
    try:
        # Test if directory exists and is writable
        if not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except:
                continue
        
        # Test write permission
        test_file = os.path.join(directory, '.write_test')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        
        # Use this directory for database
        db_path = os.path.join(directory, 'email_scheduler.db')
        DATABASE = db_path
        print(f"✅ Using database path: {DATABASE}")
        break
    except Exception as e:
        print(f"❌ Cannot use {directory}: {e}")
        continue

# Fallback if no directory works
if DATABASE is None:
    DATABASE = 'email_scheduler.db'
    print(f"⚠️ Using fallback database: {DATABASE}")

# Set environment variable
os.environ['DATABASE_PATH'] = DATABASE

print(f"📁 Database location: {DATABASE}")
print(f"📁 Current working directory: {os.getcwd()}")
print(f"📁 Files in current dir: {os.listdir('.') if os.path.exists('.') else 'None'}")

# ==================== CONFIGURATION ====================
app = Flask(__name__)

# Render-friendly configuration
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=7)

# Brevo API Configuration
BREVO_CONFIG = {
    'api_key': os.environ.get('BREVO_API_KEY', ''),
    'from_email': os.environ.get('FROM_EMAIL', 'noreply@yourdomain.com'),
    'from_name': os.environ.get('FROM_NAME', 'Email Scheduler Pro'),
    'api_url': 'https://api.brevo.com/v3/smtp/email'
}

# Pricing plans
PLANS = {
    'free': {'name': 'Free', 'emails_per_month': 9000, 'scheduled_emails': 50, 'price': 0},
    'basic': {'name': 'Basic', 'emails_per_month': 20000, 'scheduled_emails': 200, 'price': 9.99},
    'pro': {'name': 'Pro', 'emails_per_month': 50000, 'scheduled_emails': 1000, 'price': 19.99},
    'business': {'name': 'Business', 'emails_per_month': 100000, 'scheduled_emails': 5000, 'price': 49.99}
}

# ==================== PASSWORD FUNCTIONS ====================
def hash_password(password):
    """Secure password hashing - works without werkzeug"""
    if USE_WERKZEUG:
        return generate_password_hash(password)
    else:
        salt = secrets.token_hex(16)
        hash_obj = hashlib.sha256((salt + password).encode())
        return f"sha256${salt}${hash_obj.hexdigest()}"

def verify_password(password, password_hash):
    """Verify password - works without werkzeug"""
    if USE_WERKZEUG:
        return check_password_hash(password_hash, password)
    else:
        if not password_hash or '$' not in password_hash:
            return False
        parts = password_hash.split('$')
        if len(parts) != 3 or parts[0] != 'sha256':
            return False
        salt = parts[1]
        stored_hash = parts[2]
        calc_hash = hashlib.sha256((salt + password).encode()).hexdigest()
        return calc_hash == stored_hash

# ==================== DATABASE FUNCTIONS ====================
def get_db():
    """Get database connection"""
    if 'db' not in g:
        try:
            g.db = sqlite3.connect(DATABASE)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as e:
            print(f"Database connection error: {e}")
            raise
    return g.db

def close_db(e=None):
    """Close database connection"""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    """Initialize database tables"""
    try:
        db = sqlite3.connect(DATABASE)
        cursor = db.cursor()
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                plan TEXT DEFAULT 'free',
                emails_sent_this_month INTEGER DEFAULT 0,
                last_reset DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Scheduled emails table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                recipient_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                scheduled_time TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'scheduled',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP,
                retry_count INTEGER DEFAULT 0,
                error_message TEXT,
                brevo_message_id TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')
        
        # Create indexes for performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_scheduled_time ON scheduled_emails(scheduled_time, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_emails ON scheduled_emails(user_id, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_email ON users(email)')
        
        db.commit()
        db.close()
        
        print("✅ Database initialized successfully at:", DATABASE)
        
    except sqlite3.Error as e:
        print(f"Database initialization error: {e}")
        # Try to create database in current directory as fallback
        global DATABASE
        fallback_db = 'email_scheduler.db'
        try:
            db = sqlite3.connect(fallback_db)
            db.close()
            DATABASE = fallback_db
            print(f"✅ Using fallback database: {DATABASE}")
            # Retry initialization
            init_db()
        except Exception as e2:
            print(f"Fallback also failed: {e2}")

@app.teardown_appcontext
def teardown_db(error):
    close_db()

# ==================== USER FUNCTIONS ====================
def get_user(user_id):
    """Get user by ID"""
    try:
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        return user
    except sqlite3.Error as e:
        print(f"Get user error: {e}")
        return None

def get_user_by_email(email):
    """Get user by email"""
    try:
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE email = ?', (email.lower().strip(),)).fetchone()
        return user
    except sqlite3.Error as e:
        print(f"Get user by email error: {e}")
        return None

def create_user(email, password):
    """Create a new user"""
    try:
        db = get_db()
        db.execute(
            'INSERT INTO users (email, password, last_reset) VALUES (?, ?, ?)',
            (email.lower().strip(), hash_password(password), datetime.now().date().isoformat())
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.Error as e:
        print(f"Create user error: {e}")
        return False

def login_required(f):
    """Login decorator"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        
        user = get_user(session['user_id'])
        if not user:
            session.clear()
            flash('Session expired. Please login again.', 'warning')
            return redirect(url_for('login'))
        
        return f(*args, **kwargs)
    return decorated_function

def reset_monthly_counts():
    """Reset monthly email counts for all users"""
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        today = datetime.now().date()
        
        users = db.execute('SELECT id, last_reset FROM users').fetchall()
        for user in users:
            if user['last_reset']:
                last_reset = datetime.strptime(user['last_reset'], '%Y-%m-%d').date()
                if today > last_reset:
                    db.execute('UPDATE users SET emails_sent_this_month = 0, last_reset = ? WHERE id = ?', 
                              (today.isoformat(), user['id']))
        db.commit()
        db.close()
    except Exception as e:
        print(f"Reset monthly counts error: {e}")

def can_send_email(user):
    """Check if user can send more emails this month"""
    plan = PLANS[user['plan']]
    return user['emails_sent_this_month'] < plan['emails_per_month']

def can_schedule_email(user):
    """Check if user can schedule more emails"""
    try:
        db = get_db()
        scheduled_count = db.execute(
            'SELECT COUNT(*) as count FROM scheduled_emails WHERE user_id = ? AND status = "scheduled"',
            (user['id'],)
        ).fetchone()['count']
        
        plan = PLANS[user['plan']]
        return scheduled_count < plan['scheduled_emails']
    except sqlite3.Error:
        return False

# ==================== EMAIL FUNCTIONS ====================
def send_email_via_brevo(to_email, subject, body, user_email=None):
    """Send email using Brevo API"""
    if not BREVO_CONFIG['api_key']:
        return False, "Brevo API key not configured. Please set BREVO_API_KEY environment variable.", None
    
    try:
        import requests
        
        headers = {
            'api-key': BREVO_CONFIG['api_key'],
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        body_escaped = body.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto;">
                <div style="background: linear-gradient(135deg, #0284c7, #0ea5e9); color: white; padding: 20px; text-align: center; border-radius: 10px 10px 0 0;">
                    <h2>📧 Email Scheduler Pro</h2>
                </div>
                <div style="padding: 20px; background: #f9f9f9;">
                    {body_escaped.replace(chr(10), '<br>')}
                </div>
                <div style="text-align: center; padding: 15px; font-size: 12px; color: #666;">
                    <p>Sent via Email Scheduler Pro</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        data = {
            'sender': {'email': BREVO_CONFIG['from_email'], 'name': BREVO_CONFIG['from_name']},
            'to': [{'email': to_email}],
            'subject': subject,
            'textContent': body,
            'htmlContent': html_content
        }
        
        if user_email:
            data['replyTo'] = {'email': user_email}
        
        response = requests.post(BREVO_CONFIG['api_url'], json=data, headers=headers, timeout=30)
        
        if response.status_code in [200, 201]:
            result = response.json()
            return True, "Email sent successfully", result.get('messageId', 'sent')
        else:
            error_msg = response.json().get('message', 'Unknown error') if response.text else f"HTTP {response.status_code}"
            return False, f"Brevo API error: {error_msg}", None
            
    except Exception as e:
        return False, str(e), None

def send_scheduled_email(email_id):
    """Send a single scheduled email"""
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        
        email = db.execute('SELECT * FROM scheduled_emails WHERE id = ?', (email_id,)).fetchone()
        
        if not email or email['status'] != 'scheduled':
            db.close()
            return False
        
        # Check if it's time to send
        scheduled_time = datetime.fromisoformat(email['scheduled_time'])
        if scheduled_time > datetime.now():
            db.close()
            return False
        
        user = db.execute('SELECT * FROM users WHERE id = ?', (email['user_id'],)).fetchone()
        if not user:
            db.close()
            return False
        
        if not can_send_email(user):
            db.execute('UPDATE scheduled_emails SET status = "failed", error_message = "Monthly email limit reached" WHERE id = ?', (email_id,))
            db.commit()
            db.close()
            return False
        
        success, message, brevo_id = send_email_via_brevo(
            email['recipient_email'],
            email['subject'],
            email['body'],
            user['email']
        )
        
        if success:
            db.execute('''
                UPDATE scheduled_emails 
                SET status = "sent", sent_at = CURRENT_TIMESTAMP, brevo_message_id = ?, error_message = NULL
                WHERE id = ?
            ''', (brevo_id, email_id))
            
            db.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
        else:
            retry_count = email['retry_count'] + 1
            status = 'failed' if retry_count >= 3 else 'scheduled'
            db.execute('''
                UPDATE scheduled_emails 
                SET retry_count = ?, status = ?, error_message = ?
                WHERE id = ?
            ''', (retry_count, status, message, email_id))
        
        db.commit()
        db.close()
        return success
        
    except Exception as e:
        print(f"Send scheduled email error: {e}")
        return False

# ==================== SCHEDULER ENDPOINTS ====================
@app.route('/run-scheduler')
def run_scheduler():
    """Endpoint for cron-job.org to trigger email sending"""
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        
        now = datetime.now()
        due_emails = db.execute('''
            SELECT id FROM scheduled_emails 
            WHERE scheduled_time <= ? AND status = "scheduled"
        ''', (now.isoformat(),)).fetchall()
        
        db.close()
        
        sent_count = 0
        failed_count = 0
        
        for email in due_emails:
            if send_scheduled_email(email['id']):
                sent_count += 1
            else:
                failed_count += 1
        
        reset_monthly_counts()
        
        return jsonify({
            "status": "success",
            "checked_at": now.isoformat(),
            "emails_found": len(due_emails),
            "emails_sent": sent_count,
            "emails_failed": failed_count
        }), 200
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/scheduler-status')
def scheduler_status():
    """Get current scheduler status"""
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        
        now = datetime.now()
        pending_count = db.execute('''
            SELECT COUNT(*) as count FROM scheduled_emails 
            WHERE scheduled_time <= ? AND status = "scheduled"
        ''', (now.isoformat(),)).fetchone()['count']
        
        total_scheduled = db.execute('''
            SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "scheduled"
        ''').fetchone()['count']
        
        db.close()
        
        return jsonify({
            "status": "success",
            "current_time": now.isoformat(),
            "pending_emails_due": pending_count,
            "total_scheduled_emails": total_scheduled,
            "database_path": DATABASE
        }), 200
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health')
def health_check():
    """Health check endpoint"""
    try:
        db = sqlite3.connect(DATABASE)
        db.execute("SELECT 1")
        db.close()
        return jsonify({"status": "healthy", "database": DATABASE}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

@app.route('/ping')
def ping():
    """Simple ping endpoint"""
    return jsonify({"status": "alive", "time": datetime.now().isoformat()}), 200

# ==================== MAIN ROUTES ====================
@app.route('/')
def index():
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Scheduler Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%);
            min-height: 100vh;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { 
            background: white; 
            box-shadow: 0 2px 20px rgba(0,0,0,0.1);
            position: sticky; 
            top: 0; 
            z-index: 100;
        }
        .nav { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            padding: 15px 20px; 
            max-width: 1200px; 
            margin: 0 auto; 
        }
        .logo { 
            font-size: 28px; 
            font-weight: bold; 
            background: linear-gradient(135deg, #0284c7, #0ea5e9);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .nav-links a { 
            margin-left: 20px; 
            text-decoration: none; 
            color: #0369a1;
            font-weight: 500;
        }
        .hero { 
            text-align: center; 
            padding: 80px 20px; 
        }
        .hero h1 { 
            font-size: 52px; 
            margin-bottom: 20px; 
            background: linear-gradient(135deg, #0369a1, #0284c7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .hero p { font-size: 20px; margin-bottom: 30px; color: #075985; }
        .btn { 
            display: inline-block; 
            padding: 14px 35px; 
            background: linear-gradient(135deg, #0ea5e9, #0284c7);
            color: white; 
            text-decoration: none; 
            border-radius: 50px;
            font-weight: bold;
        }
        .badge {
            display: inline-block;
            background: #10b981;
            color: white;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            margin-left: 10px;
        }
        .free-badge { background: #f59e0b; }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler Pro <span class="badge">Brevo</span> <span class="badge free-badge">300/day Free</span></div>
            <div class="nav-links">
                {% if session.user_id %}
                    <a href="{{ url_for('dashboard') }}">Dashboard</a>
                    <a href="{{ url_for('logout') }}">Logout</a>
                {% else %}
                    <a href="{{ url_for('login') }}">Login</a>
                    <a href="{{ url_for('register') }}">Register</a>
                {% endif %}
            </div>
        </div>
    </div>
    <div class="hero">
        <div class="container">
            <h1>⚡ Schedule Emails with Brevo API</h1>
            <p>300 free emails/day • High deliverability • Powerful email infrastructure</p>
            {% if not session.user_id %}
                <a href="{{ url_for('register') }}" class="btn">🚀 Get Started Free</a>
            {% else %}
                <a href="{{ url_for('dashboard') }}" class="btn">📊 Go to Dashboard</a>
            {% endif %}
        </div>
    </div>
</body>
</html>
    ''', plans=PLANS)

# Simplified dashboard (you can add the full version later)
@app.route('/dashboard')
@login_required
def dashboard():
    user = get_user(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Dashboard - Email Scheduler Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f0f9ff; }
        .header { background: white; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 15px 30px; }
        .nav { display: flex; justify-content: space-between; align-items: center; max-width: 1200px; margin: 0 auto; }
        .logo { font-size: 24px; font-weight: bold; color: #0284c7; }
        .nav-links a { margin-left: 20px; text-decoration: none; color: #0369a1; }
        .container { max-width: 1200px; margin: 30px auto; padding: 0 20px; }
        .card { background: white; border-radius: 20px; padding: 25px; margin-bottom: 20px; box-shadow: 0 5px 15px rgba(0,0,0,0.1); }
        .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: white; padding: 20px; border-radius: 15px; text-align: center; }
        .stat-number { font-size: 36px; font-weight: bold; color: #0284c7; }
        .btn { padding: 10px 20px; background: #0284c7; color: white; border: none; border-radius: 10px; cursor: pointer; }
        .alert { padding: 12px 20px; margin-bottom: 20px; border-radius: 10px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        form input, form textarea { width: 100%; padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 8px; }
        button { background: #0284c7; color: white; padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler</div>
            <div class="nav-links">
                <span>Welcome, {{ user.email }}</span>
                <a href="{{ url_for('logout') }}">Logout</a>
            </div>
        </div>
    </div>
    <div class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-number">{{ user.emails_sent_this_month }}</div>
                <div>Emails Sent</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ remaining_scheduled }}</div>
                <div>Scheduled Slots</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ plan.name }}</div>
                <div>Current Plan</div>
            </div>
        </div>
        
        <div class="card">
            <h2>📤 Send Email Now</h2>
            <form method="POST" action="{{ url_for('send_now') }}">
                <input type="email" name="recipient" placeholder="Recipient Email" required>
                <input type="text" name="subject" placeholder="Subject" required>
                <textarea name="body" rows="3" placeholder="Message" required></textarea>
                <button type="submit">✈️ Send Now</button>
            </form>
        </div>
        
        <div class="card">
            <h2>⏰ Schedule Email</h2>
            <form method="POST" action="{{ url_for('schedule_email') }}">
                <input type="email" name="recipient" placeholder="Recipient Email" required>
                <input type="text" name="subject" placeholder="Subject" required>
                <textarea name="body" rows="3" placeholder="Message" required></textarea>
                <input type="datetime-local" name="schedule_datetime" required>
                <button type="submit">📅 Schedule</button>
            </form>
        </div>
    </div>
</body>
</html>
    ''', user=user, plan=PLANS[user['plan']], remaining_scheduled=50)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not email or not password:
            flash('All fields are required', 'error')
            return redirect(url_for('register'))
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return redirect(url_for('register'))
        
        if len(password) < 4:
            flash('Password must be at least 4 characters', 'error')
            return redirect(url_for('register'))
        
        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            flash('Invalid email address', 'error')
            return redirect(url_for('register'))
        
        existing_user = get_user_by_email(email)
        if existing_user:
            flash('Email already registered. Please login.', 'warning')
            return redirect(url_for('login'))
        
        if create_user(email, password):
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Registration failed. Please try again.', 'error')
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Register - Email Scheduler</title>
    <style>
        body { font-family: Arial, sans-serif; background: linear-gradient(135deg, #e0f2fe, #bae6fd); min-height: 100vh; display: flex; justify-content: center; align-items: center; }
        .card { background: white; padding: 40px; border-radius: 20px; width: 100%; max-width: 400px; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 8px; }
        button { width: 100%; padding: 12px; background: #0284c7; color: white; border: none; border-radius: 8px; cursor: pointer; }
        .alert { padding: 10px; margin-bottom: 15px; border-radius: 8px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
    </style>
</head>
<body>
    <div class="card">
        <h2>✨ Register</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="POST">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password (min 4 chars)" required>
            <input type="password" name="confirm_password" placeholder="Confirm Password" required>
            <button type="submit">Create Account →</button>
        </form>
        <p style="text-align: center; margin-top: 15px;"><a href="{{ url_for('login') }}">Already have an account? Login</a></p>
    </div>
</body>
</html>
    ''')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        if not email or not password:
            flash('Email and password are required', 'error')
            return redirect(url_for('login'))
        
        user = get_user_by_email(email)
        
        if user and verify_password(password, user['password']):
            session.permanent = True
            session['user_id'] = user['id']
            session['user_email'] = user['email']
            flash(f'Welcome back, {email}!', 'success')
            return redirect(url_for('dashboard'))
        
        flash('Invalid email or password', 'error')
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Login - Email Scheduler</title>
    <style>
        body { font-family: Arial, sans-serif; background: linear-gradient(135deg, #e0f2fe, #bae6fd); min-height: 100vh; display: flex; justify-content: center; align-items: center; }
        .card { background: white; padding: 40px; border-radius: 20px; width: 100%; max-width: 400px; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 8px; }
        button { width: 100%; padding: 12px; background: #0284c7; color: white; border: none; border-radius: 8px; cursor: pointer; }
        .alert { padding: 10px; margin-bottom: 15px; border-radius: 8px; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-success { background: #d1fae5; color: #065f46; }
    </style>
</head>
<body>
    <div class="card">
        <h2>🔐 Login</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="POST">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login →</button>
        </form>
        <p style="text-align: center; margin-top: 15px;"><a href="{{ url_for('register') }}">Create Account</a></p>
    </div>
</body>
</html>
    ''')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('index'))

@app.route('/send_now', methods=['POST'])
@login_required
def send_now():
    user = get_user(session['user_id'])
    if not user:
        return redirect(url_for('login'))
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    
    if not all([recipient, subject, body]):
        flash('All fields are required', 'error')
        return redirect(url_for('dashboard'))
    
    success, message, brevo_id = send_email_via_brevo(recipient, subject, body, user['email'])
    
    if success:
        try:
            db = get_db()
            db.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
            db.commit()
            flash('✅ Email sent successfully!', 'success')
        except:
            flash('Email sent but failed to update stats', 'warning')
    else:
        flash(f'❌ Failed: {message}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/schedule_email', methods=['POST'])
@login_required
def schedule_email():
    user = get_user(session['user_id'])
    if not user:
        return redirect(url_for('login'))
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    schedule_datetime_str = request.form.get('schedule_datetime', '')
    
    if not all([recipient, subject, body, schedule_datetime_str]):
        flash('All fields are required', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        scheduled_time = datetime.strptime(schedule_datetime_str, '%Y-%m-%dT%H:%M')
        if scheduled_time <= datetime.now():
            flash('Scheduled time must be in the future', 'error')
            return redirect(url_for('dashboard'))
        
        db = get_db()
        db.execute('''
            INSERT INTO scheduled_emails (user_id, recipient_email, subject, body, scheduled_time)
            VALUES (?, ?, ?, ?, ?)
        ''', (user['id'], recipient, subject, body, scheduled_time.isoformat()))
        db.commit()
        
        flash(f'📧 Scheduled for {scheduled_time.strftime("%Y-%m-%d %H:%M")}', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/upgrade_plan')
@login_required
def upgrade_plan():
    return render_template_string('<h2>Upgrade Plans - Contact Admin</h2><a href="/dashboard">Back</a>')

@app.route('/change_plan/<plan_name>')
@login_required
def change_plan(plan_name):
    flash('Plan upgrade is premium feature', 'info')
    return redirect(url_for('dashboard'))

# ==================== MAIN ====================
# Initialize database when app starts
with app.app_context():
    init_db()

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Email Scheduler Pro Starting...")
    print("=" * 60)
    print(f"✅ Database: {DATABASE}")
    print(f"✅ Brevo configured: {'Yes' if BREVO_CONFIG['api_key'] else 'No'}")
    print("=" * 60)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
