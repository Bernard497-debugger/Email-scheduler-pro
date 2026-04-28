#!/usr/bin/env python3
"""
Email Scheduler Pro - In-Memory Database Version
Works perfectly on Render free tier with no permission issues
"""

import os
import sqlite3
import re
import secrets
import hashlib
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, g, jsonify

# ==================== CONFIGURATION ====================
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=7)

# Brevo Configuration
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
FROM_EMAIL = os.environ.get('FROM_EMAIL', '')
FROM_NAME = os.environ.get('FROM_NAME', 'Email Scheduler Pro')

# In-memory database (no file permissions needed!)
DATABASE = ':memory:'

# Global database connection for in-memory storage
db_connection = None

def get_db():
    """Get in-memory database connection"""
    global db_connection
    if db_connection is None:
        db_connection = sqlite3.connect(DATABASE, check_same_thread=False)
        db_connection.row_factory = sqlite3.Row
        init_db(db_connection)
    return db_connection

def init_db(conn):
    """Initialize database tables"""
    cursor = conn.cursor()
    
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
            error_message TEXT,
            brevo_message_id TEXT
        )
    ''')
    
    conn.commit()
    print("✅ In-memory database initialized")

# ==================== HELPER FUNCTIONS ====================
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_val):
    return hash_password(password) == hash_val

def get_user_by_email(email):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE email = ?', (email.lower(),)).fetchone()

def create_user(email, password):
    try:
        db = get_db()
        db.execute('INSERT INTO users (email, password, last_reset) VALUES (?, ?, ?)',
                  (email.lower(), hash_password(password), datetime.now().date().isoformat()))
        db.commit()
        return True
    except:
        return False

def get_user(user_id):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def send_email_via_brevo(to_email, subject, body, user_email=None):
    """Send email using Brevo API"""
    if not BREVO_API_KEY:
        return False, "Brevo API key not configured", None
    
    try:
        headers = {
            'api-key': BREVO_API_KEY,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background: #f9f9f9; border-radius: 10px;">
                <div style="background: linear-gradient(135deg, #0284c7, #0ea5e9); color: white; padding: 20px; text-align: center; border-radius: 10px 10px 0 0;">
                    <h2>📧 Email Scheduler Pro</h2>
                </div>
                <div style="padding: 20px;">
                    {body.replace(chr(10), '<br>')}
                </div>
                <div style="text-align: center; padding: 15px; font-size: 12px; color: #666;">
                    <p>Sent via Email Scheduler Pro</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        data = {
            'sender': {'email': FROM_EMAIL, 'name': FROM_NAME},
            'to': [{'email': to_email}],
            'subject': subject,
            'textContent': body,
            'htmlContent': html_content
        }
        
        if user_email:
            data['replyTo'] = {'email': user_email}
        
        response = requests.post('https://api.brevo.com/v3/smtp/email', json=data, headers=headers, timeout=30)
        
        if response.status_code in [200, 201]:
            return True, "Email sent successfully", response.json().get('messageId')
        else:
            error = response.json().get('message', 'Unknown error') if response.text else f"HTTP {response.status_code}"
            return False, f"Brevo error: {error}", None
            
    except Exception as e:
        return False, str(e), None

def send_scheduled_email(email_id):
    """Send a scheduled email"""
    try:
        db = get_db()
        email = db.execute('SELECT * FROM scheduled_emails WHERE id = ?', (email_id,)).fetchone()
        
        if not email or email['status'] != 'scheduled':
            return False
        
        # Check if it's time to send (simple string comparison)
        scheduled_time_str = email['scheduled_time']
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if scheduled_time_str > now_str:
            return False
        
        user = db.execute('SELECT * FROM users WHERE id = ?', (email['user_id'],)).fetchone()
        if not user:
            return False
        
        success, message, brevo_id = send_email_via_brevo(
            email['recipient_email'],
            email['subject'],
            email['body'],
            user['email']
        )
        
        if success:
            db.execute('UPDATE scheduled_emails SET status = "sent", sent_at = CURRENT_TIMESTAMP, brevo_message_id = ? WHERE id = ?', (brevo_id, email_id))
            db.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
            db.commit()
            print(f"✅ Sent email {email_id} to {email['recipient_email']}")
        else:
            db.execute('UPDATE scheduled_emails SET status = "failed", error_message = ? WHERE id = ?', (message, email_id))
            db.commit()
            print(f"❌ Failed to send email {email_id}: {message}")
        
        return success
        
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

# ==================== ROUTES ====================
@app.route('/')
def index():
    return render_template_string(INDEX_TEMPLATE)

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
            flash('Password must be at least 4 characters', 'error')
        elif get_user_by_email(email):
            flash('Email already registered', 'warning')
        elif create_user(email, password):
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Registration failed', 'error')
        
        return redirect(url_for('register'))
    
    return render_template_string(REGISTER_TEMPLATE)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        user = get_user_by_email(email)
        if user and verify_password(password, user['password']):
            session.permanent = True
            session['user_id'] = user['id']
            session['user_email'] = user['email']
            flash(f'Welcome back!', 'success')
            return redirect(url_for('dashboard'))
        else:
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
        return redirect(url_for('login'))
    
    db = get_db()
    scheduled_emails = db.execute('''
        SELECT * FROM scheduled_emails 
        WHERE user_id = ? 
        ORDER BY scheduled_time DESC 
        LIMIT 50
    ''', (user['id'],)).fetchall()
    
    return render_template_string(DASHBOARD_TEMPLATE, 
                                user=user,
                                scheduled_emails=scheduled_emails,
                                brevo_configured=bool(BREVO_API_KEY and FROM_EMAIL),
                                current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

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
    
    if not BREVO_API_KEY:
        flash('⚠️ Email sending not configured. Please add BREVO_API_KEY environment variable.', 'warning')
        return redirect(url_for('dashboard'))
    
    success, message, brevo_id = send_email_via_brevo(recipient, subject, body, user['email'])
    
    if success:
        db = get_db()
        db.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
        db.commit()
        flash(f'✅ Email sent successfully to {recipient}!', 'success')
    else:
        flash(f'❌ Failed to send: {message}', 'error')
    
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
        ''', (user['id'], recipient, subject, body, scheduled_time.strftime('%Y-%m-%d %H:%M:%S')))
        db.commit()
        
        flash(f'📧 Email scheduled for {scheduled_time.strftime("%Y-%m-%d %H:%M")}', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/cancel_scheduled/<int:email_id>')
@login_required
def cancel_scheduled(email_id):
    user = get_user(session['user_id'])
    if not user:
        return redirect(url_for('login'))
    
    db = get_db()
    email = db.execute('SELECT * FROM scheduled_emails WHERE id = ?', (email_id,)).fetchone()
    
    if email and email['user_id'] == user['id'] and email['status'] == 'scheduled':
        db.execute('UPDATE scheduled_emails SET status = "cancelled" WHERE id = ?', (email_id,))
        db.commit()
        flash('Scheduled email cancelled', 'success')
    else:
        flash('Cannot cancel this email', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/run-scheduler')
def run_scheduler():
    """Endpoint for cron-job.org to trigger email sending"""
    db = get_db()
    
    now = datetime.now()
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    # Get due emails
    due_emails = db.execute('''
        SELECT id FROM scheduled_emails 
        WHERE scheduled_time <= ? AND status = "scheduled"
    ''', (now_str,)).fetchall()
    
    sent_count = 0
    failed_count = 0
    
    for email in due_emails:
        if send_scheduled_email(email['id']):
            sent_count += 1
        else:
            failed_count += 1
    
    return jsonify({
        "status": "success",
        "checked_at": now.isoformat(),
        "emails_found": len(due_emails),
        "emails_sent": sent_count,
        "emails_failed": failed_count
    })

@app.route('/scheduler-status')
def scheduler_status():
    """Get scheduler status"""
    db = get_db()
    
    scheduled = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "scheduled"').fetchone()['count']
    sent = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "sent"').fetchone()['count']
    failed = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "failed"').fetchone()['count']
    
    return jsonify({
        "status": "success",
        "scheduled": scheduled,
        "sent": sent,
        "failed": failed,
        "total": scheduled + sent + failed,
        "brevo_configured": bool(BREVO_API_KEY)
    })

@app.route('/api/scheduler/status')
def api_status():
    """Detailed JSON status"""
    db = get_db()
    
    emails = db.execute('''
        SELECT id, recipient_email, subject, scheduled_time, status 
        FROM scheduled_emails 
        WHERE status IN ('scheduled', 'failed')
        ORDER BY scheduled_time ASC
    ''').fetchall()
    
    return jsonify({
        "status": "success",
        "total_pending": len(emails),
        "emails": [dict(e) for e in emails],
        "brevo_configured": bool(BREVO_API_KEY),
        "current_time": datetime.now().isoformat()
    })

@app.route('/api/fix-stuck-emails', methods=['POST'])
def fix_stuck_emails():
    """Force send all stuck emails"""
    db = get_db()
    
    # Get stuck emails
    stuck = db.execute('SELECT id FROM scheduled_emails WHERE status = "scheduled"').fetchall()
    
    # Set them to now
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute("UPDATE scheduled_emails SET scheduled_time = ? WHERE status = 'scheduled'", (now_str,))
    db.commit()
    
    sent = 0
    for email in stuck:
        if send_scheduled_email(email['id']):
            sent += 1
    
    return jsonify({
        "status": "success",
        "stuck_found": len(stuck),
        "emails_sent": sent
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "brevo_configured": bool(BREVO_API_KEY)})

# ==================== HTML TEMPLATES ====================
INDEX_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Scheduler Pro</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%); min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { background: white; box-shadow: 0 2px 20px rgba(0,0,0,0.1); position: sticky; top: 0; z-index: 100; }
        .nav { display: flex; justify-content: space-between; align-items: center; padding: 15px 20px; max-width: 1200px; margin: 0 auto; }
        .logo { font-size: 28px; font-weight: bold; background: linear-gradient(135deg, #0284c7, #0ea5e9); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .nav-links a { margin-left: 20px; text-decoration: none; color: #0369a1; font-weight: 500; }
        .hero { text-align: center; padding: 80px 20px; }
        .hero h1 { font-size: 52px; margin-bottom: 20px; background: linear-gradient(135deg, #0369a1, #0284c7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .hero p { font-size: 20px; margin-bottom: 30px; color: #075985; }
        .btn { display: inline-block; padding: 14px 35px; background: linear-gradient(135deg, #0ea5e9, #0284c7); color: white; text-decoration: none; border-radius: 50px; font-weight: bold; }
        .badge { display: inline-block; background: #10b981; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; margin-left: 10px; }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler Pro <span class="badge">Brevo</span></div>
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
            <p>300 free emails/day • High deliverability • Auto-scheduling</p>
            {% if not session.user_id %}
                <a href="{{ url_for('register') }}" class="btn">🚀 Get Started Free</a>
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
    <title>Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: linear-gradient(135deg, #e0f2fe, #bae6fd); min-height: 100vh; display: flex; justify-content: center; align-items: center; }
        .card { background: white; padding: 40px; border-radius: 20px; width: 350px; }
        h2 { text-align: center; color: #0369a1; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }
        button { width: 100%; padding: 12px; background: #0284c7; color: white; border: none; border-radius: 5px; cursor: pointer; }
        .alert { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .link { text-align: center; margin-top: 15px; }
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
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        <div class="link"><a href="/register">Create Account</a></div>
    </div>
</body>
</html>
'''

REGISTER_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Register</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: linear-gradient(135deg, #e0f2fe, #bae6fd); min-height: 100vh; display: flex; justify-content: center; align-items: center; }
        .card { background: white; padding: 40px; border-radius: 20px; width: 350px; }
        h2 { text-align: center; color: #0369a1; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }
        button { width: 100%; padding: 12px; background: #0284c7; color: white; border: none; border-radius: 5px; cursor: pointer; }
        .alert { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .link { text-align: center; margin-top: 15px; }
    </style>
</head>
<body>
    <div class="card">
        <h2>✨ Register</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="alert alert-{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        <form method="POST">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password (min 4)" required>
            <input type="password" name="confirm_password" placeholder="Confirm Password" required>
            <button type="submit">Register</button>
        </form>
        <div class="link"><a href="/login">Already have an account?</a></div>
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
    <title>Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Arial, sans-serif; background: #f0f9ff; }
        .header { background: white; padding: 15px 30px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .nav { display: flex; justify-content: space-between; align-items: center; max-width: 1200px; margin: 0 auto; }
        .container { max-width: 1200px; margin: 30px auto; padding: 0 20px; }
        .card { background: white; border-radius: 15px; padding: 25px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        input, textarea { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 8px; }
        button { padding: 10px 20px; background: #0284c7; color: white; border: none; border-radius: 8px; cursor: pointer; }
        .alert { padding: 10px; margin-bottom: 15px; border-radius: 8px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
        .email-item { padding: 15px; border-bottom: 1px solid #eee; }
        .status { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; }
        .status-scheduled { background: #fff3e0; color: #f59e0b; }
        .btn-sm { padding: 5px 12px; font-size: 12px; text-decoration: none; border-radius: 8px; background: #f59e0b; color: white; }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <h2>🚀 Email Scheduler</h2>
            <div>
                <span>👋 {{ user.email }}</span>
                <a href="/logout" style="margin-left: 15px;">Logout</a>
            </div>
        </div>
    </div>
    <div class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="alert alert-{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        
        {% if not brevo_configured %}
            <div class="alert alert-warning">⚠️ Brevo API not configured. Add BREVO_API_KEY and FROM_EMAIL environment variables.</div>
        {% endif %}
        
        <div class="card">
            <h3>📤 Send Email Now</h3>
            <form method="POST" action="/send_now">
                <input type="email" name="recipient" placeholder="Recipient Email" required>
                <input type="text" name="subject" placeholder="Subject" required>
                <textarea name="body" rows="3" placeholder="Your message" required></textarea>
                <button type="submit">✈️ Send Now</button>
            </form>
        </div>
        
        <div class="card">
            <h3>⏰ Schedule Email</h3>
            <form method="POST" action="/schedule_email">
                <input type="email" name="recipient" placeholder="Recipient Email" required>
                <input type="text" name="subject" placeholder="Subject" required>
                <textarea name="body" rows="3" placeholder="Your message" required></textarea>
                <input type="datetime-local" name="schedule_datetime" required>
                <button type="submit">📅 Schedule</button>
            </form>
        </div>
        
        <div class="card">
            <h3>📋 Scheduled Emails</h3>
            <div><small>Current time: {{ current_time }}</small></div>
            {% if scheduled_emails %}
                {% for email in scheduled_emails %}
                    <div class="email-item">
                        <strong>To:</strong> {{ email.recipient_email }}<br>
                        <strong>Subject:</strong> {{ email.subject[:50] }}<br>
                        <strong>Time:</strong> {{ email.scheduled_time }}<br>
                        <span class="status status-{{ email.status }}">{{ email.status }}</span>
                        {% if email.status == 'scheduled' %}
                            <a href="/cancel_scheduled/{{ email.id }}" class="btn-sm" style="margin-left: 10px;">Cancel</a>
                        {% endif %}
                    </div>
                {% endfor %}
            {% else %}
                <p>No scheduled emails</p>
            {% endif %}
        </div>
        
        <div class="card">
            <h3>⚙️ Scheduler</h3>
            <a href="/run-scheduler"><button type="button">▶️ Run Scheduler Now</button></a>
            <button type="button" onclick="fixStuckEmails()">🔧 Fix Stuck Emails</button>
            <a href="/scheduler-status"><button type="button">📊 Status</button></a>
        </div>
    </div>
    
    <script>
    function fixStuckEmails() {
        fetch('/api/fix-stuck-emails', { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                alert(`✅ Sent ${data.emails_sent} emails!`);
                location.reload();
            })
            .catch(error => alert('Error: ' + error));
    }
    </script>
</body>
</html>
'''

# ==================== START APP ====================
# Initialize database on startup
with app.app_context():
    get_db()
    print("=" * 50)
    print("🚀 Email Scheduler Pro Started")
    print("=" * 50)
    print(f"✅ Database: In-memory (no file issues)")
    print(f"✅ Brevo: {'Configured' if BREVO_API_KEY else 'Not configured'}")
    print(f"✅ Endpoints: /dashboard, /run-scheduler, /api/fix-stuck-emails")
    print("=" * 50)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
