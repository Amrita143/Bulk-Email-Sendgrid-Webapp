# Email Campaign Manager — Complete Guide

## Table of Contents

1. What This Project Does (Overview)
2. Understanding the Code Structure
3. How Background Tasks Work on a Cloud Server
4. SendGrid Rate Limits & Best Practices
5. Supabase Database Setup (Step-by-Step)
6. Railway Deployment (Step-by-Step)
7. Environment Variables & Secrets
8. How To Use the Application
9. PRD (Product Requirements Document)
10. Architecture Diagram
11. Troubleshooting & FAQ

---

## 1. What This Project Does (Overview)

Right now, you run a Jupyter notebook on your laptop to send emails. The problem: if you have 35,000 emails and each takes ~1 second, your laptop must stay open for ~10 hours.

This project moves that entire process to a **cloud server** (Railway). Here's the difference:

**Before (Laptop):**
Your Laptop → Runs Python loop → Sends emails one by one → Must stay open the whole time

**After (Cloud Server):**
Your Browser → Uploads template + CSV → Clicks "Launch" → Cloud server takes over → You can close your laptop → Emails keep sending → Check progress anytime from any device

The cloud server is a computer running 24/7 in a data center. Your code runs there, not on your laptop.

---

## 2. Understanding the Code Structure

```
email-campaign-manager/
├── main.py              ← The backend (Python FastAPI server)
├── static/
│   └── index.html       ← The frontend (web page you see in browser)
├── requirements.txt     ← Python packages needed
├── Procfile             ← Tells Railway how to start the app
├── railway.json         ← Railway configuration
├── nixpacks.toml        ← Tells Railway which Python version to use
└── .gitignore           ← Files Git should ignore
```

### main.py (Backend) — What each part does:

**Imports & Setup:** Loads all required libraries. Sets up FastAPI (the web framework), SendGrid (email service), Supabase (database), and Jinja2 (template engine with your `%%field%%` syntax).

**`extract_template_fields()`:** Scans your HTML template to find all `%%FieldName%%` placeholders. Returns a list like `["First_Name", "Last_Name", ...]`.

**`run_campaign()` — THE KEY FUNCTION:** This is the background task that sends all emails. When you click "Launch Campaign," the server starts this function using `asyncio.create_task()`. The important thing: this function runs on the server, not in your browser. Even if you close the browser tab, the server keeps executing this function.

It works like your notebook loop, but with these additions:
- **Rate limiting:** Waits between each email to stay under SendGrid's 600/min limit
- **Batch pauses:** Takes breaks every N emails to avoid overwhelming SendGrid
- **Retry logic:** If an email fails (network error, etc.), retries up to 3 times
- **Progress tracking:** Updates a dictionary in memory every email, which the frontend reads
- **Database logging:** Saves every email result to Supabase for permanent records

**API Endpoints:** These are URLs the frontend calls:
- `POST /api/upload-template` — Upload HTML template, get back field names
- `POST /api/validate-csv` — Upload CSV, check if columns match template fields
- `POST /api/send-test` — Send one test email
- `POST /api/start-campaign` — Start the background campaign
- `GET /api/campaign/{id}/stream` — Real-time progress (SSE)
- `POST /api/campaign/{id}/cancel` — Stop a running campaign
- `GET /api/campaigns` — List past campaigns from database

### static/index.html (Frontend) — The web page:

A single HTML file with CSS and JavaScript. No React or build tools needed. It has 5 steps:
1. Upload template → shows detected fields
2. (Optional) Send test email
3. Upload CSV → validates columns match template
4. Configure rate, batch size, etc. → shows ETA
5. Live progress dashboard with real-time updates

The live updates use **Server-Sent Events (SSE)**: the browser opens a long-lived connection to the server, and the server pushes updates every 2 seconds. If the connection drops (e.g., laptop goes to sleep), the browser auto-reconnects and catches up.

---

## 3. How Background Tasks Work on a Cloud Server

This is the core concept to understand. Here's a simple analogy:

**Your laptop** is like a kitchen in your house. If you start cooking and leave the house, the stove turns off. That's your Jupyter notebook — it runs only while your laptop is open.

**A cloud server (Railway)** is like a restaurant kitchen that's always open. You can place an order (start a campaign), leave the restaurant, and come back later to pick up your food. The kitchen keeps working.

### Technical explanation:

When you deploy to Railway, your `main.py` starts running on Railway's server. It stays running 24/7.

When you hit "Launch Campaign," the frontend sends an HTTP request to `POST /api/start-campaign`. The server:
1. Creates a unique campaign ID
2. Calls `asyncio.create_task(run_campaign(campaign_id, config))` — this starts the email loop in the background
3. Immediately returns the campaign ID to your browser

`asyncio.create_task()` is the magic. It tells Python: "start running this function, but don't wait for it to finish. Return control right away." The function continues executing in the event loop on the server.

Your browser can now close. The server's event loop keeps running `run_campaign()` until all emails are sent.

---

## 4. SendGrid Rate Limits & Best Practices

Based on research, here are the key SendGrid limits:

**API Rate Limit: 600 requests per minute** (for the Mail Send endpoint). If you exceed this, you get HTTP 429 (Too Many Requests) with a `Retry-After` header.

**Recommended safe rate: 60 emails/minute** for normal accounts. This gives a 10x safety margin and avoids triggering spam filters or reputation damage.

**Why not send at maximum speed?**
- ISPs (Gmail, Yahoo, etc.) monitor incoming volume. Sudden spikes trigger spam filtering.
- Your sender reputation can be damaged if too many emails bounce or get marked as spam.
- SendGrid may throttle or suspend accounts that send too aggressively.

**The configuration settings in this app:**

| Setting | Default | What it does |
|---|---|---|
| `rate_per_minute` | 60 | Emails sent per minute. Max 500 (app-enforced). |
| `batch_size` | 500 | After every 500 emails, take a pause. |
| `batch_pause_seconds` | 10 | How long to pause between batches (seconds). |
| `retry_count` | 3 | How many times to retry a failed email. |

**For 35,000 emails at 60/min:**
- Time: ~583 minutes = ~9.7 hours
- But now it runs on the cloud, so you don't need to keep your laptop open!

**For 35,000 emails at 200/min (faster):**
- Time: ~175 minutes = ~2.9 hours
- Only do this if your SendGrid account has a good reputation and dedicated IP

**For 35,000 emails at 500/min (maximum safe):**
- Time: ~70 minutes = ~1.2 hours
- Only for established, high-reputation accounts with dedicated IPs

---

## 5. Supabase Database Setup (Step-by-Step)

Supabase is a cloud database (like a spreadsheet in the cloud that your app can read/write to). It stores campaign history and email logs permanently.

### Step 1: Create a Supabase account
Go to https://supabase.com and sign up (free tier is fine).

### Step 2: Create a new project
Click "New Project." Give it a name like "email-campaigns." Choose a region close to your Railway server (e.g., US East). Set a database password (save it somewhere).

### Step 3: Create the database tables
Go to the **SQL Editor** in the Supabase dashboard. Paste this SQL and run it:

```sql
-- Table to store campaign metadata (one row per campaign)
CREATE TABLE campaigns (
    id TEXT PRIMARY KEY,                    -- Unique campaign ID (e.g., "a1b2c3d4e5f6")
    status TEXT DEFAULT 'pending',          -- running, completed, cancelled
    total_emails INTEGER DEFAULT 0,         -- How many emails in the CSV
    sent_count INTEGER DEFAULT 0,           -- How many successfully sent
    failed_count INTEGER DEFAULT 0,         -- How many failed
    from_email TEXT,                         -- Sender email address
    subject_pattern TEXT,                    -- Subject line template
    rate_per_minute INTEGER DEFAULT 60,     -- Configured send rate
    started_at TIMESTAMPTZ,                 -- When campaign started
    finished_at TIMESTAMPTZ,                -- When campaign ended
    duration_seconds FLOAT,                 -- Total time in seconds
    actual_rate FLOAT,                      -- Actual emails/min achieved
    success_percentage FLOAT,               -- % of emails that succeeded
    config_json TEXT,                        -- Full configuration as JSON
    created_at TIMESTAMPTZ DEFAULT NOW()    -- Row creation time
);

-- Table to store individual email send results (one row per email attempt)
CREATE TABLE email_logs (
    id BIGSERIAL PRIMARY KEY,               -- Auto-incrementing ID
    campaign_id TEXT REFERENCES campaigns(id), -- Which campaign this belongs to
    email_index INTEGER,                     -- Row number from CSV (0-based)
    to_email TEXT,                           -- Recipient email address
    status_code INTEGER,                     -- HTTP status from SendGrid (202 = success)
    success BOOLEAN DEFAULT FALSE,           -- true if delivered
    error_message TEXT,                      -- Error details if failed
    sent_at TIMESTAMPTZ DEFAULT NOW()       -- When this email was attempted
);

-- Index for fast lookups by campaign
CREATE INDEX idx_email_logs_campaign ON email_logs(campaign_id);

-- Enable Row Level Security (optional but recommended)
ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_logs ENABLE ROW LEVEL SECURITY;

-- Allow the service role to do everything (used by our backend)
CREATE POLICY "Service role full access campaigns" ON campaigns
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Service role full access email_logs" ON email_logs
    FOR ALL USING (true) WITH CHECK (true);
```

### Step 4: Get your Supabase credentials
Go to **Settings → API** in Supabase dashboard. Copy:
- **Project URL** (looks like `https://abcdefgh.supabase.co`)
- **Service Role Key** (the longer one — use this, NOT the anon key, since we're doing server-side operations)

---

## 6. Railway Deployment (Step-by-Step)

Railway is a cloud platform that runs your code 24/7. Think of it as renting a small computer in a data center.

### Step 1: Create a Railway account
Go to https://railway.app and sign up. You get $5 free credit (enough for testing).

### Step 2: Install Railway CLI (optional, but easiest)
```bash
# On Windows (PowerShell):
iwr -useb https://railway.app/install.ps1 | iex

# On Mac:
brew install railway

# On Linux:
curl -fsSL https://railway.app/install.sh | sh
```

### Step 3: Push your code to GitHub
```bash
# In your project folder:
cd email-campaign-manager
git init
git add .
git commit -m "Initial commit - email campaign manager"

# Create a repo on GitHub, then:
git remote add origin https://github.com/YOUR_USERNAME/email-campaign-manager.git
git push -u origin main
```

### Step 4: Deploy on Railway
**Option A: Via GitHub (recommended)**
1. Go to https://railway.app/new
2. Click "Deploy from GitHub repo"
3. Select your `email-campaign-manager` repository
4. Railway auto-detects it's a Python app and starts building

**Option B: Via CLI**
```bash
railway login
railway init
railway up
```

### Step 5: Set Environment Variables (SECRETS)
In the Railway dashboard, go to your project → **Variables** tab. Add these:

| Variable | Value | Where to get it |
|---|---|---|
| `SENDGRID_API_KEY` | `SG.xxxx...` | Your SendGrid API key |
| `SUPABASE_URL` | `https://abc.supabase.co` | Supabase → Settings → API |
| `SUPABASE_KEY` | `eyJhbGci...` | Supabase → Settings → API → service_role key |

**IMPORTANT:** Never put API keys in your code. Always use environment variables. Railway encrypts these securely.

### Step 6: Get your app URL
After deploying, Railway gives you a URL like `https://email-campaign-manager-production.up.railway.app`. This is your app! Open it in a browser to use the campaign manager.

### Step 7: Custom domain (optional)
In Railway → Settings → Networking, you can add a custom domain like `campaigns.yourdomain.com`.

---

## 7. Environment Variables & Secrets

These are like passwords for your app. They are set in the Railway dashboard, not in your code:

| Variable | Purpose | Required? |
|---|---|---|
| `SENDGRID_API_KEY` | Authenticates with SendGrid to send emails | Yes |
| `SUPABASE_URL` | Your Supabase database URL | Yes (for persistence) |
| `SUPABASE_KEY` | Authenticates with Supabase | Yes (for persistence) |
| `PORT` | Port number (Railway sets this automatically) | No (auto-set) |

The app works without Supabase too (campaigns run fine), but you won't have persistent history if the server restarts.

---

## 8. How To Use the Application

### First-time setup:
1. Deploy to Railway (follow Section 6)
2. Set environment variables (Section 7)
3. Open the Railway URL in your browser

### Running a campaign:

**Step 1 — Upload Template:**
Click the upload area and select your HTML email template (like `template_4_2_2026.html`). The app detects all `%%field%%` placeholders and shows them as blue tags.

**Step 2 — Test Email (Optional):**
Enter a test recipient email (your own). The app fills in default values for all fields. Click "Send Test Email" to verify the template renders correctly in an inbox. Or click "Skip" to proceed.

**Step 3 — Upload CSV:**
Upload your campaign CSV file. The app validates that:
- All template fields exist as CSV columns
- There's an `EMAIL_ID` column
- Shows a preview of the first 5 rows

If columns are missing, you'll see which ones in red.

**Step 4 — Configure & Launch:**
- **Emails per minute:** Start with 60 (safe default). Increase only if you're confident.
- **Batch size:** Take a pause every N emails (500 is good).
- **Batch pause:** How long to pause between batches (10 seconds).
- **Retry count:** Try failed emails again (3 times is reasonable).

The app shows an estimated completion time. Click "Launch Campaign."

**Step 5 — Monitor Progress:**
A live dashboard shows:
- Progress bar (green = sent, red = failed)
- Total delivered / failed counts
- Current sending rate (emails/minute)
- ETA remaining
- Recent errors

**You can now close your laptop.** The campaign continues on the server. Come back later and refresh — the progress dashboard reconnects automatically.

When done, you see a green summary with final statistics.

### Viewing past campaigns:
Scroll to "Campaign History" and click "Refresh" to see all past campaigns stored in the database, with their stats.

---

## 9. PRD (Product Requirements Document)

### Product: Email Campaign Manager for Cloud Deployment

### Problem Statement
Bulk email campaigns of 5,000-35,000+ emails currently require keeping a laptop open for hours while a Jupyter notebook sequentially sends emails via SendGrid API. This is unreliable (laptop can sleep, crash, or lose internet) and blocks the user's machine.

### Solution
A web application deployed on Railway that:
1. Accepts HTML email templates with `%%dynamic_field%%` placeholders
2. Validates CSV recipient files against template fields
3. Sends test emails before campaign launch
4. Runs campaigns as background server tasks with configurable rate limiting
5. Provides real-time progress monitoring via SSE
6. Logs all results to Supabase for permanent records
7. Supports campaign cancellation

### Functional Requirements
- FR1: Upload and parse HTML email templates, extracting dynamic field names
- FR2: Upload and validate CSV files, ensuring all template fields have matching columns
- FR3: Send test emails with user-provided or CSV-derived sample data
- FR4: Launch background campaign tasks that survive browser disconnection
- FR5: Real-time progress streaming (sent/failed/rate/ETA)
- FR6: Campaign cancellation with graceful stop
- FR7: Configurable rate limiting (1-500 emails/min)
- FR8: Batch pausing to avoid overwhelming SendGrid
- FR9: Automatic retry for failed emails (configurable 1-10 retries)
- FR10: Persistent campaign history and email-level logs in Supabase
- FR11: Error tracking with last 100 errors visible in real-time

### Non-Functional Requirements
- NFR1: Handle campaigns of 50,000+ emails without memory issues
- NFR2: Graceful handling of SendGrid rate limits (429 responses)
- NFR3: Auto-retry on network errors with exponential backoff
- NFR4: Database writes should not block email sending
- NFR5: Frontend must reconnect automatically after connection loss

### Configuration Defaults
| Parameter | Default | Range | Rationale |
|---|---|---|---|
| Rate per minute | 60 | 1-500 | Safe for most SendGrid accounts |
| Batch size | 500 | 0 (disable)-any | Prevents sustained high-rate sending |
| Batch pause | 10s | 0-300s | Gives SendGrid queues time to process |
| Retry count | 3 | 1-10 | Handles transient network errors |
| Unsubscribe group | 25279 | Any valid ID | Your existing SendGrid group |

### Out of Scope (for this version)
- Email open/click tracking (use SendGrid dashboard for this)
- Multi-user authentication (add if needed later)
- Email scheduling (send at specific time)
- A/B testing of templates

---

## 10. Architecture Diagram

```
┌──────────────┐        HTTPS         ┌──────────────────────┐
│   Browser    │ ◄──────────────────► │   Railway Server     │
│  (Your PC)  │   Upload files,       │   (Always Running)   │
│              │   Launch campaign,    │                      │
│  index.html  │   SSE progress       │   main.py (FastAPI)  │
│  (Frontend)  │   stream             │   ┌────────────────┐ │
└──────────────┘                      │   │ run_campaign()  │ │
      │                               │   │ (Background     │ │
      │  You can close                │   │  asyncio task)  │ │
      │  your laptop here             │   └───────┬────────┘ │
      │                               │           │          │
      │                               └───────────┼──────────┘
      │                                           │
      │                               ┌───────────▼──────────┐
      │                               │   SendGrid API       │
      │                               │   (Sends actual      │
      │                               │    emails)           │
      │                               └──────────────────────┘
      │
      │   Check progress later        ┌──────────────────────┐
      └──────────────────────────────►│   Supabase DB        │
                                      │   (Stores campaign   │
                                      │    history & logs)   │
                                      └──────────────────────┘
```

---

## 11. Troubleshooting & FAQ

**Q: What if Railway restarts my server during a campaign?**
A: The in-memory campaign state is lost, but all email logs up to that point are saved in Supabase. You'd need to re-upload the CSV (minus already-sent rows) and launch a new campaign. Railway rarely restarts unless you deploy new code.

**Q: What if I get 429 (rate limited) errors?**
A: The app automatically detects 429 responses and waits 60 seconds before retrying. Reduce `rate_per_minute` if this happens frequently.

**Q: How do I know which emails were actually delivered vs. bounced?**
A: The app tracks SendGrid's 202 (accepted) status. "Accepted" means SendGrid received it — actual delivery depends on the recipient's email server. Check SendGrid's Activity Feed for delivery/bounce details.

**Q: Can I run multiple campaigns at once?**
A: Technically yes, but it's not recommended. Multiple campaigns share the same SendGrid rate limit. Run them sequentially.

**Q: How much does Railway cost?**
A: Railway gives $5/month free credit. A small server (~$5/month) is enough for this app since it's mostly waiting between emails. For a 10-hour campaign, it costs pennies.

**Q: The app works without Supabase?**
A: Yes. Campaigns run fine, and you see real-time progress. But if the server restarts, you lose history. Supabase adds permanent storage.

**Q: My template uses `{{field}}` instead of `%%field%%`. How to change?**
A: In `main.py`, find the `jinja_env = Environment(...)` line and change `variable_start_string` and `variable_end_string` to `'{{'` and `'}}'`.

**Q: How to add a new sender email?**
A: You must verify the sender domain in SendGrid first. Go to SendGrid → Settings → Sender Authentication → Domain Authentication. Add your domain's DNS records. Then you can use any email@yourdomain in the "From Email" field.

**Q: The CSV has trailing spaces in column names. Will it work?**
A: Yes. The app strips whitespace from column names automatically (`df.columns = df.columns.str.strip()`), just like your original notebook.
