#!/usr/bin/env python3
"""
Auto Scheduled Email Sending App - FOR RENDER
APScheduler runs every 30s automatically + UptimeRobot keeps app alive
"""

import os
import sqlite3
import re
import requests
import hashlib
import atexit
from datetime import datetime
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, g
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ==================== CONFIGURATION ====================
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')

DATABASE = 'email_scheduler.db'

PLANS = {
    'free':  {'name': 'Free',  'emails_per_month': 9000,  'scheduled_emails': 50,   'price': 0},
    'basic': {'name': 'Basic', 'emails_per_month': 20000, 'scheduled_emails': 200,  'price': 9.99},
    'pro':   {'name': 'Pro',   'emails_per_month': 50000, 'scheduled_emails': 1000, 'price': 19.99}
}

def get_brevo_config():
    """Read env vars fresh every call so they work on Render"""
    return {
        'api_key':   os.environ.get('BREVO_API_KEY', ''),
        'from_email': os.environ.get('BREVO_FROM_EMAIL', ''),
        'from_name': 'Email Scheduler Pro',
        'api_url':   'https://api.brevo.com/v3/smtp/email'
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
    print("\u2705 Database initialized")

app.teardown_appcontext(close_db)

with app.app_context():
    init_db()

# ==================== HELPERS ====================
def hash_password(password):
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
    return get_db().execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

def get_user_by_email(email):
    return get_db().execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()

def can_send_email(user):
    return user['emails_sent_this_month'] < PLANS[user['plan']]['emails_per_month']

def can_schedule_email(user):
    db = get_db()
    count = db.execute('SELECT COUNT(*) as c FROM scheduled_emails WHERE user_id = ? AND status = "scheduled"', (user['id'],)).fetchone()['c']
    return count < PLANS[user['plan']]['scheduled_emails']

def get_remaining_emails(user):
    return max(0, PLANS[user['plan']]['emails_per_month'] - user
