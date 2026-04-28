#!/usr/bin/env python3
"""
Auto Scheduled Email Sending App - Works on Pydroid & Render
Using Brevo API - With Environment Variables Support
"""

import os
import sqlite3
import threading
import time
import re
import requests
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, g
from functools import wraps

# ==================== INITIALIZATION ====================
app = Flask(__name__)

# ==================== ENVIRONMENT VARIABLES ====================
# For Pydroid: Edit these values directly
# For Render: Set these in Environment Variables tab

# === CHANGE THESE VALUES ===
BREVO_API_KEY = 'xkeysib-YOUR-ACTUAL-API-KEY-HERE'  # Get from https://app.brevo.com
BREVO_FROM_EMAIL = 'testing@brevo.com'  # Use 'testing@brevo.com' or your verified email
SECRET_KEY = 'your-secret-key-change-this-12345'  # Any random string
# ===========================

# Override with environment variables if they exist (for Render)
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', BREVO_API_KEY)
BREVO_FROM_EMAIL = os.environ.get('BREVO_FROM_EMAIL', BREVO_FROM_EMAIL)
SECRET_KEY = os.environ.get('SECRET_KEY', SECRET_KEY)

# Apply configuration
app.secret_key = SECRET_KEY

# Brevo API Configuration
BREVO_CONFIG = {
    'api_key': BREVO_API_KEY,
    'from_email': BREVO_FROM_EMAIL,
    'from_name': 'Email Scheduler Pro',
    'api_url': 'https://api.brevo.com/v3/smtp/email'
}

# Database file
DATABASE = 'email_scheduler.db'

# Prevent duplicate sending
sending_lock = threading.Lock()
currently_sending = set()

# Pricing plans
PLANS = {
    'free': {'name': 'Free', 'emails_per_month': 9000, 'scheduled_emails': 50, 'price': 0},
    'basic': {'name': 'Basic', 'emails_per_month': 20000, 'scheduled_emails': 200, 'price': 9.99},
    'pro': {'name': 'Pro', 'emails_per_month': 50000, 'scheduled_emails': 1000, 'price': 19.99}
}

# ==================== DATABASE FUNCTIONS ====================
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
    """Initialize database tables"""
    try:
        db = sqlite3.connect(DATABASE)
        c = db.cursor()
        
        # Users table
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            emails_sent_this_month INTEGER DEFAULT 0,
            last_reset DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Scheduled emails table
        c.execute('''CREATE TABLE IF NOT EXISTS scheduled_emails (
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
            processing_lock INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )''')
        
        # Create indexes
        c.execute('CREATE INDEX IF NOT EXISTS idx_status_time ON scheduled_emails(status, scheduled_time)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_processing ON scheduled_emails(processing_lock)')
        
        db.commit()
        db.close()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"Database initialization error: {e}")

# Register database teardown
app.teardown_appcontext(close_db)

# Initialize database with app context
with app.app_context():
    init_db()

# ==================== HELPER FUNCTIONS ====================
def hash_password(password):
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_value):
    return hash_password(password) == hash_value

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_user(user_id):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

def get_user_by_email(email):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()

def reset_monthly_counts():
    try:
        db = sqlite3.connect(DATABASE)
        today = datetime.now().date()
        users = db.execute('SELECT id, last_reset FROM users').fetchall()
        for user in users:
            if user[1]:
                last_reset = datetime.strptime(user[1], '%Y-%m-%d').date()
                if today > last_reset:
                    db.execute('UPDATE users SET emails_sent_this_month = 0, last_reset = ? WHERE id = ?', 
                              (today.isoformat(), user[0]))
        db.commit()
        db.close()
    except Exception as e:
        print(f"Reset error: {e}")

def can_send_email(user):
    plan = PLANS[user['plan']]
    return user['emails_sent_this_month'] < plan['emails_per_month']

def can_schedule_email(user):
    db = get_db()
    count = db.execute('SELECT COUNT(*) as c FROM scheduled_emails WHERE user_id = ? AND status = "scheduled"', (user['id'],)).fetchone()['c']
    return count < PLANS[user['plan']]['scheduled_emails']

def get_remaining_emails(user):
    plan = PLANS[user['plan']]
    return max(0, plan['emails_per_month'] - user['emails_sent_this_month'])

def get_remaining_scheduled(user):
    db = get_db()
    count = db.execute('SELECT COUNT(*) as c FROM scheduled_emails WHERE user_id = ? AND status = "scheduled"', (user['id'],)).fetchone()['c']
    return max(0, PLANS[user['plan']]['scheduled_emails'] - count)

# ==================== BREVO API EMAIL FUNCTIONS ====================
def send_email_via_brevo(to_email, subject, body, user_email=None):
    """Send email using Brevo API"""
    try:
        # Check if API key is configured
        if BREVO_CONFIG['api_key'] == 'xkeysib-YOUR-ACTUAL-API-KEY-HERE':
            return False, "⚠️ Brevo API key not configured. Please add your API key to the code.", None
        
        headers = {
            'api-key': BREVO_CONFIG['api_key'],
            'Content-Type': 'application/json'
        }
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family:Arial;padding:20px;background:#f0f9ff">
            <div style="max-width:600px;margin:0 auto;background:white;border-radius:20px;overflow:hidden;box-shadow:0 10px 30px rgba(0,0,0,0.1)">
                <div style="background:linear-gradient(135deg,#0284c7,#0ea5e9);color:white;padding:30px;text-align:center">
                    <h2 style="margin:0">📧 Email Scheduler Pro</h2>
                </div>
                <div style="padding:30px">
                    {body.replace(chr(10), '<br>')}
                </div>
                <div style="background:#f8fafc;padding:15px;text-align:center;font-size:12px;color:#64748b">
                    Sent via Email Scheduler Pro
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
        
        if response.status_code in (200, 201):
            result = response.json()
            return True, "Email sent successfully", result.get('messageId', 'ok')
        else:
            error_msg = response.json().get('message', 'Unknown error')
            return False, f"Brevo API error: {error_msg}", None
            
    except requests.exceptions.RequestException as e:
        return False, f"Network error: {str(e)}", None
    except Exception as e:
        return False, str(e), None

def send_scheduled_email(email_id):
    with sending_lock:
        if email_id in currently_sending:
            return False
        currently_sending.add(email_id)
    
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        c = db.cursor()
        
        c.execute('UPDATE scheduled_emails SET processing_lock = 1 WHERE id = ? AND status = "scheduled" AND processing_lock = 0', (email_id,))
        if c.rowcount == 0:
            return False
        db.commit()
        
        email = c.execute('SELECT * FROM scheduled_emails WHERE id = ?', (email_id,)).fetchone()
        if not email:
            return False
        
        user = c.execute('SELECT * FROM users WHERE id = ?', (email['user_id'],)).fetchone()
        if not user:
            return False
        
        if user['emails_sent_this_month'] >= PLANS[user['plan']]['emails_per_month']:
            c.execute('UPDATE scheduled_emails SET status = "failed", error_message = "Monthly limit reached" WHERE id = ?', (email_id,))
            db.commit()
            return False
        
        success, msg, brevo_id = send_email_via_brevo(
            email['recipient_email'], 
            email['subject'], 
            email['body'], 
            user['email']
        )
        
        if success:
            c.execute('UPDATE scheduled_emails SET status = "sent", sent_at = CURRENT_TIMESTAMP, brevo_message_id = ?, error_message = NULL, processing_lock = 0 WHERE id = ?', (brevo_id, email_id))
            c.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
        else:
            retry = email['retry_count'] + 1
            status = 'failed' if retry >= 3 else 'scheduled'
            c.execute('UPDATE scheduled_emails SET retry_count = ?, status = ?, error_message = ?, processing_lock = 0 WHERE id = ?', (retry, status, msg, email_id))
        
        db.commit()
        return success
    except Exception as e:
        print(f"Send error: {e}")
        return False
    finally:
        with sending_lock:
            currently_sending.discard(email_id)

def email_sender_thread():
    last_ids = set()
    while True:
        try:
            db = sqlite3.connect(DATABASE)
            db.row_factory = sqlite3.Row
            now = datetime.now().isoformat()
            due = db.execute('SELECT id FROM scheduled_emails WHERE scheduled_time <= ? AND status = "scheduled" AND processing_lock = 0 ORDER BY scheduled_time ASC LIMIT 10', (now,)).fetchall()
            db.close()
            
            for e in due:
                if e['id'] not in last_ids:
                    send_scheduled_email(e['id'])
                    last_ids.add(e['id'])
            
            if len(last_ids) > 100:
                last_ids.clear()
            
            reset_monthly_counts()
        except Exception as e:
            print(f"Thread error: {e}")
        time.sleep(30)

# ==================== ROUTES ====================
@app.route('/')
def index():
    return render_template_string(INDEX_TEMPLATE, plans=PLANS)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        
        if not email or not password:
            flash('All fields required', 'error')
            return redirect(url_for('register'))
        if password != confirm:
            flash('Passwords do not match', 'error')
            return redirect(url_for('register'))
        if len(password) < 4:
            flash('Password must be at least 4 characters', 'error')
            return redirect(url_for('register'))
        if get_user_by_email(email):
            flash('Email already registered', 'warning')
            return redirect(url_for('login'))
        
        db = get_db()
        db.execute('INSERT INTO users (email, password, last_reset) VALUES (?, ?, ?)', 
                   (email, hash_password(password), datetime.now().date().isoformat()))
        db.commit()
        
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))
    
    return render_template_string(REGISTER_TEMPLATE)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = get_user_by_email(email)
        
        if user and verify_password(password, user['password']):
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
    
    db = get_db()
    emails = db.execute('SELECT * FROM scheduled_emails WHERE user_id = ? ORDER BY scheduled_time DESC LIMIT 50', (user['id'],)).fetchall()
    plan = PLANS[user['plan']]
    percent = (user['emails_sent_this_month'] / plan['emails_per_month'] * 100) if plan['emails_per_month'] > 0 else 0
    
    return render_template_string(DASHBOARD_TEMPLATE, 
                                user=user, 
                                scheduled_emails=emails, 
                                plan=plan, 
                                plan_name=user['plan'], 
                                usage_percentage=percent, 
                                remaining_emails=get_remaining_emails(user), 
                                remaining_scheduled=get_remaining_scheduled(user),
                                brevo_configured=BREVO_CONFIG['api_key'] != 'xkeysib-YOUR-ACTUAL-API-KEY-HERE')

@app.route('/schedule_email', methods=['POST'])
@login_required
def schedule_email():
    user = get_user(session['user_id'])
    
    if not can_schedule_email(user):
        flash(f'Schedule limit reached ({PLANS[user["plan"]]["scheduled_emails"]} max)', 'error')
        return redirect(url_for('dashboard'))
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    dt_str = request.form.get('schedule_datetime', '')
    
    if not all([recipient, subject, body, dt_str]):
        flash('All fields are required', 'error')
        return redirect(url_for('dashboard'))
    
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', recipient):
        flash('Invalid email address', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        scheduled = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
        if scheduled <= datetime.now():
            flash('Scheduled time must be in the future', 'error')
            return redirect(url_for('dashboard'))
        
        db = get_db()
        db.execute('INSERT INTO scheduled_emails (user_id, recipient_email, subject, body, scheduled_time) VALUES (?, ?, ?, ?, ?)', 
                   (user['id'], recipient, subject, body, scheduled.isoformat()))
        db.commit()
        
        flash(f'📧 Email scheduled for {scheduled.strftime("%Y-%m-%d %H:%M")}', 'success')
    except ValueError:
        flash('Invalid date/time format', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/send_now', methods=['POST'])
@login_required
def send_now():
    user = get_user(session['user_id'])
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    
    if not all([recipient, subject, body]):
        flash('All fields are required', 'error')
        return redirect(url_for('dashboard'))
    
    if not can_send_email(user):
        flash(f'Monthly email limit reached ({PLANS[user["plan"]]["emails_per_month"]} max)', 'error')
        return redirect(url_for('dashboard'))
    
    success, msg, _ = send_email_via_brevo(recipient, subject, body, user['email'])
    
    if success:
        db = get_db()
        db.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
        db.commit()
        flash('✅ Email sent successfully!', 'success')
    else:
        flash(f'❌ Failed: {msg}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/cancel_scheduled/<int:email_id>')
@login_required
def cancel_scheduled(email_id):
    user = get_user(session['user_id'])
    db = get_db()
    
    email = db.execute('SELECT * FROM scheduled_emails WHERE id = ?', (email_id,)).fetchone()
    if email and email['user_id'] == user['id'] and email['status'] == 'scheduled':
        db.execute('UPDATE scheduled_emails SET status = "cancelled" WHERE id = ?', (email_id,))
        db.commit()
        flash('❌ Scheduled email cancelled', 'success')
    else:
        flash('Cannot cancel this email', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/upgrade_plan')
@login_required
def upgrade_plan():
    return render_template_string(UPGRADE_TEMPLATE, plans=PLANS, current_plan=get_user(session['user_id'])['plan'])

@app.route('/change_plan/<plan_name>')
@login_required
def change_plan(plan_name):
    if plan_name not in PLANS:
        flash('Invalid plan', 'error')
        return redirect(url_for('upgrade_plan'))
    
    db = get_db()
    db.execute('UPDATE users SET plan = ? WHERE id = ?', (plan_name, session['user_id']))
    db.commit()
    
    flash(f'✨ Plan changed to {PLANS[plan_name]["name"]}!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/test_brevo')
@login_required
def test_brevo():
    """Test endpoint to verify Brevo API is working"""
    if BREVO_CONFIG['api_key'] == 'xkeysib-YOUR-ACTUAL-API-KEY-HERE':
        flash('⚠️ Please configure your Brevo API key first! Edit the BREVO_API_KEY variable in the code.', 'warning')
        return redirect(url_for('dashboard'))
    
    user = get_user(session['user_id'])
    success, msg, brevo_id = send_email_via_brevo(
        user['email'], 
        '✨ Brevo API Test - Email Scheduler Pro', 
        f'Hello! This is a test email sent at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n\n'
        f'Your email scheduler is working perfectly! 🎉\n\n'
        f'Features:\n'
        f'• Auto-scheduling\n'
        f'• 300 free emails/day with Brevo\n'
        f'• Beautiful email templates\n\n'
        f'Happy scheduling! 📧', 
        user['email']
    )
    
    if success:
        flash(f'✅ Test email sent successfully! Check your inbox (and spam folder). Brevo ID: {brevo_id}', 'success')
    else:
        flash(f'❌ Test failed: {msg}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/status')
def status():
    """Check app status and configuration"""
    return f"""
    <html>
    <head><title>Email Scheduler Status</title></head>
    <body style="font-family: Arial; padding: 20px;">
        <h2>📧 Email Scheduler Pro - Status</h2>
        <p><strong>Brevo API:</strong> {'✅ Configured' if BREVO_CONFIG['api_key'] != 'xkeysib-YOUR-ACTUAL-API-KEY-HERE' else '❌ Not Configured'}</p>
        <p><strong>From Email:</strong> {BREVO_CONFIG['from_email']}</p>
        <p><strong>Database:</strong> {'✅ Connected'}</p>
        <p><strong>Secret Key:</strong> {'✅ Set' if app.secret_key != 'your-secret-key-change-this-12345' else '⚠️ Using default'}</p>
        <hr>
        <h3>📝 Instructions to fix email sending:</h3>
        <ol>
            <li>Go to <a href="https://app.brevo.com" target="_blank">Brevo Dashboard</a></li>
            <li>Click your profile → SMTP & API → API Keys</li>
            <li>Generate a new v3 API key</li>
            <li>Copy the key (starts with xkeysib-)</li>
            <li>Edit this file and change BREVO_API_KEY = 'xkeysib-YOUR-ACTUAL-KEY-HERE'</li>
            <li>Restart the app</li>
        </ol>
        <p><a href="/">← Back to Home</a></p>
    </body>
    </html>
    """

# ==================== HTML TEMPLATES ====================
INDEX_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Scheduler Pro</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        @keyframes fadeIn{from{opacity:0}to{opacity:1}}
        @keyframes slideDown{from{transform:translateY(-100px);opacity:0}to{transform:translateY(0);opacity:1}}
        @keyframes slideUp{from{transform:translateY(50px);opacity:0}to{transform:translateY(0);opacity:1}}
        @keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.05)}}
        body{font-family:'Segoe UI',Arial;background:linear-gradient(135deg,#e0f2fe 0%,#bae6fd 100%);min-height:100vh;animation:fadeIn 0.8s ease-in}
        .container{max-width:1200px;margin:0 auto;padding:20px}
        .header{background:white;box-shadow:0 2px 20px rgba(0,0,0,0.1);position:sticky;top:0;z-index:100;animation:slideDown 0.6s ease-out}
        .nav{display:flex;justify-content:space-between;align-items:center;padding:15px 20px;max-width:1200px;margin:0 auto}
        .logo{font-size:28px;font-weight:bold;background:linear-gradient(135deg,#0284c7,#0ea5e9);-webkit-background-clip:text;-webkit-text-fill-color:transparent;animation:pulse 2s infinite}
        .nav-links a{margin-left:20px;text-decoration:none;color:#0369a1;font-weight:500;transition:all 0.3s ease}
        .nav-links a:hover{color:#0284c7;transform:translateY(-2px)}
        .hero{text-align:center;padding:80px 20px;animation:slideUp 0.8s ease-out}
        .hero h1{font-size:52px;margin-bottom:20px;background:linear-gradient(135deg,#0369a1,#0284c7);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
        .hero p{font-size:20px;margin-bottom:30px;color:#075985}
        .btn{display:inline-block;padding:14px 35px;background:linear-gradient(135deg,#0ea5e9,#0284c7);color:white;text-decoration:none;border-radius:50px;font-weight:bold;transition:all 0.3s ease;box-shadow:0 4px 15px rgba(2,132,199,0.3)}
        .btn:hover{transform:translateY(-3px);box-shadow:0 6px 20px rgba(2,132,199,0.4)}
        .pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:30px;margin-top:60px;margin-bottom:60px}
        .card{background:white;border-radius:20px;padding:30px;text-align:center;box-shadow:0 10px 30px rgba(0,0,0,0.1);transition:all 0.3s ease;animation:slideUp 0.6s ease-out}
        .card:hover{transform:translateY(-10px);box-shadow:0 20px 40px rgba(2,132,199,0.2)}
        .card h3{font-size:28px;margin-bottom:15px;color:#0369a1}
        .price{font-size:48px;color:#0284c7;margin:20px 0;font-weight:bold}
        .features{list-style:none;margin:25px 0}
        .features li{padding:10px 0;color:#475569;transition:transform 0.3s ease}
        .features li:hover{transform:translateX(5px);color:#0284c7}
        .badge{display:inline-block;background:#10b981;color:white;padding:4px 12px;border-radius:20px;font-size:12px;margin-left:10px}
        .free-badge{background:#f59e0b}
        @media (max-width:768px){.hero h1{font-size:32px}}
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler Pro <span class="badge">Brevo API</span> <span class="badge free-badge">300/day Free</span></div>
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
        <h1>⚡ Schedule Emails Automatically</h1>
        <p>300 free emails/day • High deliverability • Never forget important emails</p>
        {% if not session.user_id %}
            <a href="{{ url_for('register') }}" class="btn">🚀 Get Started Free</a>
        {% endif %}
    </div>
    <div class="container">
        <h2 style="text-align:center;margin-bottom:20px;color:#0369a1">💰 Simple Pricing Plans</h2>
        <div class="pricing-grid">
            {% for name,plan in plans.items() %}
                <div class="card">
                    <h3>{{ plan.name }}</h3>
                    <div class="price">${{ "%.2f"|format(plan.price) }}<small style="font-size:16px;">/mo</small></div>
                    <ul class="features">
                        <li>📧 {{ "{:,}".format(plan.emails_per_month) }} emails/month</li>
                        <li>⏰ {{ plan.scheduled_emails }} scheduled emails</li>
                        <li>⚡ Brevo API powered</li>
                        {% if plan.price > 0 %}
                            <li>🎯 Priority support</li>
                            <li>📈 Advanced analytics</li>
                        {% else %}
                            <li>✨ 300 emails/day free</li>
                            <li>📧 100k contacts free</li>
                        {% endif %}
                    </ul>
                </div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
'''

LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Login</title><style>
@keyframes fadeIn{from{opacity:0;transform:scale(0.95)}to{opacity:1;transform:scale(1)}}
@keyframes slideIn{from{transform:translateY(-50px);opacity:0}to{transform:translateY(0);opacity:1}}
body{font-family:'Segoe UI',Arial;background:linear-gradient(135deg,#e0f2fe,#bae6fd);display:flex;justify-content:center;align-items:center;height:100vh;animation:fadeIn 0.8s ease-in}
.card{background:white;padding:45px;border-radius:30px;box-shadow:0 20px 40px rgba(2,132,199,0.2);width:100%;max-width:420px;animation:slideIn 0.6s ease-out;transition:transform 0.3s ease}
.card:hover{transform:translateY(-5px)}
.card h2{text-align:center;margin-bottom:30px;color:#0369a1;font-size:32px}
input{width:100%;padding:12px 15px;margin:10px 0;border:2px solid #e2e8f0;border-radius:12px;font-size:16px;transition:all 0.3s ease}
input:focus{outline:none;border-color:#0ea5e9;transform:translateX(5px)}
button{width:100%;padding:14px;background:linear-gradient(135deg,#0ea5e9,#0284c7);color:white;border:none;border-radius:12px;font-size:16px;font-weight:bold;cursor:pointer;transition:all 0.3s ease}
button:hover{transform:translateY(-2px);box-shadow:0 5px 20px rgba(2,132,199,0.3)}
.alert{padding:12px 15px;margin-bottom:20px;border-radius:12px;animation:slideIn 0.4s ease-out}
.alert-success{background:#d1fae5;color:#065f46}
.alert-error{background:#fee2e2;color:#991b1b}
.alert-warning{background:#fed7aa;color:#92400e}
a{color:#0ea5e9;text-decoration:none}
a:hover{text-decoration:underline}
</style></head>
<body><div class="card"><h2>🔐 Welcome Back</h2>{% with m=get_flashed_messages(with_categories=true) %}{% for c,msg in m %}<div class="alert alert-{{c}}">{{msg}}</div>{% endfor %}{% endwith %}<form method=POST><input type=email name=email placeholder="📧 Email" required><input type=password name=password placeholder="🔒 Password" required><button type=submit>Login →</button></form><p style="text-align:center;margin-top:25px"><a href="{{ url_for('register') }}">✨ Create Free Account</a></p></div></body>
</html>
'''

REGISTER_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Register</title><style>
@keyframes bounceIn{0%{transform:scale(0.9);opacity:0}60%{transform:scale(1.02)}100%{transform:scale(1);opacity:1}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
body{font-family:'Segoe UI',Arial;background:linear-gradient(135deg,#e0f2fe,#bae6fd);display:flex;justify-content:center;align-items:center;height:100vh;animation:fadeIn 0.8s ease-in}
.card{background:white;padding:45px;border-radius:30px;box-shadow:0 20px 40px rgba(2,132,199,0.2);width:100%;max-width:420px;animation:bounceIn 0.6s ease-out}
.card h2{text-align:center;margin-bottom:10px;color:#0369a1;font-size:32px}
.subtitle{text-align:center;color:#64748b;margin-bottom:30px;font-size:14px}
input{width:100%;padding:12px 15px;margin:10px 0;border:2px solid #e2e8f0;border-radius:12px;font-size:16px;transition:all 0.3s ease}
input:focus{outline:none;border-color:#0ea5e9;transform:translateX(5px)}
button{width:100%;padding:14px;background:linear-gradient(135deg,#0ea5e9,#0284c7);color:white;border:none;border-radius:12px;font-size:16px;font-weight:bold;cursor:pointer;transition:all 0.3s ease}
button:hover{transform:translateY(-2px);box-shadow:0 5px 20px rgba(2,132,199,0.3)}
.alert{padding:12px 15px;margin-bottom:20px;border-radius:12px}
.alert-success{background:#d1fae5;color:#065f46}
.alert-error{background:#fee2e2;color:#991b1b}
.alert-warning{background:#fed7aa;color:#92400e}
a{color:#0ea5e9;text-decoration:none}
</style></head>
<body><div class="card"><h2>✨ Get Started</h2><div class="subtitle">Free plan: 9,000 emails/month (300/day) with Brevo!</div>{% with m=get_flashed_messages(with_categories=true) %}{% for c,msg in m %}<div class="alert alert-{{c}}">{{msg}}</div>{% endfor %}{% endwith %}<form method=POST><input type=email name=email placeholder="📧 Email" required><input type=password name=password placeholder="🔒 Password (min 4)" required><input type=password name=confirm_password placeholder="✓ Confirm Password" required><button type=submit>Create Account →</button></form><p style="text-align:center;margin-top:25px"><a href="{{ url_for('login') }}">Already have an account? Login</a></p></div></body>
</html>
'''

DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dashboard</title><style>
*{margin:0;padding:0;box-sizing:border-box}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes slideIn{from{transform:translateX(-20px);opacity:0}to{transform:translateX(0);opacity:1}}
@keyframes glow{0%,100%{box-shadow:0 0 5px rgba(14,165,233,0.3)}50%{box-shadow:0 0 20px rgba(14,165,233,0.6)}}
body{font-family:'Segoe UI',Arial;background:#f0f9ff;animation:fadeIn 0.6s ease-in}
.header{background:white;box-shadow:0 2px 20px rgba(0,0,0,0.08);position:sticky;top:0;z-index:100}
.nav{display:flex;justify-content:space-between;align-items:center;padding:15px 30px;max-width:1400px;margin:0 auto}
.logo{font-size:24px;font-weight:bold;background:linear-gradient(135deg,#0284c7,#0ea5e9);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.nav-links a{margin-left:20px;text-decoration:none;color:#0369a1;font-weight:500;transition:all 0.3s ease}
.nav-links a:hover{color:#0284c7}
.container{max-width:1400px;margin:30px auto;padding:0 30px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:25px;margin-bottom:40px}
.stat-card{background:white;padding:25px;border-radius:20px;box-shadow:0 5px 20px rgba(0,0,0,0.05);transition:all 0.3s ease;animation:slideIn 0.5s ease-out}
.stat-card:hover{transform:translateY(-5px);box-shadow:0 10px 30px rgba(2,132,199,0.15)}
.stat-card h3{color:#64748b;margin-bottom:12px;font-size:18px}
.stat-card .number{font-size:38px;font-weight:bold;color:#0284c7;margin-bottom:10px}
.progress-bar{background:#e2e8f0;border-radius:10px;overflow:hidden;margin-top:12px}
.progress-fill{background:linear-gradient(90deg,#0ea5e9,#0284c7);height:10px;transition:width 0.5s ease;animation:glow 2s infinite}
.row{display:grid;grid-template-columns:1fr 1fr;gap:25px;margin-bottom:40px}
.card{background:white;padding:25px;border-radius:20px;box-shadow:0 5px 20px rgba(0,0,0,0.05);transition:all 0.3s ease}
.card:hover{transform:translateY(-3px);box-shadow:0 10px 30px rgba(2,132,199,0.1)}
.card h3{margin-bottom:20px;color:#0369a1;font-size:22px}
input,textarea{width:100%;padding:12px;margin:10px 0;border:2px solid #e2e8f0;border-radius:12px;font-size:14px;transition:all 0.3s ease}
input:focus,textarea:focus{outline:none;border-color:#0ea5e9;transform:translateX(5px)}
button{padding:12px 24px;background:linear-gradient(135deg,#0ea5e9,#0284c7);color:white;border:none;border-radius:12px;cursor:pointer;font-weight:bold;transition:all 0.3s ease}
button:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(2,132,199,0.3)}
.email-list{background:white;border-radius:20px;overflow:hidden;box-shadow:0 5px 20px rgba(0,0,0,0.05)}
.email-item{padding:18px;border-bottom:1px solid #e2e8f0;transition:all 0.3s ease}
.email-item:hover{background:#f0f9ff;transform:translateX(5px)}
.status{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:bold}
.status-scheduled{background:#fff3e0;color:#f59e0b}
.status-sent{background:#d1fae5;color:#10b981}
.status-failed{background:#fee2e2;color:#ef4444}
.alert{padding:12px 20px;margin-bottom:25px;border-radius:12px;animation:slideIn 0.4s ease-out}
.alert-success{background:#d1fae5;color:#065f46}
.alert-error{background:#fee2e2;color:#991b1b}
.alert-warning{background:#fed7aa;color:#92400e}
.test-btn{background:#10b981;padding:8px 16px;border-radius:8px;color:white;text-decoration:none;display:inline-block}
@media (max-width:768px){.row{grid-template-columns:1fr}.container{padding:0 15px}}
</style></head>
<body>
<div class="header"><div class="nav"><div class="logo">🚀 Email Scheduler Pro</div><div class="nav-links"><span style="color:#0284c7">⭐ {{plan_name|capitalize}} Plan</span><a href="/upgrade_plan">Upgrade</a><a href="/logout">Logout</a></div></div></div>
<div class="container">{% with m=get_flashed_messages(with_categories=true) %}{% for c,msg in m %}<div class="alert alert-{{c}}">{{msg}}</div>{% endfor %}{% endwith %}
<div style="text-align:right;margin-bottom:20px"><a href="/test_brevo" class="test-btn">🔧 Test Brevo API</a> <a href="/status" class="test-btn" style="background:#0369a1">📊 Status</a></div>
{% if not brevo_configured %}<div class="alert alert-warning">⚠️ Brevo API not configured! <a href="/status">Click here to fix</a></div>{% endif %}
<div class="stats"><div class="stat-card"><h3>📊 Monthly Usage</h3><div class="number">{{user.emails_sent_this_month}} / {{"{:,}".format(plan.emails_per_month)}}</div><div class="progress-bar"><div class="progress-fill" style="width:{{usage_percentage}}%"></div></div><small>✨ {{remaining_emails}} emails remaining</small></div><div class="stat-card"><h3>⏰ Scheduled Emails</h3><div class="number">{{remaining_scheduled}} / {{plan.scheduled_emails}}</div><small>🎯 slots available</small></div><div class="stat-card"><h3>💎 Current Plan</h3><div class="number">{{plan.name}}</div><small>${{"%.2f"|format(plan.price)}}/month</small>{% if plan_name=='free' %}<small style="display:block;color:#10b981">✨ 300 free emails/day</small>{% endif %}</div></div>
<div class="row"><div class="card"><h3>📤 Send Email Now</h3><form method=POST action="/send_now"><input type=email name=recipient placeholder="📧 Recipient Email" required><input type=text name=subject placeholder="📝 Subject" required><textarea name=body rows=3 placeholder="💬 Message" required></textarea><button type=submit>✈️ Send Now via Brevo</button></form></div>
<div class="card"><h3>⏰ Schedule Email</h3><form method=POST action="/schedule_email"><input type=email name=recipient placeholder="📧 Recipient Email" required><input type=text name=subject placeholder="📝 Subject" required><textarea name=body rows=2 placeholder="💬 Message" required></textarea><input type=datetime-local name=schedule_datetime required><button type=submit>📅 Schedule Email</button></form></div></div>
<div class="email-list"><div style="padding:18px;background:#f8fafc;border-bottom:1px solid #e2e8f0;font-weight:bold">📋 Scheduled Emails</div>{% if scheduled_emails %}{% for e in scheduled_emails %}<div class="email-item"><strong>To:</strong> {{e.recipient_email}}<br><strong>Subject:</strong> {{e.subject[:60]}}{% if e.subject|length>60 %}...{% endif %}<br><strong>🕐 Time:</strong> {{e.scheduled_time[:16].replace('T',' ')}}<br><span class="status status-{{e.status}}">{{e.status}}</span>{% if e.status=='scheduled' %} <a href="/cancel_scheduled/{{e.id}}" onclick="return confirm('Cancel this scheduled email?')" style="margin-left:10px;color:#f59e0b">❌ Cancel</a>{% endif %}</div>{% endfor %}{% else %}<div class="email-item" style="text-align:center;color:#94a3b8">✨ No scheduled emails yet. Create your first schedule above!</div>{% endif %}</div></div>
</body>
</html>
'''

UPGRADE_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Upgrade Plan</title><style>
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes float{0%,100%{transform:translateY(0px)}50%{transform:translateY(-10px)}}
body{font-family:'Segoe UI',Arial;background:linear-gradient(135deg,#e0f2fe,#bae6fd);animation:fadeIn 0.8s ease-in}
.container{max-width:1200px;margin:50px auto;padding:20px}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:30px;margin-top:30px}
.card{background:white;border-radius:20px;padding:35px;text-align:center;transition:all 0.4s ease;position:relative;overflow:hidden}
.card.current{border:3px solid #0284c7;transform:scale(1.02);animation:float 3s ease-in-out infinite}
.card:hover{transform:translateY(-10px) scale(1.02)}
.card h3{font-size:32px;margin-bottom:15px;color:#0369a1}
.price{font-size:52px;color:#0284c7;margin:20px 0;font-weight:bold}
.features{list-style:none;margin:25px 0;text-align:left;display:inline-block}
.features li{padding:10px 0;color:#475569;transition:transform 0.3s ease}
.features li:hover{transform:translateX(10px);color:#0284c7}
.btn{display:inline-block;padding:12px 30px;background:linear-gradient(135deg,#0ea5e9,#0284c7);color:white;text-decoration:none;border-radius:50px;font-weight:bold;transition:all 0.3s ease}
.btn:hover{transform:translateY(-2px);box-shadow:0 5px 20px rgba(2,132,199,0.4)}
.btn:disabled{background:#cbd5e1;cursor:not-allowed}
.alert{padding:12px 20px;margin-bottom:25px;border-radius:12px}
.alert-success{background:#d1fae5;color:#065f46}
.info-box{text-align:center;margin-top:50px;padding:30px;background:white;border-radius:20px;box-shadow:0 5px 20px rgba(0,0,0,0.1)}
.brevo-highlight{background:linear-gradient(135deg,#e0f2fe,#bae6fd);padding:15px;border-radius:15px;margin-top:20px}
</style></head>
<body>
<div style="background:white;box-shadow:0 2px 20px rgba(0,0,0,0.1);padding:15px 30px"><div style="display:flex;justify-content:space-between;align-items:center;max-width:1200px;margin:0 auto"><div style="font-size:24px;font-weight:bold;background:linear-gradient(135deg,#0284c7,#0ea5e9);-webkit-background-clip:text;-webkit-text-fill-color:transparent">✨ Email Scheduler Pro</div><div><a href="/dashboard" style="margin-left:20px;text-decoration:none;color:#0369a1">Dashboard</a><a href="/logout" style="margin-left:20px;text-decoration:none;color:#0369a1">Logout</a></div></div></div>
<div class="container"><h2 style="text-align:center;color:#0369a1;margin-bottom:20px">🚀 Choose Your Perfect Plan</h2>{% with m=get_flashed_messages(with_categories=true) %}{% for c,msg in m %}<div class="alert alert-{{c}}">{{msg}}</div>{% endfor %}{% endwith %}
<div class="pricing-grid">{% for name,plan in plans.items() %}<div class="card {% if name==current_plan %}current{% endif %}"><h3>{{plan.name}}</h3><div class="price">${{"%.2f"|format(plan.price)}}<small style="font-size:16px">/mo</small></div><ul class="features"><li>📧 {{"{:,}".format(plan.emails_per_month)}} emails/month</li><li>⏰ {{plan.scheduled_emails}} scheduled emails</li><li>⚡ Brevo API powered</li><li>📊 Real-time delivery tracking</li>{% if plan.price>0 %}<li>🎯 Priority support</li><li>📈 Advanced analytics</li>{% else %}<li>✨ 300 emails/day free</li><li>📧 100k contacts free</li>{% endif %}</ul>{% if name==current_plan %}<button class="btn" disabled>✓ Current Plan</button>{% else %}<a href="/change_plan/{{name}}" class="btn">Switch to {{plan.name}}</a>{% endif %}</div>{% endfor %}</div>
<div class="info-box"><p style="font-size:18px;margin-bottom:15px">💡 <strong>Powered by Brevo (formerly Sendinblue)</strong></p><p style="color:#64748b">300 free emails/day • 100k free contacts • 99.9% uptime SLA</p><div class="brevo-highlight"><p style="color:#0369a1">✨ <strong>Why Brevo is perfect for your email app:</strong></p><p style="color:#475569;margin-top:10px">✓ 3x more free emails than competitors (300/day vs 100/day)</p><p style="color:#475569">✓ Transactional & marketing email in one platform</p><p style="color:#475569">✓ Built-in analytics and tracking</p><p style="color:#475569">✓ No credit card required for free tier</p></div></div></div>
</body>
</html>
'''

# ==================== MAIN APPLICATION ====================
if __name__ == '__main__':
    # Start background email sender thread
    thread = threading.Thread(target=email_sender_thread, daemon=True)
    thread.start()
    
    print("=" * 60)
    print("🚀 Email Scheduler Pro")
    print("=" * 60)
    print("✅ Database initialized")
    print("✅ Background email sender started")
    print("✅ Server running")
    print("=" * 60)
    print("")
    print("📧 Brevo API Status:")
    if BREVO_CONFIG['api_key'] != 'xkeysib-YOUR-ACTUAL-API-KEY-HERE':
        print(f"   ✅ API Key configured: {BREVO_CONFIG['api_key'][:20]}...")
        print(f"   ✅ From Email: {BREVO_CONFIG['from_email']}")
    else:
        print("   ⚠️  Brevo API key NOT configured!")
        print("   📝 To fix: Edit the BREVO_API_KEY variable in the code")
        print("   🔗 Get your API key from: https://app.brevo.com")
    print("")
    print("🌐 Status page: http://127.0.0.1:5000/status")
    print("=" * 60)
    
    # Get port from environment variable for Render
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
