# app.py - Complete Email Scheduler
import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# This is the correct import after installing brevo-python
try:
    import sib_api_v3_sdk
    from sib_api_v3_sdk.rest import ApiException
    BREVO_AVAILABLE = True
except ImportError:
    print("⚠️ Warning: brevo-python not installed. Run: pip install brevo-python")
    BREVO_AVAILABLE = False

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

load_dotenv()

app = Flask(__name__)
CORS(app)

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

# Email Scheduler Class
class BrevoEmailScheduler:
    def __init__(self):
        if not BREVO_AVAILABLE:
            print("❌ Brevo SDK not available")
            return
            
        api_key = os.getenv('BREVO_API_KEY')
        if not api_key:
            print("⚠️ BREVO_API_KEY not set")
            return
            
        self.configuration = sib_api_v3_sdk.Configuration()
        self.configuration.api_key['api-key'] = api_key
        self.api_client = sib_api_v3_sdk.ApiClient(self.configuration)
        self.email_api = sib_api_v3_sdk.TransactionalEmailsApi(self.api_client)
    
    def send_email(self, to_email, to_name, subject, html_content, template_id=None):
        """Send email using Brevo"""
        if not BREVO_AVAILABLE:
            return False
            
        try:
            sender_email = os.getenv('BREVO_SENDER_EMAIL', 'sender@example.com')
            sender_name = os.getenv('BREVO_SENDER_NAME', 'Email Scheduler')
            
            send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
                to=[{'email': to_email, 'name': to_name or ''}],
                sender={'email': sender_email, 'name': sender_name},
                subject=subject,
                html_content=html_content
            )
            
            if template_id:
                send_smtp_email.template_id = template_id
            
            response = self.email_api.send_transac_email(send_smtp_email)
            print(f"✅ Email sent to {to_email}: {response.message_id}")
            return True
            
        except ApiException as e:
            print(f"❌ Failed: {e}")
            return False

email_scheduler = BrevoEmailScheduler()
background_scheduler = BackgroundScheduler()

# HTML Template (same as before, keep it)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Email Scheduler</title>
    <style>
        body { font-family: Arial; padding: 20px; background: #f0f0f0; }
        .container { max-width: 800px; margin: auto; background: white; padding: 20px; border-radius: 10px; }
        input, textarea, select { width: 100%; padding: 8px; margin: 5px 0 15px; }
        button { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; }
        .success { color: green; }
        .error { color: red; }
        .tab { display: inline-block; padding: 10px 20px; background: #ddd; cursor: pointer; }
        .tab.active { background: #007bff; color: white; }
        .tab-content { display: none; padding: 20px; }
        .tab-content.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📧 Email Scheduler with Brevo</h1>
        
        <div>
            <div class="tab active" onclick="showTab('send')">Send Now</div>
            <div class="tab" onclick="showTab('schedule')">Schedule</div>
            <div class="tab" onclick="showTab('recurring')">Recurring</div>
            <div class="tab" onclick="showTab('jobs')">Jobs</div>
        </div>
        
        <div id="send" class="tab-content active">
            <h2>Send Email Now</h2>
            <input type="email" id="send_email" placeholder="To Email"><br>
            <input type="text" id="send_name" placeholder="To Name"><br>
            <input type="text" id="send_subject" placeholder="Subject"><br>
            <textarea id="send_content" rows="5" placeholder="HTML Content"></textarea><br>
            <button onclick="sendNow()">Send Email</button>
            <div id="send_result"></div>
        </div>
        
        <div id="schedule" class="tab-content">
            <h2>Schedule Email</h2>
            <input type="email" id="schedule_email" placeholder="To Email"><br>
            <input type="text" id="schedule_name" placeholder="To Name"><br>
            <input type="text" id="schedule_subject" placeholder="Subject"><br>
            <textarea id="schedule_content" rows="5" placeholder="HTML Content"></textarea><br>
            <input type="datetime-local" id="schedule_time"><br>
            <button onclick="scheduleEmail()">Schedule</button>
            <div id="schedule_result"></div>
        </div>
        
        <div id="recurring" class="tab-content">
            <h2>Recurring Email</h2>
            <input type="email" id="rec_email" placeholder="To Email"><br>
            <input type="text" id="rec_name" placeholder="To Name"><br>
            <input type="text" id="rec_subject" placeholder="Subject"><br>
            <textarea id="rec_content" rows="5" placeholder="HTML Content"></textarea><br>
            <select id="rec_freq">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
            </select><br>
            <input type="number" id="rec_hour" placeholder="Hour (0-23)" value="9"><br>
            <input type="number" id="rec_minute" placeholder="Minute (0-59)" value="0"><br>
            <button onclick="scheduleRecurring()">Set Recurring</button>
            <div id="rec_result"></div>
        </div>
        
        <div id="jobs" class="tab-content">
            <h2>Scheduled Jobs</h2>
            <div id="jobs_list"></div>
            <button onclick="loadJobs()">Refresh</button>
        </div>
    </div>
    
    <script>
        function showTab(tabName) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById(tabName).classList.add('active');
            if (tabName === 'jobs') loadJobs();
        }
        
        async function sendNow() {
            const data = {
                to_email: document.getElementById('send_email').value,
                to_name: document.getElementById('send_name').value,
                subject: document.getElementById('send_subject').value,
                html_content: document.getElementById('send_content').value
            };
            const res = await fetch('/api/send-email', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            const result = await res.json();
            document.getElementById('send_result').innerHTML = 
                `<div class="${result.success ? 'success' : 'error'}">${result.message}</div>`;
        }
        
        async function scheduleEmail() {
            const data = {
                to_email: document.getElementById('schedule_email').value,
                to_name: document.getElementById('schedule_name').value,
                subject: document.getElementById('schedule_subject').value,
                html_content: document.getElementById('schedule_content').value,
                schedule_time: document.getElementById('schedule_time').value
            };
            const res = await fetch('/api/schedule-email', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            const result = await res.json();
            document.getElementById('schedule_result').innerHTML = 
                `<div class="${result.success ? 'success' : 'error'}">${result.message}</div>`;
        }
        
        async function scheduleRecurring() {
            const data = {
                to_email: document.getElementById('rec_email').value,
                to_name: document.getElementById('rec_name').value,
                subject: document.getElementById('rec_subject').value,
                html_content: document.getElementById('rec_content').value,
                frequency: document.getElementById('rec_freq').value,
                hour: parseInt(document.getElementById('rec_hour').value),
                minute: parseInt(document.getElementById('rec_minute').value)
            };
            const res = await fetch('/api/schedule-recurring', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            const result = await res.json();
            document.getElementById('rec_result').innerHTML = 
                `<div class="${result.success ? 'success' : 'error'}">${result.message}</div>`;
        }
        
        async function loadJobs() {
            const res = await fetch('/api/jobs');
            const data = await res.json();
            const jobsList = document.getElementById('jobs_list');
            if (data.jobs.length === 0) {
                jobsList.innerHTML = '<p>No scheduled jobs</p>';
                return;
            }
            jobsList.innerHTML = data.jobs.map(job => `
                <div style="border:1px solid #ddd; margin:10px 0; padding:10px">
                    <strong>${job.id}</strong><br>
                    Next: ${job.next_run_time || 'N/A'}<br>
                    <button onclick="deleteJob('${job.id}')">Delete</button>
                </div>
            `).join('');
        }
        
        async function deleteJob(jobId) {
            await fetch(`/api/jobs/${jobId}`, {method: 'DELETE'});
            loadJobs();
        }
        
        // Set default datetime to tomorrow
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        tomorrow.setHours(9, 0, 0);
        document.getElementById('schedule_time').value = tomorrow.toISOString().slice(0, 16);
    </script>
</body>
</html>
"""

# Routes
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/send-email', methods=['POST'])
def send_email():
    data = request.json
    success = email_scheduler.send_email(
        data['to_email'],
        data.get('to_name', ''),
        data['subject'],
        data['html_content']
    )
    return jsonify({'success': success, 'message': 'Email sent' if success else 'Failed'})

@app.route('/api/schedule-email', methods=['POST'])
def schedule_email():
    data = request.json
    schedule_time = datetime.fromisoformat(data['schedule_time'])
    job_id = f"job_{datetime.now().timestamp()}"
    
    def send():
        email_scheduler.send_email(
            data['to_email'],
            data.get('to_name', ''),
            data['subject'],
            data['html_content']
        )
    
    background_scheduler.add_job(send, 'date', run_date=schedule_time, id=job_id)
    return jsonify({'success': True, 'message': f'Scheduled for {schedule_time}'})

@app.route('/api/schedule-recurring', methods=['POST'])
def schedule_recurring():
    data = request.json
    
    def send():
        email_scheduler.send_email(
            data['to_email'],
            data.get('to_name', ''),
            data['subject'],
            data['html_content']
        )
    
    if data['frequency'] == 'daily':
        trigger = CronTrigger(hour=data['hour'], minute=data['minute'])
        job_id = f"daily_{data['to_email']}"
    elif data['frequency'] == 'weekly':
        trigger = CronTrigger(day_of_week='mon', hour=data['hour'], minute=data['minute'])
        job_id = f"weekly_{data['to_email']}"
    else:
        trigger = CronTrigger(day=1, hour=data['hour'], minute=data['minute'])
        job_id = f"monthly_{data['to_email']}"
    
    background_scheduler.add_job(send, trigger, id=job_id, replace_existing=True)
    return jsonify({'success': True, 'message': f'{data["frequency"].capitalize()} email scheduled'})

@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    jobs = [{'id': j.id, 'next_run_time': str(j.next_run_time)} for j in background_scheduler.get_jobs()]
    return jsonify({'jobs': jobs})

@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    background_scheduler.remove_job(job_id)
    return jsonify({'success': True})

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'brevo_available': BREVO_AVAILABLE})

# Start scheduler
background_scheduler.start()
atexit.register(lambda: background_scheduler.shutdown())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"""
    ╔══════════════════════════════════════════╗
    ║   Email Scheduler Started                ║
    ║   http://localhost:{port}                 ║
    ║   Brevo SDK: {'✅ Available' if BREVO_AVAILABLE else '❌ Not installed'}  ║
    ╚══════════════════════════════════════════╝
    """)
    app.run(host='0.0.0.0', port=port, debug=True)
