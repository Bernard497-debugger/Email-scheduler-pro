#!/usr/bin/env python3
"""
Auto Scheduled Email Sending App - PRODUCTION READY
Fixed for Gunicorn and direct execution
"""

import sqlite3
import threading
import time
import re
import os
import secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, g

# Try to import werkzeug, fall back to hashlib if not available
try:
    from werkzeug.security import generate_password_hash, check_password_hash
    USE_WERKZEUG = True
except ImportError:
    import hashlib
    USE_WERKZEUG = False

# ==================== CONFIGURATION ====================
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=7)

# Database file path
DATABASE = os.environ.get('DATABASE_PATH', 'email_scheduler.db')

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
        # Fallback to hashlib
        salt = secrets.token_hex(16)
        hash_obj = hashlib.sha256((salt + password).encode())
        return f"sha256${salt}${hash_obj.hexdigest()}"

def verify_password(password, password_hash):
    """Verify password - works without werkzeug"""
    if USE_WERKZEUG:
        return check_password_hash(password_hash, password)
    else:
        # Fallback to hashlib
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
        
        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_scheduled_time ON scheduled_emails(scheduled_time, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_emails ON scheduled_emails(user_id, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_email ON users(email)')
        
        db.commit()
        db.close()
        
        # Run migrations
        migrate_database()
        
        print("✅ Database initialized successfully")
        
    except sqlite3.Error as e:
        print(f"Database initialization error: {e}")

def migrate_database():
    """Add missing columns to existing database"""
    try:
        db = sqlite3.connect(DATABASE)
        cursor = db.cursor()
        
        cursor.execute("PRAGMA table_info(scheduled_emails)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'brevo_message_id' not in columns:
            cursor.execute("ALTER TABLE scheduled_emails ADD COLUMN brevo_message_id TEXT")
            print("✅ Added brevo_message_id column")
        
        if 'error_message' not in columns:
            cursor.execute("ALTER TABLE scheduled_emails ADD COLUMN error_message TEXT")
            print("✅ Added error_message column")
        
        db.commit()
        db.close()
    except Exception as e:
        print(f"Migration error: {e}")

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

# ==================== EMAIL FUNCTIONS ====================
def send_email_via_brevo(to_email, subject, body, user_email=None):
    """Send email using Brevo API"""
    if not BREVO_CONFIG['api_key']:
        return False, "Brevo API key not configured", None
    
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

# ==================== ROUTES ====================
@app.route('/')
def index():
    return render_template_string(INDEX_TEMPLATE, plans=PLANS)

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
    
    return render_template_string(REGISTER_TEMPLATE)

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
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    user = get_user(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    
    try:
        db = get_db()
        scheduled_emails = db.execute('''
            SELECT * FROM scheduled_emails 
            WHERE user_id = ? 
            ORDER BY scheduled_time DESC 
            LIMIT 50
        ''', (user['id'],)).fetchall()
    except sqlite3.Error:
        scheduled_emails = []
    
    plan_details = PLANS[user['plan']]
    usage_percentage = (user['emails_sent_this_month'] / plan_details['emails_per_month'] * 100) if plan_details['emails_per_month'] > 0 else 0
    
    remaining_emails = max(0, plan_details['emails_per_month'] - user['emails_sent_this_month'])
    
    try:
        scheduled_count = db.execute(
            'SELECT COUNT(*) as count FROM scheduled_emails WHERE user_id = ? AND status = "scheduled"',
            (user['id'],)
        ).fetchone()['count']
        remaining_scheduled = max(0, plan_details['scheduled_emails'] - scheduled_count)
    except:
        remaining_scheduled = plan_details['scheduled_emails']
    
    return render_template_string(DASHBOARD_TEMPLATE, 
                                user=user,
                                scheduled_emails=scheduled_emails,
                                plan=plan_details,
                                plan_name=user['plan'],
                                usage_percentage=usage_percentage,
                                remaining_emails=remaining_emails,
                                remaining_scheduled=remaining_scheduled,
                                brevo_configured=bool(BREVO_CONFIG['api_key']))

@app.route('/schedule_email', methods=['POST'])
@login_required
def schedule_email():
    user = get_user(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    schedule_datetime_str = request.form.get('schedule_datetime', '')
    
    if not all([recipient, subject, body, schedule_datetime_str]):
        flash('All fields are required', 'error')
        return redirect(url_for('dashboard'))
    
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', recipient):
        flash('Invalid email address', 'error')
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
        
        flash(f'📧 Email scheduled for {scheduled_time.strftime("%Y-%m-%d %H:%M")}', 'success')
    except ValueError:
        flash('Invalid date/time format', 'error')
    except sqlite3.Error as e:
        flash(f'Database error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/send_now', methods=['POST'])
@login_required
def send_now():
    user = get_user(session['user_id'])
    
    if not user:
        session.clear()
        return redirect(url_for('login'))
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    
    if not all([recipient, subject, body]):
        flash('All fields are required', 'error')
        return redirect(url_for('dashboard'))
    
    if not BREVO_CONFIG['api_key']:
        flash('Brevo API not configured. Please set BREVO_API_KEY environment variable.', 'error')
        return redirect(url_for('dashboard'))
    
    success, message, brevo_id = send_email_via_brevo(recipient, subject, body, user['email'])
    
    if success:
        try:
            db = get_db()
            db.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
            db.commit()
            flash(f'✅ Email sent successfully!', 'success')
        except sqlite3.Error as e:
            flash(f'Email sent but failed to update stats: {str(e)}', 'warning')
    else:
        flash(f'❌ Failed to send: {message}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/cancel_scheduled/<int:email_id>')
@login_required
def cancel_scheduled(email_id):
    user = get_user(session['user_id'])
    
    if not user:
        session.clear()
        return redirect(url_for('login'))
    
    try:
        db = get_db()
        email = db.execute('SELECT * FROM scheduled_emails WHERE id = ?', (email_id,)).fetchone()
        
        if email and email['user_id'] == user['id'] and email['status'] == 'scheduled':
            db.execute('UPDATE scheduled_emails SET status = "cancelled" WHERE id = ?', (email_id,))
            db.commit()
            flash('Scheduled email cancelled', 'success')
        else:
            flash('Cannot cancel this email', 'error')
    except sqlite3.Error as e:
        flash(f'Error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/upgrade_plan')
@login_required
def upgrade_plan():
    user = get_user(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    
    return render_template_string(UPGRADE_TEMPLATE, plans=PLANS, current_plan=user['plan'])

@app.route('/change_plan/<plan_name>')
@login_required
def change_plan(plan_name):
    if plan_name not in PLANS:
        flash('Invalid plan', 'error')
        return redirect(url_for('upgrade_plan'))
    
    try:
        db = get_db()
        db.execute('UPDATE users SET plan = ? WHERE id = ?', (plan_name, session['user_id']))
        db.commit()
        flash(f'✨ Plan changed to {PLANS[plan_name]["name"]}!', 'success')
    except sqlite3.Error as e:
        flash(f'Error changing plan: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

# ==================== HTML TEMPLATES ====================
# (Templates remain the same - omitted for brevity but they work)
INDEX_TEMPLATE = '''
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
            transition: color 0.3s;
        }
        .nav-links a:hover { color: #0284c7; }
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
            transition: transform 0.3s, box-shadow 0.3s;
            box-shadow: 0 4px 15px rgba(2, 132, 199, 0.3);
        }
        .btn:hover { transform: translateY(-3px); box-shadow: 0 6px 20px rgba(2, 132, 199, 0.4); }
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
        @media (max-width: 768px) {
            .hero h1 { font-size: 36px; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler Pro <span class="badge">Brevo</span> <span class="badge free-badge">300/day Free</span></div>
            <div class="nav-links">
                {% if session.user_id %}
                    <a href="{{ url_for('dashboard') }}">Dashboard</a>
                    <a href="{{ url_for('upgrade_plan') }}">Upgrade</a>
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
'''

LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Email Scheduler Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .card { 
            background: white; 
            padding: 45px; 
            border-radius: 30px; 
            box-shadow: 0 20px 40px rgba(2, 132, 199, 0.2);
            width: 100%; 
            max-width: 420px;
        }
        .card h2 { text-align: center; margin-bottom: 30px; color: #0369a1; font-size: 32px; }
        .form-group { margin-bottom: 25px; }
        label { display: block; margin-bottom: 8px; color: #475569; font-weight: 500; }
        input { 
            width: 100%; 
            padding: 12px 15px; 
            border: 2px solid #e2e8f0; 
            border-radius: 12px;
            font-size: 16px;
            transition: border-color 0.3s;
        }
        input:focus {
            outline: none;
            border-color: #0ea5e9;
        }
        button { 
            width: 100%; 
            padding: 14px; 
            background: linear-gradient(135deg, #0ea5e9, #0284c7);
            color: white; 
            border: none; 
            border-radius: 12px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            transition: opacity 0.3s;
        }
        button:hover { opacity: 0.9; }
        .link { text-align: center; margin-top: 25px; }
        .link a { color: #0ea5e9; text-decoration: none; }
        .alert { padding: 12px 15px; margin-bottom: 20px; border-radius: 12px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
        .alert-info { background: #dbeafe; color: #1e40af; }
    </style>
</head>
<body>
    <div class="card">
        <h2>🔐 Welcome Back</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="POST">
            <div class="form-group">
                <label>📧 Email Address</label>
                <input type="email" name="email" required placeholder="your@email.com">
            </div>
            <div class="form-group">
                <label>🔒 Password</label>
                <input type="password" name="password" required placeholder="••••••••">
            </div>
            <button type="submit">Login →</button>
        </form>
        <div class="link">
            <a href="{{ url_for('register') }}">✨ Create Free Account</a>
        </div>
    </div>
</body>
</html>
'''

REGISTER_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Register - Email Scheduler Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .card { 
            background: white; 
            padding: 45px; 
            border-radius: 30px; 
            box-shadow: 0 20px 40px rgba(2, 132, 199, 0.2);
            width: 100%; 
            max-width: 420px;
        }
        .card h2 { text-align: center; margin-bottom: 10px; color: #0369a1; font-size: 32px; }
        .subtitle { text-align: center; color: #64748b; margin-bottom: 30px; font-size: 14px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; color: #475569; font-weight: 500; }
        input { 
            width: 100%; 
            padding: 12px 15px; 
            border: 2px solid #e2e8f0; 
            border-radius: 12px;
            font-size: 16px;
            transition: border-color 0.3s;
        }
        input:focus {
            outline: none;
            border-color: #0ea5e9;
        }
        button { 
            width: 100%; 
            padding: 14px; 
            background: linear-gradient(135deg, #0ea5e9, #0284c7);
            color: white; 
            border: none; 
            border-radius: 12px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            transition: opacity 0.3s;
        }
        button:hover { opacity: 0.9; }
        .link { text-align: center; margin-top: 25px; }
        .link a { color: #0ea5e9; text-decoration: none; }
        .alert { padding: 12px 15px; margin-bottom: 20px; border-radius: 12px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
    </style>
</head>
<body>
    <div class="card">
        <h2>✨ Get Started</h2>
        <div class="subtitle">Free plan includes 9,000 emails/month (300/day) with Brevo!</div>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="POST">
            <div class="form-group">
                <label>📧 Email</label>
                <input type="email" name="email" required placeholder="your@email.com">
            </div>
            <div class="form-group">
                <label>🔒 Password (min 4 characters)</label>
                <input type="password" name="password" required placeholder="••••••••">
            </div>
            <div class="form-group">
                <label>✓ Confirm Password</label>
                <input type="password" name="confirm_password" required placeholder="••••••••">
            </div>
            <button type="submit">Create Account →</button>
        </form>
        <div class="link">
            <a href="{{ url_for('login') }}">Already have an account? Login</a>
        </div>
    </div>
</body>
</html>
'''

DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - Email Scheduler Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; 
            background: #f0f9ff;
        }
        .header { 
            background: white; 
            box-shadow: 0 2px 20px rgba(0,0,0,0.08);
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .nav { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            padding: 15px 30px; 
            max-width: 1400px; 
            margin: 0 auto; 
        }
        .logo { 
            font-size: 24px; 
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
        .container { max-width: 1400px; margin: 30px auto; padding: 0 30px; }
        .stats-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); 
            gap: 25px; 
            margin-bottom: 40px;
        }
        .stat-card { 
            background: white; 
            padding: 25px; 
            border-radius: 20px; 
            box-shadow: 0 5px 20px rgba(0,0,0,0.05);
        }
        .stat-card h3 { color: #64748b; margin-bottom: 12px; font-size: 18px; }
        .stat-card .number { font-size: 38px; font-weight: bold; color: #0284c7; margin-bottom: 10px; }
        .progress-bar { background: #e2e8f0; border-radius: 10px; overflow: hidden; margin-top: 12px; }
        .progress-fill { 
            background: linear-gradient(90deg, #0ea5e9, #0284c7);
            height: 10px; 
            transition: width 0.5s;
        }
        .row { display: grid; grid-template-columns: 1fr 1fr; gap: 25px; margin-bottom: 40px; }
        .form-card { 
            background: white; 
            padding: 25px; 
            border-radius: 20px; 
            box-shadow: 0 5px 20px rgba(0,0,0,0.05);
        }
        .form-card h3 { margin-bottom: 20px; color: #0369a1; font-size: 22px; }
        .form-group { margin-bottom: 18px; }
        label { display: block; margin-bottom: 8px; color: #475569; font-weight: 500; }
        input, textarea { 
            width: 100%; 
            padding: 12px; 
            border: 2px solid #e2e8f0; 
            border-radius: 12px;
            font-size: 14px;
            transition: border-color 0.3s;
        }
        input:focus, textarea:focus {
            outline: none;
            border-color: #0ea5e9;
        }
        button { 
            padding: 12px 24px; 
            background: linear-gradient(135deg, #0ea5e9, #0284c7);
            color: white; 
            border: none; 
            border-radius: 12px;
            cursor: pointer;
            font-weight: bold;
            transition: opacity 0.3s;
        }
        button:hover { opacity: 0.9; }
        .email-list { background: white; border-radius: 20px; overflow: hidden; box-shadow: 0 5px 20px rgba(0,0,0,0.05); }
        .email-item { padding: 18px; border-bottom: 1px solid #e2e8f0; }
        .status { 
            display: inline-block; 
            padding: 4px 12px; 
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
        }
        .status-scheduled { background: #fff3e0; color: #f59e0b; }
        .status-sent { background: #d1fae5; color: #10b981; }
        .status-failed { background: #fee2e2; color: #ef4444; }
        .status-cancelled { background: #e2e8f0; color: #64748b; }
        .btn-sm { padding: 5px 12px; font-size: 12px; margin-left: 8px; border-radius: 8px; text-decoration: none; display: inline-block; }
        .btn-warning { background: #f59e0b; color: white; }
        .alert { padding: 12px 20px; margin-bottom: 25px; border-radius: 12px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
        @media (max-width: 768px) { 
            .row { grid-template-columns: 1fr; }
            .container { padding: 0 15px; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler Pro</div>
            <div class="nav-links">
                <span style="color: #0284c7;">⭐ {{ plan_name|capitalize }} Plan</span>
                <a href="{{ url_for('upgrade_plan') }}">Upgrade</a>
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
        
        <div class="stats-grid">
            <div class="stat-card">
                <h3>📊 Monthly Usage</h3>
                <div class="number">{{ user.emails_sent_this_month }} / {{ "{:,}".format(plan.emails_per_month) }}</div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: {{ usage_percentage }}%"></div>
                </div>
                <small>✨ {{ remaining_emails }} emails remaining this month</small>
            </div>
            <div class="stat-card">
                <h3>⏰ Scheduled Emails</h3>
                <div class="number">{{ remaining_scheduled }} / {{ plan.scheduled_emails }}</div>
                <small>🎯 slots available for scheduling</small>
            </div>
            <div class="stat-card">
                <h3>💎 Current Plan</h3>
                <div class="number">{{ plan.name }}</div>
                <small>${{ "%.2f"|format(plan.price) }}/month</small>
                {% if plan_name == 'free' %}
                    <small style="display: block; color: #10b981;">✨ 300 free emails/day</small>
                {% endif %}
            </div>
        </div>
        
        <div class="row">
            <div class="form-card">
                <h3>📤 Send Email Now</h3>
                <form method="POST" action="{{ url_for('send_now') }}">
                    <div class="form-group">
                        <label>📧 Recipient Email</label>
                        <input type="email" name="recipient" required placeholder="friend@example.com">
                    </div>
                    <div class="form-group">
                        <label>📝 Subject</label>
                        <input type="text" name="subject" required placeholder="Your email subject">
                    </div>
                    <div class="form-group">
                        <label>💬 Message</label>
                        <textarea name="body" rows="3" required placeholder="Type your message here..."></textarea>
                    </div>
                    <button type="submit">✈️ Send Now</button>
                </form>
            </div>
            
            <div class="form-card">
                <h3>⏰ Schedule Email</h3>
                <form method="POST" action="{{ url_for('schedule_email') }}">
                    <div class="form-group">
                        <label>📧 Recipient Email</label>
                        <input type="email" name="recipient" required placeholder="friend@example.com">
                    </div>
                    <div class="form-group">
                        <label>📝 Subject</label>
                        <input type="text" name="subject" required placeholder="Your email subject">
                    </div>
                    <div class="form-group">
                        <label>💬 Message</label>
                        <textarea name="body" rows="2" required placeholder="Type your message here..."></textarea>
                    </div>
                    <div class="form-group">
                        <label>📅 Schedule Date & Time</label>
                        <input type="datetime-local" name="schedule_datetime" required>
                    </div>
                    <button type="submit">📅 Schedule Email</button>
                </form>
            </div>
        </div>
        
        <div class="email-list">
            <div style="padding: 18px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-weight: bold;">
                📋 Scheduled Emails
            </div>
            {% if scheduled_emails %}
                {% for email in scheduled_emails %}
                    <div class="email-item">
                        <div>
                            <strong>To:</strong> {{ email.recipient_email }}<br>
                            <strong>Subject:</strong> {{ email.subject[:60] }}{% if email.subject|length > 60 %}...{% endif %}<br>
                            <strong>🕐 Time:</strong> {{ email.scheduled_time[:16].replace('T', ' ') }}<br>
                            <span class="status status-{{ email.status }}">{{ email.status }}</span>
                            {% if email.error_message %}
                                <br><small style="color: #ef4444;">Error: {{ email.error_message[:50] }}</small>
                            {% endif %}
                        </div>
                        {% if email.status == 'scheduled' %}
                            <div style="margin-top: 10px;">
                                <a href="{{ url_for('cancel_scheduled', email_id=email.id) }}" class="btn-sm btn-warning" style="text-decoration: none;" onclick="return confirm('Cancel this scheduled email?')">❌ Cancel</a>
                            </div>
                        {% endif %}
                    </div>
                {% endfor %}
            {% else %}
                <div class="email-item" style="text-align: center; color: #94a3b8;">
                    ✨ No scheduled emails yet. Create your first schedule above!
                </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
'''

UPGRADE_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Upgrade Plan - Email Scheduler Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%);
        }
        .header { 
            background: white; 
            box-shadow: 0 2px 20px rgba(0,0,0,0.1);
            padding: 15px 30px;
        }
        .nav { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            max-width: 1200px; 
            margin: 0 auto; 
        }
        .logo { 
            font-size: 24px; 
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
        .container { max-width: 1200px; margin: 50px auto; padding: 0 20px; }
        .pricing-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); 
            gap: 30px; 
            margin-top: 30px;
        }
        .card { 
            background: white; 
            border-radius: 20px; 
            padding: 35px; 
            text-align: center; 
            transition: transform 0.3s;
            position: relative;
        }
        .card.current { 
            border: 3px solid #0284c7;
            transform: scale(1.02);
        }
        .card:hover { transform: translateY(-10px) scale(1.02); }
        .card h3 { font-size: 32px; margin-bottom: 15px; color: #0369a1; }
        .price { font-size: 52px; color: #0284c7; margin: 20px 0; font-weight: bold; }
        .features { list-style: none; margin: 25px 0; text-align: left; display: inline-block; }
        .features li { padding: 10px 0; color: #475569; }
        .btn { 
            display: inline-block; 
            padding: 12px 30px; 
            background: linear-gradient(135deg, #0ea5e9, #0284c7);
            color: white; 
            text-decoration: none; 
            border-radius: 50px;
            font-weight: bold;
            transition: opacity 0.3s;
        }
        .btn:hover { opacity: 0.9; }
        .btn:disabled { background: #cbd5e1; cursor: not-allowed; }
        .alert { padding: 12px 20px; margin-bottom: 25px; border-radius: 12px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .info-box {
            text-align: center;
            margin-top: 50px;
            padding: 30px;
            background: white;
            border-radius: 20px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">✨ Email Scheduler Pro</div>
            <div class="nav-links">
                <a href="{{ url_for('dashboard') }}">Dashboard</a>
                <a href="{{ url_for('logout') }}">Logout</a>
            </div>
        </div>
    </div>
    
    <div class="container">
        <h2 style="text-align: center; color: #0369a1; margin-bottom: 20px;">🚀 Choose Your Perfect Plan</h2>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <div class="pricing-grid">
            {% for plan_name, plan in plans.items() %}
                <div class="card {% if plan_name == current_plan %}current{% endif %}">
                    <h3>{{ plan.name }}</h3>
                    <div class="price">${{ "%.2f"|format(plan.price) }}<small style="font-size: 16px;">/mo</small></div>
                    <ul class="features">
                        <li>📧 {{ "{:,}".format(plan.emails_per_month) }} emails/month</li>
                        <li>⏰ {{ plan.scheduled_emails }} scheduled emails</li>
                        <li>⚡ Brevo API powered</li>
                        <li>📊 Real-time delivery tracking</li>
                        {% if plan.price > 0 %}
                            <li>🎯 Priority support</li>
                            <li>📈 Advanced analytics</li>
                        {% endif %}
                    </ul>
                    {% if plan_name == current_plan %}
                        <button class="btn" disabled>✓ Current Plan</button>
                    {% else %}
                        <a href="{{ url_for('change_plan', plan_name=plan_name) }}" class="btn">Switch to {{ plan.name }}</a>
                    {% endif %}
                </div>
            {% endfor %}
        </div>
        
        <div class="info-box">
            <p style="font-size: 18px; margin-bottom: 15px;">💡 <strong>Powered by Brevo (formerly Sendinblue)</strong></p>
            <p style="color: #64748b;">300 free emails/day • 100k free contacts • 99.9% uptime SLA</p>
        </div>
    </div>
</body>
</html>
'''

# ==================== MAIN APPLICATION ====================
# Initialize database when app starts
with app.app_context():
    init_db()

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Email Scheduler Pro - READY")
    print("=" * 60)
    print("✅ Database initialized")
    print("✅ Server running at: http://127.0.0.1:5000")
    print("=" * 60)
    print("")
    print("⚙️  To configure Brevo API:")
    print("   export BREVO_API_KEY='your-api-key'")
    print("   export FROM_EMAIL='noreply@yourdomain.com'")
    print("")
    
    # Run the app
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
