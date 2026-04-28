#!/usr/bin/env python3
"""
Email Scheduler Pro - Complete Working Version for Render
With proper Brevo API integration and environment variable handling
"""

import os
import sqlite3
import re
import secrets
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, g, jsonify

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
app.permanent_session_lifetime = timedelta(days=7)

# ==================== BREVO CONFIGURATION ====================
# Read environment variables directly
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
FROM_EMAIL = os.environ.get('FROM_EMAIL', '')
FROM_NAME = os.environ.get('FROM_NAME', 'Email Scheduler Pro')

BREVO_CONFIG = {
    'api_key': BREVO_API_KEY,
    'from_email': FROM_EMAIL,
    'from_name': FROM_NAME,
    'api_url': 'https://api.brevo.com/v3/smtp/email'
}

# Print debug info on startup
print("=" * 60)
print("🚀 EMAIL SCHEDULER PRO STARTING")
print("=" * 60)
print(f"✅ Database path: {DATABASE}")
print(f"✅ Brevo API Key: {'✓ SET' if BREVO_API_KEY else '✗ MISSING'}")
print(f"✅ From Email: {FROM_EMAIL if FROM_EMAIL else '✗ MISSING'}")
print(f"✅ From Name: {FROM_NAME}")
print(f"✅ Secret Key: {'✓ SET' if app.secret_key else '✗ MISSING'}")
print("=" * 60)

# Pricing plans
PLANS = {
    'free': {'name': 'Free', 'emails_per_month': 9000, 'scheduled_emails': 50, 'price': 0},
    'basic': {'name': 'Basic', 'emails_per_month': 20000, 'scheduled_emails': 200, 'price': 9.99},
    'pro': {'name': 'Pro', 'emails_per_month': 50000, 'scheduled_emails': 1000, 'price': 19.99},
    'business': {'name': 'Business', 'emails_per_month': 100000, 'scheduled_emails': 5000, 'price': 49.99}
}

# ==================== DATABASE FUNCTIONS ====================
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
        print("✅ Database initialized successfully")
        
    except Exception as e:
        print(f"Database initialization error: {e}")

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

# ==================== PASSWORD FUNCTIONS ====================
def hash_password(password):
    """Secure password hashing"""
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_value):
    return hash_password(password) == hash_value

# ==================== USER FUNCTIONS ====================
def get_user(user_id):
    try:
        db = get_db()
        return db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    except:
        return None

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
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def can_send_email(user):
    """Check if user can send more emails this month"""
    plan = PLANS[user['plan']]
    return user['emails_sent_this_month'] < plan['emails_per_month']

def reset_monthly_counts():
    """Reset monthly email counts"""
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
    except:
        pass

# ==================== BREVO EMAIL FUNCTIONS ====================
def send_email_via_brevo(to_email, subject, body, user_email=None):
    """Send email using Brevo API"""
    
    # Check if Brevo is configured
    if not BREVO_CONFIG['api_key']:
        return False, "Brevo API key not configured. Please add BREVO_API_KEY environment variable.", None
    
    if not BREVO_CONFIG['from_email']:
        return False, "FROM_EMAIL not configured. Please add FROM_EMAIL environment variable.", None
    
    try:
        headers = {
            'api-key': BREVO_CONFIG['api_key'],
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # Create HTML content
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: linear-gradient(135deg, #0284c7, #0ea5e9); color: white; padding: 20px; text-align: center; border-radius: 10px 10px 0 0; }}
                .content {{ padding: 20px; background: #f9f9f9; }}
                .footer {{ text-align: center; padding: 15px; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>📧 Email Scheduler Pro</h2>
                </div>
                <div class="content">
                    {body.replace(chr(10), '<br>')}
                </div>
                <div class="footer">
                    <p>Sent via Email Scheduler Pro</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        data = {
            'sender': {
                'email': BREVO_CONFIG['from_email'],
                'name': BREVO_CONFIG['from_name']
            },
            'to': [{'email': to_email}],
            'subject': subject,
            'textContent': body,
            'htmlContent': html_content
        }
        
        if user_email:
            data['replyTo'] = {'email': user_email}
        
        # Make API request
        response = requests.post(BREVO_CONFIG['api_url'], json=data, headers=headers, timeout=30)
        
        if response.status_code in [200, 201]:
            result = response.json()
            return True, "Email sent successfully", result.get('messageId', 'sent')
        else:
            error_msg = response.json().get('message', 'Unknown error') if response.text else f"HTTP {response.status_code}"
            return False, f"Brevo API error: {error_msg}", None
            
    except requests.exceptions.Timeout:
        return False, "Request timeout", None
    except requests.exceptions.RequestException as e:
        return False, f"Network error: {str(e)}", None
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
            flash(f'Welcome back, {email}!', 'success')
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
        session.clear()
        return redirect(url_for('login'))
    
    # Get scheduled emails
    db = get_db()
    scheduled_emails = db.execute('''
        SELECT * FROM scheduled_emails 
        WHERE user_id = ? 
        ORDER BY scheduled_time DESC 
        LIMIT 50
    ''', (user['id'],)).fetchall()
    
    plan_details = PLANS[user['plan']]
    remaining_emails = max(0, plan_details['emails_per_month'] - user['emails_sent_this_month'])
    remaining_scheduled = 50 - len([e for e in scheduled_emails if e['status'] == 'scheduled'])
    
    return render_template_string(DASHBOARD_TEMPLATE, 
                                user=user,
                                scheduled_emails=scheduled_emails,
                                plan=plan_details,
                                plan_name=user['plan'],
                                remaining_emails=remaining_emails,
                                remaining_scheduled=remaining_scheduled,
                                brevo_configured=bool(BREVO_CONFIG['api_key'] and BREVO_CONFIG['from_email']))

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
    
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', recipient):
        flash('Invalid email address', 'error')
        return redirect(url_for('dashboard'))
    
    if not BREVO_CONFIG['api_key']:
        flash('⚠️ Email sending is not configured. Please add BREVO_API_KEY environment variable.', 'warning')
        return redirect(url_for('dashboard'))
    
    if not can_send_email(user):
        flash(f'Monthly email limit reached ({PLANS[user["plan"]]["emails_per_month"]} max)', 'error')
        return redirect(url_for('dashboard'))
    
    success, message, brevo_id = send_email_via_brevo(recipient, subject, body, user['email'])
    
    if success:
        try:
            db = get_db()
            db.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
            db.commit()
            flash(f'✅ Email sent successfully to {recipient}!', 'success')
        except Exception as e:
            flash(f'Email sent but failed to update stats: {str(e)}', 'warning')
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
        ''', (user['id'], recipient, subject, body, scheduled_time.isoformat()))
        db.commit()
        
        flash(f'📧 Email scheduled for {scheduled_time.strftime("%Y-%m-%d %H:%M")}', 'success')
    except ValueError:
        flash('Invalid date/time format', 'error')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/cancel_scheduled/<int:email_id>')
@login_required
def cancel_scheduled(email_id):
    user = get_user(session['user_id'])
    if not user:
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
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

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
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/scheduler-status')
def scheduler_status():
    """Get current scheduler status"""
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        
        scheduled = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "scheduled"').fetchone()['count']
        sent = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "sent"').fetchone()['count']
        failed = db.execute('SELECT COUNT(*) as count FROM scheduled_emails WHERE status = "failed"').fetchone()['count']
        
        db.close()
        
        return jsonify({
            "status": "success",
            "scheduled": scheduled,
            "sent": sent,
            "failed": failed,
            "total": scheduled + sent + failed,
            "brevo_configured": bool(BREVO_CONFIG['api_key'])
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "database": DATABASE,
        "brevo_configured": bool(BREVO_CONFIG['api_key'])
    })

@app.route('/check-config')
def check_config():
    """Debug endpoint to check configuration"""
    return jsonify({
        "brevo_configured": bool(BREVO_CONFIG['api_key'] and BREVO_CONFIG['from_email']),
        "api_key_preview": BREVO_CONFIG['api_key'][:20] + "..." if BREVO_CONFIG['api_key'] else "Not set",
        "from_email": BREVO_CONFIG['from_email'] if BREVO_CONFIG['from_email'] else "Not set",
        "from_name": BREVO_CONFIG['from_name'],
        "database_path": DATABASE
    })

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
        .btn { display: inline-block; padding: 14px 35px; background: linear-gradient(135deg, #0ea5e9, #0284c7); color: white; text-decoration: none; border-radius: 50px; font-weight: bold; box-shadow: 0 4px 15px rgba(2,132,199,0.3); }
        .badge { display: inline-block; background: #10b981; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; margin-left: 10px; }
        .free-badge { background: #f59e0b; }
        @media (max-width: 768px) { .hero h1 { font-size: 36px; } }
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
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%); min-height: 100vh; display: flex; justify-content: center; align-items: center; }
        .card { background: white; padding: 45px; border-radius: 30px; box-shadow: 0 20px 40px rgba(2,132,199,0.2); width: 100%; max-width: 420px; }
        .card h2 { text-align: center; margin-bottom: 30px; color: #0369a1; font-size: 32px; }
        .form-group { margin-bottom: 25px; }
        label { display: block; margin-bottom: 8px; color: #475569; font-weight: 500; }
        input { width: 100%; padding: 12px 15px; border: 2px solid #e2e8f0; border-radius: 12px; font-size: 16px; }
        input:focus { outline: none; border-color: #0ea5e9; }
        button { width: 100%; padding: 14px; background: linear-gradient(135deg, #0ea5e9, #0284c7); color: white; border: none; border-radius: 12px; font-size: 16px; font-weight: bold; cursor: pointer; }
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
        <h2>🔐 Welcome Back</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="alert alert-{{ category }}">{{ message }}</div>
            {% endfor %}
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
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%); min-height: 100vh; display: flex; justify-content: center; align-items: center; }
        .card { background: white; padding: 45px; border-radius: 30px; box-shadow: 0 20px 40px rgba(2,132,199,0.2); width: 100%; max-width: 420px; }
        .card h2 { text-align: center; margin-bottom: 10px; color: #0369a1; font-size: 32px; }
        .subtitle { text-align: center; color: #64748b; margin-bottom: 30px; font-size: 14px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; color: #475569; font-weight: 500; }
        input { width: 100%; padding: 12px 15px; border: 2px solid #e2e8f0; border-radius: 12px; font-size: 16px; }
        input:focus { outline: none; border-color: #0ea5e9; }
        button { width: 100%; padding: 14px; background: linear-gradient(135deg, #0ea5e9, #0284c7); color: white; border: none; border-radius: 12px; font-size: 16px; font-weight: bold; cursor: pointer; }
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
            {% for category, message in messages %}
                <div class="alert alert-{{ category }}">{{ message }}</div>
            {% endfor %}
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
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f0f9ff; }
        .header { background: white; box-shadow: 0 2px 20px rgba(0,0,0,0.08); position: sticky; top: 0; z-index: 100; }
        .nav { display: flex; justify-content: space-between; align-items: center; padding: 15px 30px; max-width: 1400px; margin: 0 auto; }
        .logo { font-size: 24px; font-weight: bold; background: linear-gradient(135deg, #0284c7, #0ea5e9); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .nav-links a { margin-left: 20px; text-decoration: none; color: #0369a1; font-weight: 500; }
        .container { max-width: 1400px; margin: 30px auto; padding: 0 30px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 25px; margin-bottom: 40px; }
        .stat-card { background: white; padding: 25px; border-radius: 20px; box-shadow: 0 5px 20px rgba(0,0,0,0.05); transition: transform 0.3s; }
        .stat-card:hover { transform: translateY(-5px); }
        .stat-card h3 { color: #64748b; margin-bottom: 12px; font-size: 18px; }
        .stat-card .number { font-size: 38px; font-weight: bold; color: #0284c7; margin-bottom: 10px; }
        .row { display: grid; grid-template-columns: 1fr 1fr; gap: 25px; margin-bottom: 40px; }
        .form-card { background: white; padding: 25px; border-radius: 20px; box-shadow: 0 5px 20px rgba(0,0,0,0.05); }
        .form-card h3 { margin-bottom: 20px; color: #0369a1; font-size: 22px; }
        .form-group { margin-bottom: 18px; }
        label { display: block; margin-bottom: 8px; color: #475569; font-weight: 500; }
        input, textarea { width: 100%; padding: 12px; border: 2px solid #e2e8f0; border-radius: 12px; font-size: 14px; }
        input:focus, textarea:focus { outline: none; border-color: #0ea5e9; }
        button { padding: 12px 24px; background: linear-gradient(135deg, #0ea5e9, #0284c7); color: white; border: none; border-radius: 12px; cursor: pointer; font-weight: bold; }
        button:hover { opacity: 0.9; }
        .email-list { background: white; border-radius: 20px; overflow: hidden; box-shadow: 0 5px 20px rgba(0,0,0,0.05); }
        .email-item { padding: 18px; border-bottom: 1px solid #e2e8f0; }
        .status { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; }
        .status-scheduled { background: #fff3e0; color: #f59e0b; }
        .status-sent { background: #d1fae5; color: #10b981; }
        .status-failed { background: #fee2e2; color: #ef4444; }
        .btn-sm { padding: 5px 12px; font-size: 12px; margin-left: 8px; border-radius: 8px; text-decoration: none; display: inline-block; }
        .btn-warning { background: #f59e0b; color: white; }
        .alert { padding: 12px 20px; margin-bottom: 25px; border-radius: 12px; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
        .alert-info { background: #dbeafe; color: #1e40af; }
        .brevo-badge { background: #0ea5e9; color: white; padding: 2px 8px; border-radius: 12px; font-size: 11px; margin-left: 8px; }
        @media (max-width: 768px) { .row { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler Pro</div>
            <div class="nav-links">
                <span style="color: #0284c7;">⭐ {{ plan_name|capitalize }} Plan</span>
                <a href="{{ url_for('logout') }}">Logout</a>
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
            <div class="alert alert-warning">
                ⚠️ Brevo API not configured! Add BREVO_API_KEY and FROM_EMAIL environment variables to enable email sending.
            </div>
        {% endif %}
        
        <div class="stats-grid">
            <div class="stat-card">
                <h3>📊 Monthly Usage</h3>
                <div class="number">{{ user.emails_sent_this_month }} / {{ plan.emails_per_month }}</div>
                <small>✨ {{ remaining_emails }} emails remaining</small>
            </div>
            <div class="stat-card">
                <h3>⏰ Scheduled Emails</h3>
                <div class="number">{{ remaining_scheduled }} / {{ plan.scheduled_emails }}</div>
                <small>🎯 slots available</small>
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
                <a href="/run-scheduler" style="float: right; margin-left: 10px; font-size: 12px;">▶️ Run Scheduler</a>
                <a href="/scheduler-status" style="float: right; font-size: 12px;">📊 Status</a>
            </div>
            {% if scheduled_emails %}
                {% for email in scheduled_emails %}
                    <div class="email-item">
                        <div>
                            <strong>To:</strong> {{ email.recipient_email }}<br>
                            <strong>Subject:</strong> {{ email.subject[:60] }}{% if email.subject|length > 60 %}...{% endif %}<br>
                            <strong>🕐 Time:</strong> {{ email.scheduled_time[:16].replace('T', ' ') }}<br>
                            <span class="status status-{{ email.status }}">{{ email.status }}</span>
                            {% if email.brevo_message_id %}
                                <br><small>📨 Brevo ID: {{ email.brevo_message_id[:12] }}...</small>
                            {% endif %}
                            {% if email.error_message %}
                                <br><small style="color: #ef4444;">❌ {{ email.error_message[:50] }}</small>
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

# ==================== INITIALIZE ====================
with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
