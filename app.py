#!/usr/bin/env python3
"""
SIMPLE EMAIL SCHEDULER FOR RENDER
No background threads needed - Uses HTTP endpoints instead
"""

import os
import sqlite3
import re
import requests
import hashlib
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==================== CONFIG ====================
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
BREVO_FROM_EMAIL = os.environ.get('BREVO_FROM_EMAIL', 'your-email@gmail.com')
DATABASE = '/tmp/scheduler.db'

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Simple users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        email TEXT UNIQUE,
        password TEXT
    )''')
    
    # Scheduled emails table
    c.execute('''CREATE TABLE IF NOT EXISTS scheduled (
        id INTEGER PRIMARY KEY,
        recipient TEXT,
        subject TEXT,
        body TEXT,
        send_time TEXT,
        status TEXT DEFAULT 'pending'
    )''')
    
    conn.commit()
    conn.close()
    print("✅ Database ready")

# ==================== EMAIL SENDER ====================
def send_email(to, subject, body):
    """Send email using Brevo"""
    headers = {
        'api-key': BREVO_API_KEY,
        'Content-Type': 'application/json'
    }
    
    data = {
        'sender': {'email': BREVO_FROM_EMAIL},
        'to': [{'email': to}],
        'subject': subject,
        'htmlContent': f"<html><body>{body}</body></html>"
    }
    
    try:
        r = requests.post('https://api.brevo.com/v3/smtp/email', 
                         json=data, headers=headers, timeout=30)
        return r.status_code in (200, 201), r.text
    except Exception as e:
        return False, str(e)

# ==================== MAIN ENDPOINTS ====================
@app.route('/')
def home():
    return '''
    <h1>📧 Simple Scheduler Running</h1>
    <p>Status: ONLINE</p>
    <p>Use these endpoints:</p>
    <ul>
        <li><b>POST /schedule</b> - Schedule an email (form data: recipient, subject, body, send_time)</li>
        <li><b>GET /process</b> - Process pending emails (call this every 5 min)</li>
        <li><b>GET /pending</b> - View pending emails</li>
    </ul>
    <form method="POST" action="/schedule">
        <input name="recipient" placeholder="recipient@email.com" required><br>
        <input name="subject" placeholder="Subject" required><br>
        <textarea name="body" placeholder="Message" required></textarea><br>
        <input name="send_time" placeholder="YYYY-MM-DD HH:MM:SS" required><br>
        <button type="submit">Schedule</button>
    </form>
    '''

@app.route('/schedule', methods=['POST'])
def schedule():
    """Schedule an email"""
    recipient = request.form.get('recipient')
    subject = request.form.get('subject')
    body = request.form.get('body')
    send_time = request.form.get('send_time')
    
    if not all([recipient, subject, body, send_time]):
        return jsonify({'error': 'Missing fields'}), 400
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('INSERT INTO scheduled (recipient, subject, body, send_time) VALUES (?,?,?,?)',
             (recipient, subject, body, send_time))
    conn.commit()
    email_id = c.lastrowid
    conn.close()
    
    return jsonify({'success': True, 'id': email_id, 'scheduled_for': send_time})

@app.route('/process')
def process():
    """Process ALL pending emails - Call this every 5 minutes"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sent_count = 0
    failed_count = 0
    
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    # Get pending emails that are due
    c.execute('SELECT * FROM scheduled WHERE status = "pending" AND send_time <= ?', (now,))
    emails = c.fetchall()
    
    for email in emails:
        success, msg = send_email(email[1], email[2], email[3])
        
        if success:
            c.execute('UPDATE scheduled SET status = "sent" WHERE id = ?', (email[0],))
            sent_count += 1
        else:
            c.execute('UPDATE scheduled SET status = "failed" WHERE id = ?', (email[0],))
            failed_count += 1
        
        conn.commit()
    
    conn.close()
    
    return jsonify({
        'processed_at': now,
        'sent': sent_count,
        'failed': failed_count,
        'total_processed': sent_count + failed_count
    })

@app.route('/pending')
def pending():
    """View all pending emails"""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT id, recipient, subject, send_time, status FROM scheduled ORDER BY send_time')
    emails = [{'id': e[0], 'to': e[1], 'subject': e[2], 'time': e[3], 'status': e[4]} for e in c.fetchall()]
    conn.close()
    return jsonify({'pending_emails': emails})

@app.route('/health')
def health():
    return jsonify({'status': 'alive', 'database': '/tmp/scheduler.db'})

# ==================== RUN ====================
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    print("=" * 50)
    print("🚀 SIMPLE SCHEDULER READY")
    print(f"✅ API: {'CONFIGURED' if BREVO_API_KEY else 'MISSING'}")
    print(f"✅ Port: {port}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port)
