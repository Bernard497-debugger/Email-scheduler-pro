# app.py - Complete Auto Email Scheduler with Brevo API
# Deploy this single file to Render

import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from flask import Flask, render_template_string, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Configuration
BREVO_API_KEY = os.getenv('BREVO_API_KEY')
BREVO_SENDER_EMAIL = os.getenv('BREVO_SENDER_EMAIL')
BREVO_SENDER_NAME = os.getenv('BREVO_SENDER_NAME', 'Email Scheduler')

# Initialize database
def init_db():
    conn = sqlite3.connect('email_scheduler.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT UNIQUE,
            schedule_time TIMESTAMP,
            recipients TEXT,
            subject TEXT,
            html_content TEXT,
            template_id INTEGER,
            recurring TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Brevo Email Scheduler Class
class BrevoEmailScheduler:
    def __init__(self):
        """Initialize Brevo API client"""
        if not BREVO_API_KEY:
            print("⚠️ Warning: BREVO_API_KEY not set")
            return
            
        self.configuration = sib_api_v3_sdk.Configuration()
        self.configuration.api_key['api-key'] = BREVO_API_KEY
        self.api_client = sib_api_v3_sdk.ApiClient(self.configuration)
        self.email_api = sib_api_v3_sdk.TransactionalEmailsApi(self.api_client)
        self.contacts_api = sib_api_v3_sdk.ContactsApi(self.api_client)
    
    def send_transactional_email(self, 
                                 to_emails: List[Dict],
                                 subject: str,
                                 html_content: str = None,
                                 template_id: int = None,
                                 params: Dict = None) -> bool:
        """Send transactional email via Brevo"""
        if not BREVO_API_KEY:
            print("❌ Brevo API key not configured")
            return False
            
        try:
            send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
                to=to_emails,
                sender={'email': BREVO_SENDER_EMAIL, 'name': BREVO_SENDER_NAME},
                subject=subject,
                html_content=html_content
            )
            
            if template_id:
                send_smtp_email.template_id = template_id
                if params:
                    send_smtp_email.params = params
            
            response = self.email_api.send_transac_email(send_smtp_email)
            print(f"✅ Email sent! Message ID: {response.message_id}")
            return True
            
        except ApiException as e:
            print(f"❌ Failed: {e}")
            return False

# Initialize scheduler and email client
email_scheduler = BrevoEmailScheduler()
background_scheduler = BackgroundScheduler()

# HTML Template for Dashboard
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Email Scheduler - Brevo API</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { background: white; border-radius: 15px; padding: 30px; margin-bottom: 30px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); text-align: center; }
        .header h1 { color: #667eea; margin-bottom: 10px; }
        .header p { color: #666; }
        .tabs { display: flex; gap: 10px; margin-bottom: 30px; flex-wrap: wrap; }
        .tab { background: white; padding: 12px 24px; border: none; border-radius: 10px; cursor: pointer; font-size: 16px; font-weight: 500; transition: all 0.3s; color: #666; }
        .tab.active { background: #667eea; color: white; transform: translateY(-2px); box-shadow: 0 5px 15px rgba(102,126,234,0.3); }
        .card { background: white; border-radius: 15px; padding: 30px; margin-bottom: 20px; box-shadow: 0 5px 20px rgba(0,0,0,0.08); }
        .card h2 { color: #333; margin-bottom: 20px; font-size: 24px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; font-weight: 500; color: #555; }
        input, select, textarea { width: 100%; padding: 12px; border: 2px solid #e1e5e9; border-radius: 8px; font-size: 14px; transition: all 0.3s; font-family: inherit; }
        input:focus, select:focus, textarea:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102,126,234,0.1); }
        button { background: #667eea; color: white; border: none; padding: 12px 30px; border-radius: 8px; cursor: pointer; font-size: 16px; font-weight: 500; transition: all 0.3s; }
        button:hover { background: #5a67d8; transform: translateY(-2px); box-shadow: 0 5px 15px rgba(102,126,234,0.3); }
        .delete-btn { background: #fc8181; }
        .delete-btn:hover { background: #f56565; }
        .success { color: #48bb78; padding: 10px; background: #f0fff4; border-radius: 8px; margin-top: 15px; }
        .error { color: #fc8181; padding: 10px; background: #fff5f5; border-radius: 8px; margin-top: 15px; }
        .job-list { margin-top: 20px; }
        .job-item { background: #f7fafc; padding: 15px; margin-bottom: 10px; border-radius: 10px; display: flex; justify-content: space-between; align-items: center; transition: all 0.3s; }
        .job-item:hover { transform: translateX(5px); box-shadow: 0 2px 10px rgba(0,0,0,0.05); }
        .job-info strong { color: #667eea; }
        .job-info small { color: #718096; display: block; margin-top: 5px; }
        .status-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 500; }
        .status-running { background: #c6f6d5; color: #22543d; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media (max-width: 768px) { .grid-2 { grid-template-columns: 1fr; } }
        .info-box { background: #ebf8ff; border-left: 4px solid #667eea; padding: 15px; border-radius: 8px; margin-bottom: 20px; }
        .info-box h3 { color: #2c5282; margin-bottom: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📧 Auto Email Scheduler</h1>
            <p>Powered by Brevo API | Schedule and send automated emails</p>
        </div>

        <div class="tabs">
            <button class="tab active" onclick="showTab('send')">📨 Send Now</button>
            <button class="tab" onclick="showTab('schedule')">📅 Schedule</button>
            <button class="tab" onclick="showTab('recurring')">🔄 Recurring</button>
            <button class="tab" onclick="showTab('jobs')">📋 Scheduled Jobs</button>
        </div>

        <!-- Send Now Tab -->
        <div id="send" class="tab-content" style="display: block;">
            <div class="card">
                <h2>Send Email Immediately</h2>
                <div class="grid-2">
                    <div class="form-group">
                        <label>📧 To Email *</label>
                        <input type="email" id="send_to_email" placeholder="recipient@example.com">
                    </div>
                    <div class="form-group">
                        <label>👤 To Name</label>
                        <input type="text" id="send_to_name" placeholder="Recipient name">
                    </div>
                </div>
                <div class="form-group">
                    <label>📝 Subject *</label>
                    <input type="text" id="send_subject" placeholder="Email subject">
                </div>
                <div class="form-group">
                    <label>📄 Content (HTML) *</label>
                    <textarea id="send_content" rows="6" placeholder="<h1>Hello!</h1><p>Your message here</p>"></textarea>
                </div>
                <div class="form-group">
                    <label>🎨 Template ID (Optional)</label>
                    <input type="number" id="send_template_id" placeholder="Brevo template ID">
                </div>
                <button onclick="sendEmailNow()">✉️ Send Email Now</button>
                <div id="send_result"></div>
            </div>
        </div>

        <!-- Schedule Tab -->
        <div id="schedule" class="tab-content" style="display: none;">
            <div class="card">
                <h2>Schedule Email for Later</h2>
                <div class="grid-2">
                    <div class="form-group">
                        <label>📧 To Email *</label>
                        <input type="email" id="schedule_to_email" placeholder="recipient@example.com">
                    </div>
                    <div class="form-group">
                        <label>👤 To Name</label>
                        <input type="text" id="schedule_to_name" placeholder="Recipient name">
                    </div>
                </div>
                <div class="form-group">
                    <label>📝 Subject *</label>
                    <input type="text" id="schedule_subject" placeholder="Email subject">
                </div>
                <div class="form-group">
                    <label>📄 Content (HTML) *</label>
                    <textarea id="schedule_content" rows="6" placeholder="<h1>Hello!</h1><p>Your message here</p>"></textarea>
                </div>
                <div class="form-group">
                    <label>⏰ Schedule Date & Time *</label>
                    <input type="datetime-local" id="schedule_datetime">
                </div>
                <button onclick="scheduleEmail()">📅 Schedule Email</button>
                <div id="schedule_result"></div>
            </div>
        </div>

        <!-- Recurring Tab -->
        <div id="recurring" class="tab-content" style="display: none;">
            <div class="card">
                <h2>Set Recurring Email</h2>
                <div class="grid-2">
                    <div class="form-group">
                        <label>📧 To Email *</label>
                        <input type="email" id="recurring_to_email" placeholder="recipient@example.com">
                    </div>
                    <div class="form-group">
                        <label>👤 To Name</label>
                        <input type="text" id="recurring_to_name" placeholder="Recipient name">
                    </div>
                </div>
                <div class="form-group">
                    <label>📝 Subject *</label>
                    <input type="text" id="recurring_subject" placeholder="Email subject">
                </div>
                <div class="form-group">
                    <label>📄 Content (HTML) *</label>
                    <textarea id="recurring_content" rows="6" placeholder="<h1>Hello!</h1><p>Your message here</p>"></textarea>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>🔄 Frequency *</label>
                        <select id="recurring_frequency">
                            <option value="daily">Daily</option>
                            <option value="weekly">Weekly</option>
                            <option value="monthly">Monthly</option>
                        </select>
                    </div>
                    <div class="form-group" id="weekly_day_group" style="display:none;">
                        <label>📆 Day of Week</label>
                        <select id="weekly_day">
                            <option value="mon">Monday</option>
                            <option value="tue">Tuesday</option>
                            <option value="wed">Wednesday</option>
                            <option value="thu">Thursday</option>
                            <option value="fri">Friday</option>
                            <option value="sat">Saturday</option>
                            <option value="sun">Sunday</option>
                        </select>
                    </div>
                    <div class="form-group" id="monthly_day_group" style="display:none;">
                        <label>📆 Day of Month</label>
                        <input type="number" id="monthly_day" min="1" max="31" value="1">
                    </div>
                    <div class="form-group">
                        <label>🕐 Hour (0-23)</label>
                        <input type="number" id="recurring_hour" min="0" max="23" value="9">
                    </div>
                    <div class="form-group">
                        <label>🕐 Minute (0-59)</label>
                        <input type="number" id="recurring_minute" min="0" max="59" value="0">
                    </div>
                </div>
                <button onclick="scheduleRecurring()">🔄 Set Recurring Email</button>
                <div id="recurring_result"></div>
            </div>
        </div>

        <!-- Jobs Tab -->
        <div id="jobs" class="tab-content" style="display: none;">
            <div class="card">
                <h2>📋 Scheduled Jobs</h2>
                <div class="info-box">
                    <h3>ℹ️ About Scheduled Jobs</h3>
                    <p>All scheduled and recurring emails are managed here. Jobs persist even after server restarts.</p>
                </div>
                <div id="jobs_list" class="job-list">Loading...</div>
                <button onclick="refreshJobs()" style="margin-top: 15px;">🔄 Refresh List</button>
            </div>
        </div>
    </div>

    <script>
        let currentTab = 'send';
        
        document.getElementById('recurring_frequency').addEventListener('change', function() {
            const weeklyGroup = document.getElementById('weekly_day_group');
            const monthlyGroup = document.getElementById('monthly_day_group');
            
            weeklyGroup.style.display = this.value === 'weekly' ? 'block' : 'none';
            monthlyGroup.style.display = this.value === 'monthly' ? 'block' : 'none';
        });

        function showTab(tabName) {
            currentTab = tabName;
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(content => content.style.display = 'none');
            
            event.target.classList.add('active');
            document.getElementById(tabName).style.display = 'block';
            
            if (tabName === 'jobs') {
                refreshJobs();
            }
        }

        async function sendEmailNow() {
            const data = {
                to_email: document.getElementById('send_to_email').value,
                to_name: document.getElementById('send_to_name').value,
                subject: document.getElementById('send_subject').value,
                html_content: document.getElementById('send_content').value,
                template_id: parseInt(document.getElementById('send_template_id').value) || null
            };

            if (!data.to_email || !data.subject || !data.html_content) {
                document.getElementById('send_result').innerHTML = '<div class="error">❌ Please fill in all required fields (*)</div>';
                return;
            }

            try {
                const response = await fetch('/api/send-email', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                const result = await response.json();
                document.getElementById('send_result').innerHTML = 
                    `<div class="${result.success ? 'success' : 'error'}">${result.success ? '✅ ' + result.message : '❌ ' + result.message}</div>`;
                
                if (result.success) {
                    // Clear form
                    document.getElementById('send_to_email').value = '';
                    document.getElementById('send_subject').value = '';
                    document.getElementById('send_content').value = '';
                }
            } catch (error) {
                document.getElementById('send_result').innerHTML = `<div class="error">❌ Error: ${error.message}</div>`;
            }
        }

        async function scheduleEmail() {
            const datetime = document.getElementById('schedule_datetime').value;
            if (!datetime) {
                document.getElementById('schedule_result').innerHTML = '<div class="error">❌ Please select a date and time</div>';
                return;
            }

            const data = {
                to_email: document.getElementById('schedule_to_email').value,
                to_name: document.getElementById('schedule_to_name').value,
                subject: document.getElementById('schedule_subject').value,
                html_content: document.getElementById('schedule_content').value,
                schedule_time: datetime
            };

            if (!data.to_email || !data.subject || !data.html_content) {
                document.getElementById('schedule_result').innerHTML = '<div class="error">❌ Please fill in all required fields (*)</div>';
                return;
            }

            try {
                const response = await fetch('/api/schedule-email', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                const result = await response.json();
                document.getElementById('schedule_result').innerHTML = 
                    `<div class="${result.success ? 'success' : 'error'}">${result.success ? '✅ ' + result.message : '❌ ' + result.message}</div>`;
                if (result.success) refreshJobs();
            } catch (error) {
                document.getElementById('schedule_result').innerHTML = `<div class="error">❌ Error: ${error.message}</div>`;
            }
        }

        async function scheduleRecurring() {
            const data = {
                to_email: document.getElementById('recurring_to_email').value,
                to_name: document.getElementById('recurring_to_name').value,
                subject: document.getElementById('recurring_subject').value,
                html_content: document.getElementById('recurring_content').value,
                frequency: document.getElementById('recurring_frequency').value,
                hour: parseInt(document.getElementById('recurring_hour').value),
                minute: parseInt(document.getElementById('recurring_minute').value),
                day: document.getElementById('weekly_day')?.value,
                day_of_month: parseInt(document.getElementById('monthly_day')?.value) || 1
            };

            if (!data.to_email || !data.subject || !data.html_content) {
                document.getElementById('recurring_result').innerHTML = '<div class="error">❌ Please fill in all required fields (*)</div>';
                return;
            }

            try {
                const response = await fetch('/api/schedule-recurring', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                const result = await response.json();
                document.getElementById('recurring_result').innerHTML = 
                    `<div class="${result.success ? 'success' : 'error'}">${result.success ? '✅ ' + result.message : '❌ ' + result.message}</div>`;
                if (result.success) refreshJobs();
            } catch (error) {
                document.getElementById('recurring_result').innerHTML = `<div class="error">❌ Error: ${error.message}</div>`;
            }
        }

        async function refreshJobs() {
            try {
                const response = await fetch('/api/jobs');
                const data = await response.json();
                const jobsList = document.getElementById('jobs_list');
                
                if (data.jobs.length === 0) {
                    jobsList.innerHTML = '<div style="text-align: center; padding: 40px; color: #718096;">📭 No scheduled jobs found</div>';
                    return;
                }
                
                jobsList.innerHTML = data.jobs.map(job => `
                    <div class="job-item">
                        <div class="job-info">
                            <strong>${job.id}</strong>
                            <small>Next run: ${job.next_run_time || 'Not scheduled'}</small>
                            <small>Trigger: ${job.trigger}</small>
                        </div>
                        <button class="delete-btn" onclick="deleteJob('${job.id}')">🗑️ Delete</button>
                    </div>
                `).join('');
            } catch (error) {
                document.getElementById('jobs_list').innerHTML = `<div class="error">❌ Error loading jobs: ${error.message}</div>`;
            }
        }

        async function deleteJob(jobId) {
            if (!confirm('Delete this scheduled job?')) return;
            
            try {
                const response = await fetch(`/api/jobs/${jobId}`, {method: 'DELETE'});
                const result = await response.json();
                if (result.success) {
                    refreshJobs();
                    alert('✅ Job deleted successfully');
                } else {
                    alert('❌ Failed to delete job');
                }
            } catch (error) {
                alert('❌ Error: ' + error.message);
            }
        }

        // Set default datetime to tomorrow
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        tomorrow.setHours(9, 0, 0);
        document.getElementById('schedule_datetime').value = tomorrow.toISOString().slice(0, 16);
        
        // Auto-refresh jobs every 30 seconds
        setInterval(() => {
            if (currentTab === 'jobs') refreshJobs();
        }, 30000);
    </script>
</body>
</html>
"""

# Flask Routes
@app.route('/')
def index():
    """Dashboard home page"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/send-email', methods=['POST'])
def send_email():
    """Send an immediate email"""
    try:
        data = request.json
        recipients = [{'email': data['to_email'], 'name': data.get('to_name', '')}]
        
        success = email_scheduler.send_transactional_email(
            to_emails=recipients,
            subject=data['subject'],
            html_content=data['html_content'],
            template_id=data.get('template_id')
        )
        
        return jsonify({
            'success': success,
            'message': 'Email sent successfully' if success else 'Failed to send email'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 400

@app.route('/api/schedule-email', methods=['POST'])
def schedule_email():
    """Schedule an email for future delivery"""
    try:
        data = request.json
        schedule_time = datetime.fromisoformat(data['schedule_time'])
        recipients = [{'email': data['to_email'], 'name': data.get('to_name', '')}]
        
        job_id = f"one_time_{datetime.now().timestamp()}"
        
        def scheduled_send():
            email_scheduler.send_transactional_email(
                to_emails=recipients,
                subject=data['subject'],
                html_content=data['html_content'],
                template_id=data.get('template_id')
            )
        
        background_scheduler.add_job(
            func=scheduled_send,
            trigger='date',
            run_date=schedule_time,
            id=job_id
        )
        
        # Save to database
        conn = sqlite3.connect('email_scheduler.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO scheduled_emails (job_id, schedule_time, recipients, subject, html_content)
            VALUES (?, ?, ?, ?, ?)
        ''', (job_id, schedule_time.isoformat(), json.dumps(recipients), data['subject'], data['html_content']))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'message': f'Email scheduled for {schedule_time}'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 400

@app.route('/api/schedule-recurring', methods=['POST'])
def schedule_recurring():
    """Schedule recurring emails"""
    try:
        data = request.json
        recipients = [{'email': data['to_email'], 'name': data.get('to_name', '')}]
        
        def send_recurring_email():
            email_scheduler.send_transactional_email(
                to_emails=recipients,
                subject=data['subject'],
                html_content=data['html_content'],
                template_id=data.get('template_id')
            )
        
        # Create trigger based on frequency
        if data['frequency'] == 'daily':
            trigger = CronTrigger(hour=data.get('hour', 9), minute=data.get('minute', 0))
            job_id = f"daily_{data['to_email']}_{data.get('hour', 9)}_{data.get('minute', 0)}"
        elif data['frequency'] == 'weekly':
            trigger = CronTrigger(
                day_of_week=data.get('day', 'mon'),
                hour=data.get('hour', 9),
                minute=data.get('minute', 0)
            )
            job_id = f"weekly_{data['to_email']}_{data.get('day', 'mon')}"
        elif data['frequency'] == 'monthly':
            trigger = CronTrigger(
                day=data.get('day_of_month', 1),
                hour=data.get('hour', 9),
                minute=data.get('minute', 0)
            )
            job_id = f"monthly_{data['to_email']}_{data.get('day_of_month', 1)}"
        else:
            return jsonify({'success': False, 'message': 'Invalid frequency'}), 400
        
        background_scheduler.add_job(
            func=send_recurring_email,
            trigger=trigger,
            id=job_id,
            replace_existing=True
        )
        
        # Save to database
        conn = sqlite3.connect('email_scheduler.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO scheduled_emails (job_id, recipients, subject, html_content, recurring)
            VALUES (?, ?, ?, ?, ?)
        ''', (job_id, json.dumps(recipients), data['subject'], data['html_content'], data['frequency']))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'message': f'{data["frequency"].capitalize()} email scheduled'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 400

@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    """List all scheduled jobs"""
    jobs = []
    for job in background_scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'next_run_time': str(job.next_run_time) if job.next_run_time else None,
            'trigger': str(job.trigger)
        })
    return jsonify({'jobs': jobs})

@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    """Delete a scheduled job"""
    try:
        background_scheduler.remove_job(job_id)
        
        # Remove from database
        conn = sqlite3.connect('email_scheduler.db')
        cursor = conn.cursor()
        cursor.execute('DELETE FROM scheduled_emails WHERE job_id = ?', (job_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Job deleted'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 400

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Render"""
    return jsonify({
        'status': 'healthy',
        'scheduler_running': background_scheduler.running,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/keep-alive', methods=['GET'])
def keep_alive():
    """Keep the service awake"""
    return jsonify({'status': 'awake', 'timestamp': datetime.now().isoformat()})

# Load saved jobs from database on startup
def load_saved_jobs():
    """Load saved jobs from database on startup"""
    conn = sqlite3.connect('email_scheduler.db')
    cursor = conn.cursor()
    cursor.execute('SELECT job_id, schedule_time, recipients, subject, html_content, recurring FROM scheduled_emails')
    rows = cursor.fetchall()
    
    for row in rows:
        job_id, schedule_time, recipients, subject, html_content, recurring = row
        recipients_list = json.loads(recipients)
        
        def create_job_func(recipients, subject, html_content):
            def send_func():
                email_scheduler.send_transactional_email(recipients, subject, html_content)
            return send_func
        
        if recurring:
            # Recurring job
            if recurring == 'daily':
                trigger = CronTrigger(hour=9, minute=0)
            elif recurring == 'weekly':
                trigger = CronTrigger(day_of_week='mon', hour=9, minute=0)
            else:
                trigger = CronTrigger(day=1, hour=9, minute=0)
            
            background_scheduler.add_job(
                func=create_job_func(recipients_list, subject, html_content),
                trigger=trigger,
                id=job_id,
                replace_existing=True
            )
        elif schedule_time:
            # One-time job
            schedule_dt = datetime.fromisoformat(schedule_time)
            if schedule_dt > datetime.now():
                background_scheduler.add_job(
                    func=create_job_func(recipients_list, subject, html_content),
                    trigger='date',
                    run_date=schedule_dt,
                    id=job_id
                )
    
    conn.close()
    print(f"✅ Loaded {len(rows)} saved jobs")

# Start the scheduler
background_scheduler.start()
load_saved_jobs()
atexit.register(lambda: background_scheduler.shutdown())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"""
    ╔══════════════════════════════════════════════════════════╗
    ║     🚀 Auto Email Scheduler with Brevo API Started       ║
    ╠══════════════════════════════════════════════════════════╣
    ║  📧 Dashboard: http://localhost:{port}                   ║
    ║  💚 Health Check: http://localhost:{port}/health         ║
    ║  ⚡ Scheduler Status: {'Running' if background_scheduler.running else 'Stopped'}                     ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
