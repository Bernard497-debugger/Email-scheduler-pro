#!/usr/bin/env python3
"""
Auto Scheduled Email Sending App - FOR RENDER
Using Brevo API - WORKING VERSION
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

app = Flask(__name__)

# ==================== CONFIGURATION (Read from Environment) ====================
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
BREVO_FROM_EMAIL = os.environ.get('BREVO_FROM_EMAIL', 'testing@brevo.com')
SECRET_KEY = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')

app.secret_key = SECRET_KEY

BREVO_CONFIG = {
    'api_key': BREVO_API_KEY,
    'from_email': BREVO_FROM_EMAIL,
    'from_name': 'Email Scheduler Pro',
    'api_url': 'https://api.brevo.com/v3/smtp/email'
}

DATABASE = 'email_scheduler.db'

PLANS = {
    'free': {'name': 'Free', 'emails_per_month': 9000, 'scheduled_emails': 50, 'price': 0},
    'basic': {'name': 'Basic', 'emails_per_month': 20000, 'scheduled_emails': 200, 'price': 9.99},
    'pro': {'name': 'Pro', 'emails_per_month': 50000, 'scheduled_emails': 1000, 'price': 19.99}
}

# ==================== DATABASE ====================
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
    db = sqlite3.connect(DATABASE)
    c = db.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        plan TEXT DEFAULT 'free',
        emails_sent_this_month INTEGER DEFAULT 0,
        last_reset DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
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
        FOREIGN KEY (user_id) REFERENCES users (id)
    )''')
    db.commit()
    db.close()
    print("✅ Database initialized")

app.teardown_appcontext(close_db)

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
            flash('Please login', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_user(user_id):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

def get_user_by_email(email):
    db = get_db()
    return db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()

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

# ==================== EMAIL FUNCTION ====================
def send_email_via_brevo(to_email, subject, body, user_email=None):
    try:
        headers = {
            'api-key': BREVO_CONFIG['api_key'],
            'Content-Type': 'application/json'
        }
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family:Arial;padding:20px;background:#f0f9ff">
            <div style="max-width:600px;margin:0 auto;background:white;border-radius:20px;overflow:hidden">
                <div style="background:linear-gradient(135deg,#0284c7,#0ea5e9);color:white;padding:30px;text-align:center">
                    <h2>📧 Email Scheduler Pro</h2>
                </div>
                <div style="padding:30px">
                    {body.replace(chr(10), '<br>')}
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
            return True, "Sent", response.json().get('messageId', 'ok')
        else:
            error_msg = response.json().get('message', 'Unknown error')
            return False, f"API error: {error_msg}", None
    except Exception as e:
        return False, str(e), None

# ==================== AUTO-SCHEDULER ====================
def send_due_emails():
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        c = db.cursor()
        
        now = datetime.now()
        print(f"[{now.strftime('%H:%M:%S')}] Checking for due emails...")
        
        c.execute('''
            SELECT * FROM scheduled_emails 
            WHERE datetime(scheduled_time) <= datetime(?)
            AND status = 'scheduled'
        ''', (now.isoformat(),))
        
        due_emails = c.fetchall()
        
        for email in due_emails:
            print(f"  → Sending to: {email['recipient_email']}")
            
            user = c.execute('SELECT * FROM users WHERE id = ?', (email['user_id'],)).fetchone()
            
            if not user:
                continue
            
            success, msg, msg_id = send_email_via_brevo(
                email['recipient_email'],
                email['subject'],
                email['body'],
                user['email']
            )
            
            if success:
                print(f"    ✓ Sent!")
                c.execute('UPDATE scheduled_emails SET status = "sent", sent_at = ?, brevo_message_id = ? WHERE id = ?', 
                         (now.isoformat(), msg_id, email['id']))
                c.execute('UPDATE users SET emails_sent_this_month = emails_sent_this_month + 1 WHERE id = ?', (user['id'],))
                db.commit()
            else:
                print(f"    ✗ Failed: {msg}")
                retry = email['retry_count'] + 1
                new_status = 'failed' if retry >= 3 else 'scheduled'
                c.execute('UPDATE scheduled_emails SET retry_count = ?, status = ?, error_message = ? WHERE id = ?', 
                         (retry, new_status, msg, email['id']))
                db.commit()
        
        db.close()
    except Exception as e:
        print(f"Error: {e}")

def scheduler_thread():
    print("🔄 Auto-scheduler started (checks every 30s)")
    while True:
        send_due_emails()
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
            flash('Password too short', 'error')
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
            flash(f'Welcome back!', 'success')
            return redirect(url_for('dashboard'))
        
        flash('Invalid email or password', 'error')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out', 'info')
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
                                remaining_scheduled=get_remaining_scheduled(user))

@app.route('/schedule_email', methods=['POST'])
@login_required
def schedule_email():
    user = get_user(session['user_id'])
    
    if not can_schedule_email(user):
        flash('Schedule limit reached', 'error')
        return redirect(url_for('dashboard'))
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    dt_str = request.form.get('schedule_datetime', '')
    
    if not all([recipient, subject, body, dt_str]):
        flash('All fields required', 'error')
        return redirect(url_for('dashboard'))
    
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', recipient):
        flash('Invalid email', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        scheduled = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
        if scheduled <= datetime.now():
            flash('Future time required', 'error')
            return redirect(url_for('dashboard'))
        
        db = get_db()
        db.execute('INSERT INTO scheduled_emails (user_id, recipient_email, subject, body, scheduled_time) VALUES (?, ?, ?, ?, ?)', 
                   (user['id'], recipient, subject, body, scheduled.isoformat()))
        db.commit()
        
        flash(f'✅ Email scheduled for {scheduled.strftime("%Y-%m-%d %H:%M")}', 'success')
    except ValueError:
        flash('Invalid date/time', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/send_now', methods=['POST'])
@login_required
def send_now():
    user = get_user(session['user_id'])
    
    recipient = request.form.get('recipient', '').strip()
    subject = request.form.get('subject', '').strip()
    body = request.form.get('body', '').strip()
    
    if not all([recipient, subject, body]):
        flash('All fields required', 'error')
        return redirect(url_for('dashboard'))
    
    if not can_send_email(user):
        flash('Monthly limit reached', 'error')
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
        flash('Scheduled email cancelled', 'success')
    else:
        flash('Cannot cancel', 'error')
    
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
    user = get_user(session['user_id'])
    success, msg, _ = send_email_via_brevo(user['email'], 'Test Email', f'Test at {datetime.now()}', user['email'])
    
    if success:
        flash('✅ Test email sent!', 'success')
    else:
        flash(f'❌ Failed: {msg}', 'error')
    
    return redirect(url_for('dashboard'))

# ==================== HTML TEMPLATES ====================
INDEX_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><title>Email Scheduler Pro</title><style>
*{margin:0;padding:0}
body{font-family:Arial;background:linear-gradient(135deg,#e0f2fe,#bae6fd)}
.header{background:white;padding:15px 20px;box-shadow:0 2px 10px rgba(0,0,0,0.1)}
.nav{display:flex;justify-content:space-between;max-width:1200px;margin:0 auto}
.logo{font-size:24px;font-weight:bold;color:#0284c7}
.nav-links a{margin-left:20px;text-decoration:none;color:#0369a1}
.hero{text-align:center;padding:80px 20px}
.hero h1{font-size:48px;color:#0369a1}
.btn{display:inline-block;padding:14px 35px;background:#0284c7;color:white;text-decoration:none;border-radius:50px}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:30px;max-width:1200px;margin:40px auto}
.card{background:white;border-radius:20px;padding:30px;text-align:center}
.price{font-size:48px;color:#0284c7;margin:20px 0}
</style></head>
<body>
<div class="header"><div class="nav"><div class="logo">🚀 Email Scheduler Pro</div><div class="nav-links">{% if session.user_id %}<a href="/dashboard">Dashboard</a><a href="/logout">Logout</a>{% else %}<a href="/login">Login</a><a href="/register">Register</a>{% endif %}</div></div></div>
<div class="hero"><h1>⚡ Schedule Emails Automatically</h1><p>Powered by Brevo API</p>{% if not session.user_id %}<a href="/register" class="btn">Get Started Free</a>{% endif %}</div>
<div class="pricing-grid">{% for name,plan in plans.items() %}<div class="card"><h3>{{plan.name}}</h3><div class="price">${{"%.2f"|format(plan.price)}}/mo</div><ul style="list-style:none"><li>📧 {{plan.emails_per_month}} emails/month</li><li>⏰ {{plan.scheduled_emails}} scheduled</li></ul></div>{% endfor %}</div>
</body>
</html>
'''

LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><title>Login</title><style>
body{font-family:Arial;background:linear-gradient(135deg,#e0f2fe,#bae6fd);display:flex;justify-content:center;align-items:center;height:100vh}
.card{background:white;padding:40px;border-radius:20px;width:400px}
input{width:100%;padding:10px;margin:10px 0;border:1px solid #ddd;border-radius:5px}
button{width:100%;padding:10px;background:#0284c7;color:white;border:none;border-radius:5px}
.alert{padding:10px;margin:10px 0;border-radius:5px}
.alert-success{background:#d4edda;color:#155724}
.alert-error{background:#f8d7da;color:#721c24}
</style></head>
<body><div class="card"><h2>Login</h2>{% with m=get_flashed_messages(with_categories=true) %}{% for c,msg in m %}<div class="alert alert-{{c}}">{{msg}}</div>{% endfor %}{% endwith %}<form method=POST><input type=email name=email placeholder="Email" required><input type=password name=password placeholder="Password" required><button type=submit>Login</button></form><p style="text-align:center;margin-top:20px"><a href="/register">Create Account</a></p></div></body>
</html>
'''

REGISTER_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><title>Register</title><style>
body{font-family:Arial;background:linear-gradient(135deg,#e0f2fe,#bae6fd);display:flex;justify-content:center;align-items:center;height:100vh}
.card{background:white;padding:40px;border-radius:20px;width:400px}
input{width:100%;padding:10px;margin:10px 0;border:1px solid #ddd;border-radius:5px}
button{width:100%;padding:10px;background:#0284c7;color:white;border:none;border-radius:5px}
.alert{padding:10px;margin:10px 0;border-radius:5px}
</style></head>
<body><div class="card"><h2>Register</h2>{% with m=get_flashed_messages(with_categories=true) %}{% for c,msg in m %}<div class="alert alert-{{c}}">{{msg}}</div>{% endfor %}{% endwith %}<form method=POST><input type=email name=email placeholder="Email" required><input type=password name=password placeholder="Password (min 4)" required><input type=password name=confirm_password placeholder="Confirm" required><button type=submit>Register</button></form><p style="text-align:center;margin-top:20px"><a href="/login">Login</a></p></div></body>
</html>
'''

DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><title>Dashboard</title><style>
*{margin:0;padding:0}
body{font-family:Arial;background:#f0f9ff}
.header{background:white;padding:15px 20px;box-shadow:0 2px 5px rgba(0,0,0,0.1)}
.nav{display:flex;justify-content:space-between;max-width:1200px;margin:0 auto}
.container{max-width:1200px;margin:20px auto;padding:0 20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-bottom:30px}
.stat-card{background:white;padding:20px;border-radius:10px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:30px}
.card{background:white;padding:20px;border-radius:10px}
input,textarea{width:100%;padding:10px;margin:10px 0;border:1px solid #ddd;border-radius:5px}
button{padding:10px 20px;background:#0284c7;color:white;border:none;border-radius:5px;cursor:pointer}
.email-list{background:white;border-radius:10px;overflow:hidden}
.email-item{padding:15px;border-bottom:1px solid #ddd}
.status{display:inline-block;padding:2px 8px;border-radius:3px;font-size:12px}
.status-scheduled{background:#fff3cd;color:#856404}
.status-sent{background:#d4edda;color:#155724}
.alert{padding:10px;margin-bottom:20px;border-radius:5px}
.alert-success{background:#d4edda;color:#155724}
.alert-error{background:#f8d7da;color:#721c24}
.test-btn{background:#10b981;padding:5px 10px;border-radius:5px;color:white;text-decoration:none}
@media (max-width:768px){.row{grid-template-columns:1fr}}
</style></head>
<body>
<div class="header"><div class="nav"><div class="logo">📧 Email Scheduler Pro</div><div><span>{{plan_name|capitalize}} Plan</span><a href="/upgrade_plan" style="margin-left:20px">Upgrade</a><a href="/logout" style="margin-left:20px">Logout</a></div></div></div>
<div class="container">
{% with m=get_flashed_messages(with_categories=true) %}{% for c,msg in m %}<div class="alert alert-{{c}}">{{msg}}</div>{% endfor %}{% endwith %}
<div style="text-align:right;margin-bottom:20px"><a href="/test_brevo" class="test-btn">🔧 Test API</a></div>
<div class="stats"><div class="stat-card"><h3>Monthly Usage</h3><div class="number">{{user.emails_sent_this_month}} / {{plan.emails_per_month}}</div><div style="background:#e2e8f0;border-radius:10px;margin-top:10px"><div style="width:{{usage_percentage}}%;height:10px;background:#0284c7;border-radius:10px"></div></div><small>{{remaining_emails}} remaining</small></div><div class="stat-card"><h3>Scheduled</h3><div class="number">{{remaining_scheduled}} / {{plan.scheduled_emails}}</div><small>slots available</small></div></div>
<div class="row"><div class="card"><h3>Send Now</h3><form method=POST action="/send_now"><input type=email name=recipient placeholder="Recipient Email" required><input type=text name=subject placeholder="Subject" required><textarea name=body rows=3 placeholder="Message" required></textarea><button type=submit>Send Now</button></form></div>
<div class="card"><h3>Schedule Email</h3><form method=POST action="/schedule_email"><input type=email name=recipient placeholder="Recipient Email" required><input type=text name=subject placeholder="Subject" required><textarea name=body rows=2 placeholder="Message" required></textarea><input type=datetime-local name=schedule_datetime required><button type=submit>Schedule</button></form></div></div>
<div class="email-list"><div style="padding:15px;background:#f8f9fa;font-weight:bold">Scheduled Emails</div>{% if scheduled_emails %}{% for e in scheduled_emails %}<div class="email-item"><strong>To:</strong> {{e.recipient_email}}<br><strong>Subject:</strong> {{e.subject[:50]}}<br><strong>Scheduled:</strong> {{e.scheduled_time[:16].replace('T',' ')}}<br><span class="status status-{{e.status}}">{{e.status}}</span>{% if e.status=='scheduled' %} <a href="/cancel_scheduled/{{e.id}}" onclick="return confirm('Cancel?')">Cancel</a>{% endif %}</div>{% endfor %}{% else %}<div style="padding:15px;text-align:center;color:#999">No scheduled emails</div>{% endif %}</div>
</div>
</body>
</html>
'''

UPGRADE_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><title>Upgrade</title><style>
body{font-family:Arial;background:linear-gradient(135deg,#e0f2fe,#bae6fd)}
.container{max-width:1200px;margin:50px auto;padding:20px}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}
.card{background:white;border-radius:10px;padding:30px;text-align:center}
.card.current{border:2px solid #0284c7}
.price{font-size:36px;color:#0284c7;margin:20px 0}
.btn{display:inline-block;padding:10px 20px;background:#0284c7;color:white;text-decoration:none;border-radius:5px}
</style></head>
<body><div class="container"><h2 style="text-align:center">Upgrade Plan</h2><div class="pricing-grid">{% for name,plan in plans.items() %}<div class="card {% if name==current_plan %}current{% endif %}"><h3>{{plan.name}}</h3><div class="price">${{"%.2f"|format(plan.price)}}/mo</div><ul style="list-style:none"><li>📧 {{plan.emails_per_month}} emails/month</li><li>⏰ {{plan.scheduled_emails}} scheduled</li></ul>{% if name==current_plan %}<button disabled>Current</button>{% else %}<a href="/change_plan/{{name}}" class="btn">Switch</a>{% endif %}</div>{% endfor %}</div></div></body>
</html>
'''

# ==================== MAIN ====================
if __name__ == '__main__':
    # Start scheduler
    thread = threading.Thread(target=scheduler_thread, daemon=True)
    thread.start()
    time.sleep(1)
    
    port = int(os.environ.get('PORT', 5000))
    print("=" * 50)
    print("🚀 Email Scheduler Pro - LIVE")
    print(f"✅ Server: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
