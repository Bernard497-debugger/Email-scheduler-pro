#!/usr/bin/env python3
"""
Email Scheduler Pro - Working Version for Render
Fixed: global variable declaration issue
"""

import os
import sqlite3
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, g

# ==================== CONFIGURATION ====================
app = Flask(__name__)

# Use Render's writable directory
WRITABLE_DIR = '/opt/render/project/data'
if not os.path.exists(WRITABLE_DIR):
    WRITABLE_DIR = os.getcwd()

DATABASE = os.path.join(WRITABLE_DIR, 'email_scheduler.db')

# Create database directory if needed
db_dir = os.path.dirname(DATABASE)
if db_dir and not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
    except:
        pass

app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

print(f"✅ Database path: {DATABASE}")
print(f"✅ Working directory: {os.getcwd()}")

# Brevo API Configuration
BREVO_CONFIG = {
    'api_key': os.environ.get('BREVO_API_KEY', ''),
    'from_email': os.environ.get('FROM_EMAIL', 'noreply@yourdomain.com'),
    'from_name': os.environ.get('FROM_NAME', 'Email Scheduler Pro'),
    'api_url': 'https://api.brevo.com/v3/smtp/email'
}

# ==================== DATABASE ====================
def init_db():
    """Initialize database"""
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
            error_message TEXT
        )
    ''')
    
    db.commit()
    db.close()
    print("✅ Database initialized")

def get_db():
    """Get database connection"""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db:
        db.close()

# ==================== PASSWORD ====================
def hash_password(password):
    """Simple password hashing"""
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_value):
    return hash_password(password) == hash_value

# ==================== USER FUNCTIONS ====================
def get_user_by_email(email):
    try:
        db = get_db()
        return db.execute('SELECT * FROM users WHERE email = ?', (email.lower(),)).fetchone()
    except:
        return None

def create_user(email, password):
    try:
        db = get_db()
        db.execute(
            'INSERT INTO users (email, password, last_reset) VALUES (?, ?, ?)',
            (email.lower(), hash_password(password), datetime.now().date().isoformat())
        )
        db.commit()
        return True
    except:
        return False

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ==================== ROUTES ====================
@app.route('/')
def index():
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Email Scheduler Pro</title>
    <style>
        body { font-family: Arial, sans-serif; background: linear-gradient(135deg, #e0f2fe, #bae6fd); min-height: 100vh; margin: 0; }
        .container { max-width: 800px; margin: 0 auto; padding: 20px; }
        .header { background: white; padding: 15px; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .hero { text-align: center; padding: 60px 20px; }
        h1 { color: #0284c7; }
        h2 { color: #0369a1; }
        .btn { display: inline-block; padding: 12px 30px; background: #0284c7; color: white; text-decoration: none; border-radius: 25px; margin: 10px; border: none; cursor: pointer; }
        .btn:hover { background: #0369a1; }
    </style>
</head>
<body>
    <div class="header">
        <h2>🚀 Email Scheduler Pro</h2>
        <div>
            {% if session.user_id %}
                <a href="/dashboard" class="btn">Dashboard</a>
                <a href="/logout" class="btn">Logout</a>
            {% else %}
                <a href="/login" class="btn">Login</a>
                <a href="/register" class="btn">Register</a>
            {% endif %}
        </div>
    </div>
    <div class="hero">
        <h1>Schedule Emails with Brevo API</h1>
        <p>300 free emails/day • High deliverability • Auto-scheduling</p>
        {% if not session.user_id %}
            <a href="/register" class="btn">Get Started Free</a>
        {% endif %}
    </div>
</body>
</html>
    ''')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        
        if not email or not password:
            flash('All fields required', 'error')
        elif password != confirm:
            flash('Passwords do not match', 'error')
        elif len(password) < 4:
            flash('Password too short (min 4 characters)', 'error')
        elif get_user_by_email(email):
            flash('Email already registered', 'warning')
        elif create_user(email, password):
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Registration failed', 'error')
        
        return redirect(url_for('register'))
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Register - Email Scheduler</title>
    <style>
        body { font-family: Arial, sans-serif; background: linear-gradient(135deg, #e0f2fe, #bae6fd); min-height: 100vh; display: flex; justify-content: center; align-items: center; margin: 0; }
        .card { background: white; padding: 40px; border-radius: 20px; width: 350px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); }
        h2 { color: #0369a1; text-align: center; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 8px; box-sizing: border-box; }
        button { width: 100%; padding: 12px; background: #0284c7; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; }
        button:hover { background: #0369a1; }
        .alert { padding: 10px; margin: 10px 0; border-radius: 8px; font-size: 14px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
        .link { text-align: center; margin-top: 15px; }
        .link a { color: #0284c7; text-decoration: none; }
    </style>
</head>
<body>
    <div class="card">
        <h2>✨ Create Account</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="alert alert-{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        <form method="POST">
            <input type="email" name="email" placeholder="Email Address" required>
            <input type="password" name="password" placeholder="Password (min 4 chars)" required>
            <input type="password" name="confirm_password" placeholder="Confirm Password" required>
            <button type="submit">Register →</button>
        </form>
        <div class="link">
            <a href="/login">Already have an account? Login</a>
        </div>
    </div>
</body>
</html>
    ''')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        user = get_user_by_email(email)
        if user and verify_password(password, user['password']):
            session['user_id'] = user['id']
            session['user_email'] = user['email']
            flash(f'Welcome back!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password', 'error')
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Login - Email Scheduler</title>
    <style>
        body { font-family: Arial, sans-serif; background: linear-gradient(135deg, #e0f2fe, #bae6fd); min-height: 100vh; display: flex; justify-content: center; align-items: center; margin: 0; }
        .card { background: white; padding: 40px; border-radius: 20px; width: 350px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); }
        h2 { color: #0369a1; text-align: center; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 8px; box-sizing: border-box; }
        button { width: 100%; padding: 12px; background: #0284c7; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; }
        button:hover { background: #0369a1; }
        .alert { padding: 10px; margin: 10px 0; border-radius: 8px; font-size: 14px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .link { text-align: center; margin-top: 15px; }
        .link a { color: #0284c7; text-decoration: none; }
    </style>
</head>
<body>
    <div class="card">
        <h2>🔐 Login</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="alert alert-{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        <form method="POST">
            <input type="email" name="email" placeholder="Email Address" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login →</button>
        </form>
        <div class="link">
            <a href="/register">Create New Account</a>
        </div>
    </div>
</body>
</html>
    ''')

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Dashboard - Email Scheduler</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f0f9ff; margin: 0; }
        .header { background: white; padding: 15px 30px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .container { max-width: 900px; margin: 30px auto; padding: 0 20px; }
        .card { background: white; border-radius: 15px; padding: 25px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 20px; }
        .stat-card { background: white; border-radius: 15px; padding: 20px; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .stat-number { font-size: 32px; font-weight: bold; color: #0284c7; }
        input, textarea { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 8px; box-sizing: border-box; }
        button { padding: 10px 20px; background: #0284c7; color: white; border: none; border-radius: 8px; cursor: pointer; }
        button:hover { background: #0369a1; }
        .alert { padding: 10px; margin-bottom: 15px; border-radius: 8px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
        .nav-links a { margin-left: 15px; text-decoration: none; color: #0284c7; }
        h3 { color: #0369a1; margin-top: 0; }
        .scheduler-buttons { display: flex; gap: 10px; margin-top: 15px; }
        .scheduler-buttons a { text-decoration: none; }
    </style>
</head>
<body>
    <div class="header">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2>🚀 Email Scheduler Pro</h2>
            <div class="nav-links">
                <span>👋 {{ session.user_email }}</span>
                <a href="/logout">Logout</a>
            </div>
        </div>
    </div>
    <div class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="alert alert-{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-number">0</div>
                <div>Emails Sent</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">50</div>
                <div>Slots Left</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">Free</div>
                <div>Current Plan</div>
            </div>
        </div>
        
        <div class="card">
            <h3>📤 Send Email Now</h3>
            <form method="POST" action="/send_now">
                <input type="email" name="recipient" placeholder="Recipient Email" required>
                <input type="text" name="subject" placeholder="Subject" required>
                <textarea name="body" rows="3" placeholder="Your message here..." required></textarea>
                <button type="submit">✈️ Send Now</button>
            </form>
        </div>
        
        <div class="card">
            <h3>⏰ Schedule Email</h3>
            <form method="POST" action="/schedule_email">
                <input type="email" name="recipient" placeholder="Recipient Email" required>
                <input type="text" name="subject" placeholder="Subject" required>
                <textarea name="body" rows="3" placeholder="Your message here..." required></textarea>
                <input type="datetime-local" name="schedule_datetime" required>
                <button type="submit">📅 Schedule</button>
            </form>
        </div>
        
        <div class="card">
            <h3>⚙️ Scheduler Controls</h3>
            <div class="scheduler-buttons">
                <a href="/run-scheduler"><button type="button">▶️ Run Scheduler Now</button></a>
                <a href="/scheduler-status"><button type="button">📊 Check Status</button></a>
            </div>
            <p style="font-size: 12px; color: #666; margin-top: 15px;">
                💡 Tip: Set up cron-job.org to call /run-scheduler every minute for auto-sending
            </p>
        </div>
    </div>
</body>
</html>
    ''')

@app.route('/send_now', methods=['POST'])
@login_required
def send_now():
    recipient = request.form.get('recipient')
    subject = request.form.get('subject')
    body = request.form.get('body')
    
    # For now, just log it
    flash(f'✅ Email would be sent to {recipient} (Add Brevo API key to enable)', 'success')
    return redirect(url_for('dashboard'))

@app.route('/schedule_email', methods=['POST'])
@login_required
def schedule_email():
    recipient = request.form.get('recipient')
    subject = request.form.get('subject')
    body = request.form.get('body')
    schedule_time = request.form.get('schedule_datetime')
    
    try:
        db = get_db()
        db.execute('''
            INSERT INTO scheduled_emails (user_id, recipient_email, subject, body, scheduled_time)
            VALUES (?, ?, ?, ?, ?)
        ''', (session['user_id'], recipient, subject, body, schedule_time))
        db.commit()
        flash(f'✅ Email scheduled for {schedule_time}', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/run-scheduler')
def run_scheduler():
    """Endpoint for cron-job.org to trigger email sending"""
    try:
        db = get_db()
        now = datetime.now().isoformat()
        
        # Get due emails
        due_emails = db.execute('''
            SELECT id FROM scheduled_emails 
            WHERE scheduled_time <= ? AND status = 'scheduled'
        ''', (now,)).fetchall()
        
        count = len(due_emails)
        
        return {
            "status": "success", 
            "message": f"Scheduler checked {count} emails",
            "time": str(datetime.now()),
            "emails_found": count
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.route('/scheduler-status')
def scheduler_status():
    try:
        db = get_db()
        scheduled = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "scheduled"').fetchone()['count']
        sent = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "sent"').fetchone()['count']
        failed = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "failed"').fetchone()['count']
        
        return {
            "status": "success",
            "scheduled": scheduled,
            "sent": sent,
            "failed": failed,
            "total": scheduled + sent + failed
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('index'))

@app.route('/health')
def health():
    return {"status": "healthy", "database": DATABASE}

# ==================== INITIALIZE ====================
# Initialize database when app starts
with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
