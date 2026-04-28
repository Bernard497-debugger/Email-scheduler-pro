#!/usr/bin/env python3
"""
Auto Scheduled Email Sending App - Pydroid Compatible Version
Using Brevo API for Email Delivery
Light Blue & White Theme with Animations - DATABASE FIXED
"""

import sqlite3
import threading
import time
import re
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, g
from functools import wraps

# ==================== CONFIGURATION ====================
app = Flask(__name__)
app.secret_key = 'pydroid-email-scheduler-secret-key'

# Database file path (works on Pydroid)
DATABASE = 'email_scheduler.db'

# Brevo API Configuration
# Sign up at https://www.brevo.com (formerly Sendinblue)
# Get your API key from: Account → SMTP & API → API Keys
BREVO_CONFIG = {
    'api_key': '',  # Get from Brevo dashboard
    'from_email': 'botsile55@gmail.com',  # Use testing@brevo.com for trial
    'from_name': 'Email Scheduler Pro',
    'api_url': 'https://api.brevo.com/v3/smtp/email'
}

# Pricing plans (in USD)
PLANS = {
    'free': {
        'name': 'Free',
        'emails_per_month': 9000,  # Brevo free gives 300/day = ~9000/month
        'scheduled_emails': 50,
        'price': 0
    },
    'basic': {
        'name': 'Basic',
        'emails_per_month': 20000,
        'scheduled_emails': 200,
        'price': 9.99
    },
    'pro': {
        'name': 'Pro',
        'emails_per_month': 50000,
        'scheduled_emails': 1000,
        'price': 19.99
    },
    'business': {
        'name': 'Business',
        'emails_per_month': 100000,
        'scheduled_emails': 5000,
        'price': 49.99
    }
}

# ==================== DATABASE FUNCTIONS ====================
def get_db():
    """Get database connection"""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    """Close database connection"""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def migrate_database():
    """Add missing columns to existing database"""
    try:
        db = sqlite3.connect(DATABASE)
        cursor = db.cursor()
        
        # Check if brevo_message_id column exists
        cursor.execute("PRAGMA table_info(scheduled_emails)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'brevo_message_id' not in columns:
            print("Adding brevo_message_id column...")
            cursor.execute("ALTER TABLE scheduled_emails ADD COLUMN brevo_message_id TEXT")
            db.commit()
            print("✅ brevo_message_id column added")
        
        if 'error_message' not in columns:
            print("Adding error_message column...")
            cursor.execute("ALTER TABLE scheduled_emails ADD COLUMN error_message TEXT")
            db.commit()
            print("✅ error_message column added")
        
        db.close()
    except Exception as e:
        print(f"Migration error: {e}")

def init_db():
    """Initialize database tables"""
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
    
    # Scheduled emails table - with all columns
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
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    db.commit()
    db.close()
    
    # Run migration for existing databases
    migrate_database()

@app.teardown_appcontext
def teardown_db(error):
    close_db()

# ==================== HELPER FUNCTIONS ====================
def hash_password(password):
    """Simple password hashing (for Pydroid compatibility)"""
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_value):
    """Verify password"""
    return hash_password(password) == hash_value

def login_required(f):
    """Login decorator"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to access this page', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_user(user_id):
    """Get user by ID"""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    return user

def get_user_by_email(email):
    """Get user by email"""
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    return user

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
    db = get_db()
    scheduled_count = db.execute(
        'SELECT COUNT(*) as count FROM scheduled_emails WHERE user_id = ? AND status = "scheduled"',
        (user['id'],)
    ).fetchone()['count']
    
    plan = PLANS[user['plan']]
    return scheduled_count < plan['scheduled_emails']

def get_remaining_emails(user):
    """Get remaining emails this month"""
    plan = PLANS[user['plan']]
    return max(0, plan['emails_per_month'] - user['emails_sent_this_month'])

def get_remaining_scheduled(user):
    """Get remaining scheduled email slots"""
    db = get_db()
    scheduled_count = db.execute(
        'SELECT COUNT(*) as count FROM scheduled_emails WHERE user_id = ? AND status = "scheduled"',
        (user['id'],)
    ).fetchone()['count']
    
    plan = PLANS[user['plan']]
    return max(0, plan['scheduled_emails'] - scheduled_count)

# ==================== BREVO API EMAIL FUNCTIONS ====================
def send_email_via_brevo(to_email, subject, body, user_email=None):
    """Send email using Brevo API"""
    try:
        headers = {
            'api-key': BREVO_CONFIG['api_key'],
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # Build HTML content with proper formatting
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
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
        
        # Optional: Add reply-to if user email is provided
        if user_email:
            data['replyTo'] = {'email': user_email}
        
        response = requests.post(BREVO_CONFIG['api_url'], json=data, headers=headers)
        
        if response.status_code == 201 or response.status_code == 200:
            result = response.json()
            return True, "Email sent successfully", result.get('messageId', 'sent')
        else:
            error_msg = response.json().get('message', 'Unknown error')
            return False, f"Brevo API error: {error_msg}", None
            
    except requests.exceptions.RequestException as e:
        return False, f"Network error: {str(e)}", None
    except Exception as e:
        return False, str(e), None

def send_scheduled_email(email_id):
    """Send a single scheduled email using Brevo"""
    try:
        # Create a new database connection for the background thread
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        email = db.execute('SELECT * FROM scheduled_emails WHERE id = ?', (email_id,)).fetchone()
        
        if not email or email['status'] != 'scheduled':
            db.close()
            return
        
        # Get user info
        user = db.execute('SELECT * FROM users WHERE id = ?', (email['user_id'],)).fetchone()
        if not user:
            db.close()
            return
        
        if not can_send_email(user):
            db.execute('UPDATE scheduled_emails SET status = "failed", error_message = "Monthly email limit reached" WHERE id = ?', (email_id,))
            db.commit()
            db.close()
            return
        
        # Try to send the email
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
            
            # Update user's email count
            db.execute('''
                UPDATE users 
                SET emails_sent_this_month = emails_sent_this_month + 1 
                WHERE id = ?
            ''', (user['id'],))
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

def email_sender_thread():
    """Background thread to send scheduled emails"""
    while True:
        try:
            # Create a new database connection for the background thread
            db = sqlite3.connect(DATABASE)
            db.row_factory = sqlite3.Row
            cursor = db.cursor()
            
            now = datetime.now()
            due_emails = cursor.execute('''
                SELECT id FROM scheduled_emails 
                WHERE scheduled_time <= ? AND status = "scheduled"
            ''', (now.isoformat(),)).fetchall()
            
            db.close()
            
            for email in due_emails:
                send_scheduled_email(email['id'])
            
            # Reset monthly counts
            reset_monthly_counts()
        except Exception as e:
            print(f"Background thread error: {e}")
        
        time.sleep(60)  # Check every minute

# ==================== TEST BREVO CONNECTION ====================
@app.route('/test_brevo')
@login_required
def test_brevo():
    """Test endpoint to verify Brevo API is working"""
    if BREVO_CONFIG['api_key'] == 'xkeysib-your-api-key-here':
        flash('⚠️ Please configure your Brevo API key in the code first!', 'warning')
        return redirect(url_for('dashboard'))
    
    user = get_user(session['user_id'])
    if not user:
        return redirect(url_for('login'))
    
    # Send test email to the logged-in user
    success, message, brevo_id = send_email_via_brevo(
        user['email'],
        '✨ Brevo API Test - Email Scheduler Pro',
        f'Hello {user["email"]}!\n\nThis is a test email sent via Brevo API.\n\n'
        f'Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
        f'Your email scheduler app is working perfectly! 🎉\n\n'
        f'Features:\n'
        f'• 300 free emails/day with Brevo\n'
        f'• Auto-scheduling engine\n'
        f'• Multiple pricing plans\n'
        f'• Beautiful email templates\n\n'
        f'Happy scheduling! 📧',
        user['email']
    )
    
    if success:
        flash(f'✅ Test email sent successfully! Check your inbox. ID: {brevo_id}', 'success')
    else:
        flash(f'❌ Test failed: {message}', 'error')
    
    return redirect(url_for('dashboard'))

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
        
        if get_user_by_email(email):
            flash('Email already registered. Please login.', 'warning')
            return redirect(url_for('login'))
        
        db = get_db()
        db.execute(
            'INSERT INTO users (email, password, last_reset) VALUES (?, ?, ?)',
            (email, hash_password(password), datetime.now().date().isoformat())
        )
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
    scheduled_emails = db.execute('''
        SELECT * FROM scheduled_emails 
        WHERE user_id = ? 
        ORDER BY scheduled_time DESC 
        LIMIT 50
    ''', (user['id'],)).fetchall()
    
    plan_details = PLANS[user['plan']]
    usage_percentage = (user['emails_sent_this_month'] / plan_details['emails_per_month'] * 100) if plan_details['emails_per_month'] > 0 else 0
    
    return render_template_string(DASHBOARD_TEMPLATE, 
                                user=user,
                                scheduled_emails=scheduled_emails,
                                plan=plan_details,
                                plan_name=user['plan'],
                                usage_percentage=usage_percentage,
                                remaining_emails=get_remaining_emails(user),
                                remaining_scheduled=get_remaining_scheduled(user),
                                brevo_configured=BREVO_CONFIG['api_key'] != 'xkeysib-your-api-key-here')

@app.route('/schedule_email', methods=['POST'])
@login_required
def schedule_email():
    user = get_user(session['user_id'])
    
    if not can_schedule_email(user):
        flash(f'Scheduling limit reached ({PLANS[user["plan"]]["scheduled_emails"]} max). Upgrade to schedule more.', 'error')
        return redirect(url_for('dashboard'))
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    schedule_datetime_str = request.form.get('schedule_datetime', '')
    
    if not all([recipient, subject, body, schedule_datetime_str]):
        flash('All fields are required', 'error')
        return redirect(url_for('dashboard'))
    
    # Validate email format
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
        flash(f'Monthly email limit reached ({PLANS[user["plan"]]["emails_per_month"]} max). Upgrade to send more.', 'error')
        return redirect(url_for('dashboard'))
    
    success, message, brevo_id = send_email_via_brevo(recipient, subject, body, user['email'])
    
    if success:
        db = get_db()
        db.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
        db.commit()
        flash(f'✅ Email sent successfully!', 'success')
    else:
        flash(f'❌ Failed to send: {message}', 'error')
    
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
        flash('Scheduled email cancelled', 'success')
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

# ==================== HTML TEMPLATES ====================
# [All the HTML templates remain exactly the same as in the previous version]
# I'm omitting them here for brevity, but they should be copied from the previous response

INDEX_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Scheduler Pro - Brevo API</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%);
            min-height: 100vh;
            animation: fadeIn 0.8s ease-in;
        }
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        @keyframes slideDown {
            from { transform: translateY(-100px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        @keyframes slideUp {
            from { transform: translateY(50px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.05); }
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { 
            background: white; 
            box-shadow: 0 2px 20px rgba(0,0,0,0.1);
            position: sticky; 
            top: 0; 
            z-index: 100;
            animation: slideDown 0.6s ease-out;
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
            animation: pulse 2s infinite;
        }
        .nav-links a { 
            margin-left: 20px; 
            text-decoration: none; 
            color: #0369a1;
            font-weight: 500;
            transition: all 0.3s ease;
        }
        .nav-links a:hover { color: #0284c7; transform: translateY(-2px); }
        .hero { 
            text-align: center; 
            padding: 80px 20px; 
            animation: slideUp 0.8s ease-out;
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
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(2, 132, 199, 0.3);
        }
        .btn:hover { transform: translateY(-3px); box-shadow: 0 6px 20px rgba(2, 132, 199, 0.4); }
        .pricing-grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); 
            gap: 30px; 
            margin-top: 60px;
            margin-bottom: 60px;
        }
        .card { 
            background: white; 
            border-radius: 20px; 
            padding: 30px; 
            text-align: center; 
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            transition: all 0.3s ease;
        }
        .card:hover { transform: translateY(-10px); box-shadow: 0 20px 40px rgba(2, 132, 199, 0.2); }
        .card h3 { font-size: 28px; margin-bottom: 15px; color: #0369a1; }
        .price { font-size: 48px; color: #0284c7; margin: 20px 0; font-weight: bold; }
        .features { list-style: none; margin: 25px 0; }
        .features li { padding: 10px 0; color: #475569; transition: transform 0.3s ease; }
        .features li:hover { transform: translateX(5px); color: #0284c7; }
        .badge {
            display: inline-block;
            background: #10b981;
            color: white;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            margin-left: 10px;
        }
        .free-badge {
            background: #f59e0b;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler Pro <span class="badge">Brevo API</span> <span class="badge free-badge">300/day Free</span></div>
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
    
    <div class="container">
        <h2 style="text-align: center; margin-bottom: 20px; color: #0369a1;">💰 Simple Pricing Plans</h2>
        <div class="pricing-grid">
            {% for plan_name, plan in plans.items() %}
                <div class="card">
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

# I'm including only the INDEX_TEMPLATE above for brevity
# The LOGIN_TEMPLATE, REGISTER_TEMPLATE, DASHBOARD_TEMPLATE, and UPGRADE_TEMPLATE
# remain exactly the same as in the previous version

# For the full code, please copy the templates from the previous response
# They work perfectly with this database-fixed version

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
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            animation: fadeIn 0.8s ease-in;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: scale(0.95); }
            to { opacity: 1; transform: scale(1); }
        }
        @keyframes slideIn {
            from { transform: translateY(-50px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        .card { 
            background: white; 
            padding: 45px; 
            border-radius: 30px; 
            box-shadow: 0 20px 40px rgba(2, 132, 199, 0.2);
            width: 100%; 
            max-width: 420px;
            animation: slideIn 0.6s ease-out;
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
            transition: all 0.3s ease;
        }
        input:focus {
            outline: none;
            border-color: #0ea5e9;
            box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.1);
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
            transition: all 0.3s ease;
        }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 20px rgba(2, 132, 199, 0.3); }
        .link { text-align: center; margin-top: 25px; }
        .link a { color: #0ea5e9; text-decoration: none; }
        .alert { padding: 12px 15px; margin-bottom: 20px; border-radius: 12px; animation: slideIn 0.4s ease-out; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
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
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            animation: fadeIn 0.8s ease-in;
        }
        @keyframes bounceIn {
            0% { transform: scale(0.9); opacity: 0; }
            60% { transform: scale(1.02); }
            100% { transform: scale(1); opacity: 1; }
        }
        .card { 
            background: white; 
            padding: 45px; 
            border-radius: 30px; 
            box-shadow: 0 20px 40px rgba(2, 132, 199, 0.2);
            width: 100%; 
            max-width: 420px;
            animation: bounceIn 0.6s ease-out;
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
            transition: all 0.3s ease;
        }
        input:focus {
            outline: none;
            border-color: #0ea5e9;
            box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.1);
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
            transition: all 0.3s ease;
        }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 20px rgba(2, 132, 199, 0.3); }
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

# For DASHBOARD_TEMPLATE and UPGRADE_TEMPLATE, please use the ones from the previous response
# They are identical and work perfectly

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
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            background: #f0f9ff;
            animation: fadeIn 0.6s ease-in;
        }
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        @keyframes slideIn {
            from { transform: translateX(-20px); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        @keyframes glow {
            0%, 100% { box-shadow: 0 0 5px rgba(14, 165, 233, 0.3); }
            50% { box-shadow: 0 0 20px rgba(14, 165, 233, 0.6); }
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
            transition: all 0.3s ease;
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
            transition: all 0.3s ease;
            animation: slideIn 0.5s ease-out;
        }
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 30px rgba(2, 132, 199, 0.15);
        }
        .stat-card h3 { color: #64748b; margin-bottom: 12px; font-size: 18px; }
        .stat-card .number { font-size: 38px; font-weight: bold; color: #0284c7; margin-bottom: 10px; }
        .progress-bar { background: #e2e8f0; border-radius: 10px; overflow: hidden; margin-top: 12px; }
        .progress-fill { 
            background: linear-gradient(90deg, #0ea5e9, #0284c7);
            height: 10px; 
            transition: width 0.5s ease;
            animation: glow 2s infinite;
        }
        .row { display: grid; grid-template-columns: 1fr 1fr; gap: 25px; margin-bottom: 40px; }
        .form-card { 
            background: white; 
            padding: 25px; 
            border-radius: 20px; 
            box-shadow: 0 5px 20px rgba(0,0,0,0.05);
            transition: all 0.3s ease;
        }
        .form-card:hover { transform: translateY(-3px); box-shadow: 0 10px 30px rgba(2, 132, 199, 0.1); }
        .form-card h3 { margin-bottom: 20px; color: #0369a1; font-size: 22px; }
        .form-group { margin-bottom: 18px; }
        label { display: block; margin-bottom: 8px; color: #475569; font-weight: 500; }
        input, textarea { 
            width: 100%; 
            padding: 12px; 
            border: 2px solid #e2e8f0; 
            border-radius: 12px;
            font-size: 14px;
            transition: all 0.3s ease;
        }
        input:focus, textarea:focus {
            outline: none;
            border-color: #0ea5e9;
            transform: translateX(5px);
        }
        button { 
            padding: 12px 24px; 
            background: linear-gradient(135deg, #0ea5e9, #0284c7);
            color: white; 
            border: none; 
            border-radius: 12px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s ease;
        }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(2, 132, 199, 0.3); }
        .email-list { background: white; border-radius: 20px; overflow: hidden; box-shadow: 0 5px 20px rgba(0,0,0,0.05); }
        .email-item { padding: 18px; border-bottom: 1px solid #e2e8f0; transition: all 0.3s ease; }
        .email-item:hover { background: #f0f9ff; transform: translateX(5px); }
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
        .btn-sm { padding: 5px 12px; font-size: 12px; margin-left: 8px; border-radius: 8px; text-decoration: none; display: inline-block; }
        .btn-warning { background: #f59e0b; color: white; }
        .alert { padding: 12px 20px; margin-bottom: 25px; border-radius: 12px; animation: slideIn 0.4s ease-out; }
        .alert-success { background: #d1fae5; color: #065f46; }
        .alert-error { background: #fee2e2; color: #991b1b; }
        .alert-warning { background: #fed7aa; color: #92400e; }
        .test-btn {
            background: #10b981;
            margin-left: 10px;
        }
        .brevo-badge {
            display: inline-block;
            background: #0ea5e9;
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 10px;
            margin-left: 8px;
        }
        @media (max-width: 768px) { 
            .row { grid-template-columns: 1fr; }
            .container { padding: 0 15px; }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="nav">
            <div class="logo">🚀 Email Scheduler Pro <span class="brevo-badge">Brevo</span></div>
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
        
        {% if not brevo_configured %}
            <div class="alert alert-warning">
                ⚠️ Brevo API not configured! 
                <a href="/test_brevo" style="color: #92400e;">Click here to set up</a>
            </div>
        {% else %}
            <div style="text-align: right; margin-bottom: 20px;">
                <a href="/test_brevo" class="btn-sm test-btn" style="background: #10b981; padding: 8px 16px; color: white; text-decoration: none;">🔧 Test Brevo API</a>
            </div>
        {% endif %}
        
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
                    <button type="submit">✈️ Send Now via Brevo</button>
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
                            {% if email.brevo_message_id %}
                                <small style="color: #64748b;"> Brevo ID: {{ email.brevo_message_id[:8] }}...</small>
                            {% endif %}
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
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            background: linear-gradient(135deg, #e0f2fe 0%, #bae6fd 100%);
            animation: fadeIn 0.8s ease-in;
        }
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        @keyframes float {
            0%, 100% { transform: translateY(0px); }
            50% { transform: translateY(-10px); }
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
            transition: all 0.4s ease;
            position: relative;
            overflow: hidden;
        }
        .card.current { 
            border: 3px solid #0284c7;
            transform: scale(1.02);
            animation: float 3s ease-in-out infinite;
        }
        .card:hover { transform: translateY(-10px) scale(1.02); }
        .card h3 { font-size: 32px; margin-bottom: 15px; color: #0369a1; }
        .price { font-size: 52px; color: #0284c7; margin: 20px 0; font-weight: bold; }
        .features { list-style: none; margin: 25px 0; text-align: left; display: inline-block; }
        .features li { padding: 10px 0; color: #475569; transition: transform 0.3s ease; }
        .features li:hover { transform: translateX(10px); color: #0284c7; }
        .btn { 
            display: inline-block; 
            padding: 12px 30px; 
            background: linear-gradient(135deg, #0ea5e9, #0284c7);
            color: white; 
            text-decoration: none; 
            border-radius: 50px;
            font-weight: bold;
            transition: all 0.3s ease;
        }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 5px 20px rgba(2, 132, 199, 0.4); }
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
        .brevo-highlight {
            background: linear-gradient(135deg, #e0f2fe, #bae6fd);
            padding: 15px;
            border-radius: 15px;
            margin-top: 20px;
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
                        {% else %}
                            <li>✨ 300 emails/day free</li>
                            <li>📧 100k contacts free</li>
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
            <div class="brevo-highlight">
                <p style="color: #0369a1;">✨ <strong>Why Brevo is perfect for your email app:</strong></p>
                <p style="color: #475569; margin-top: 10px;">✓ 3x more free emails than competitors (300/day vs 100/day)</p>
                <p style="color: #475569;">✓ Transactional & marketing email in one platform</p>
                <p style="color: #475569;">✓ Built-in analytics and tracking</p>
                <p style="color: #475569;">✓ No credit card required for free tier</p>
            </div>
        </div>
    </div>
</body>
</html>
'''

# ==================== MAIN APPLICATION ====================
if __name__ == '__main__':
    # Initialize database with migrations
    init_db()
    
    # Start background email sender thread
    sender_thread = threading.Thread(target=email_sender_thread, daemon=True)
    sender_thread.start()
    
    print("=" * 60)
    print("🚀 Email Scheduler Pro - Brevo API Version (FULLY FIXED)")
    print("=" * 60)
    print("✅ Database initialized with all required columns")
    print("✅ Background email sender started")
    print("✅ Server running at: http://127.0.0.1:5000")
    print("=" * 60)
    print("")
    print("⚙️  BREVO API SETUP:")
    print("   1. Sign up at https://www.brevo.com")
    print("   2. Go to Account → SMTP & API → API Keys")
    print("   3. Create a new API key (v3)")
    print("   4. Update BREVO_CONFIG['api_key'] in the code")
    print("   5. For testing, use 'testing@brevo.com' as from_email")
    print("")
    print("📧 Free Tier Features:")
    print("   • 300 emails/day (9,000/month)")
    print("   • 100,000 contacts storage")
    print("   • Transactional & marketing emails")
    print("   • Visual email template builder")
    print("   • Webhook support")
    print("=" * 60)
    
    # Run the app
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
