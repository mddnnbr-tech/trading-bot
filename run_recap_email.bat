@echo off
REM Daily trading bot recap emailer
REM Schedule this in Windows Task Scheduler to run at 4:30 PM ET on weekdays.

cd /d "C:\Users\ashle\OneDrive\Desktop\trading-bot"
python send_recap_email.py >> logs\email_sender.log 2>&1
