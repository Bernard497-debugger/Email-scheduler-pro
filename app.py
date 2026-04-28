import os
import requests
from datetime import datetime
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# =========================
# CONFIG (ENV VARIABLES)
# =========================
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL")

# =========================
# EMAIL FUNCTION
# =========================
def send_email(to_email, subject, html_content):
    url = "https://api.brevo.com/v3/smtp/email"

    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }

    data = {
        "sender": {"email": FROM_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content
    }

    try:
        response = requests.post(url, json=data, headers=headers)
        print("Email status:", response.status_code, response.text)
    except Exception as e:
        print("Error sending email:", e)


# =========================
# SCHEDULED JOB
# =========================
def scheduled_job():
    print("Running job at:", datetime.now())

    send_email(
        to_email="receiver@example.com",  # change this
        subject="🚀 Scheduled Email",
        html_content="""
        <h2>Hello 👋</h2>
        <p>This email was sent automatically using Flask + APScheduler + Brevo.</p>
        """
    )


# =========================
# SCHEDULER SETUP
# =========================
scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_job, 'interval', minutes=1)  # change timing here
scheduler.start()


# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "✅ Email Scheduler Running"


# =========================
# RUN APP
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
