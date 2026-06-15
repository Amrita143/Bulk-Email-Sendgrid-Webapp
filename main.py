"""
Email Campaign Manager - Backend API
=====================================
A FastAPI application that runs bulk email campaigns as background tasks
on a cloud server using SendGrid API. Campaigns continue running even
if the user closes their browser.

Key concepts:
- FastAPI: A modern Python web framework for building APIs
- Background Tasks: Long-running email sending loops that run independently
- SSE (Server-Sent Events): Real-time progress updates pushed to the browser
- Supabase: Cloud database to store campaign data permanently (via REST API)
"""

import os
import re
import io
import csv
import json
import time
import uuid
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from jinja2 import Environment
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Asm
import httpx
import pandas as pd

# ============================================================
# CONFIGURATION
# ============================================================

# These are read from environment variables (set in Railway dashboard)
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # Use the "anon" or "service_role" key
EMAIL_QUEUE_ENABLED = os.environ.get("EMAIL_QUEUE_ENABLED", "false").lower() == "true"
QUEUE_WORKER_ID = os.environ.get("QUEUE_WORKER_ID", f"worker-{uuid.uuid4().hex[:8]}")
QUEUE_BATCH_SIZE = int(os.environ.get("QUEUE_BATCH_SIZE", "10"))

# Jinja2 template engine - supports BOTH %%field%% and {{field}} syntax
# The regex in extract_template_fields auto-detects which one your template uses.
# We create the right Jinja env based on the uploaded template.
# Default uses %% %% to match your existing templates.

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# FASTAPI APP SETUP
# ============================================================

app = FastAPI(title="Email Campaign Manager")

# Allow requests from any origin (needed for the frontend to talk to the backend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the frontend HTML file
app.mount("/static", StaticFiles(directory="static"), name="static")

# ============================================================
# IN-MEMORY CAMPAIGN TRACKING
# ============================================================
# While the campaign runs, we track progress in memory for real-time updates.
# This dict maps campaign_id -> progress data.
# The final results are also saved to Supabase for permanent storage.

active_campaigns = {}


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def detect_template_syntax(html_content: str) -> str:
    """
    Auto-detect whether the template uses %%field%% or {{field}} syntax.
    Returns 'percent' or 'curly'.
    """
    percent_matches = re.findall(r'%%\w+%%', html_content)
    curly_matches = re.findall(r'\{\{\s*\w+\s*\}\}', html_content)
    
    if len(percent_matches) >= len(curly_matches):
        return 'percent'
    return 'curly'


def get_jinja_env(syntax: str = 'percent') -> Environment:
    """
    Create a Jinja2 Environment matching the template syntax.
    'percent' -> %%field%%  (your original templates)
    'curly'   -> {{field}}  (standard Jinja2)
    """
    if syntax == 'curly':
        return Environment()  # Default Jinja2 uses {{ }}
    else:
        return Environment(
            variable_start_string='%%',
            variable_end_string='%%',
        )


def extract_template_fields(html_content: str) -> tuple:
    """
    Extract all placeholder fields from an HTML template.
    Auto-detects %%field%% or {{field}} syntax.
    
    Returns (list_of_field_names, syntax_type)
    
    Example: "Hello %%First_Name%%" -> (["First_Name"], "percent")
    Example: "Hello {{First_Name}}" -> (["First_Name"], "curly")
    """
    syntax = detect_template_syntax(html_content)
    
    if syntax == 'curly':
        fields = re.findall(r'\{\{\s*(\w+)\s*\}\}', html_content)
    else:
        fields = re.findall(r'%%(\w+)%%', html_content)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_fields = []
    for f in fields:
        if f not in seen:
            seen.add(f)
            unique_fields.append(f)
    return unique_fields, syntax


def generate_subject_line(row: dict, subject_pattern: str, jinja_env: Environment) -> str:
    """
    Generate the email subject line by replacing placeholders.
    Works with both %%field%% and {{field}} syntax.
    """
    template = jinja_env.from_string(subject_pattern)
    rendered = template.render(**row)
    # Clean up extra spaces from empty middle names
    return ' '.join(rendered.split())


def clean_csv_row(row: dict) -> dict:
    """Normalize CSV row values before templating and sending."""
    clean_row = {}
    for key, value in row.items():
        key = key.strip()
        if pd.isna(value) if not isinstance(value, str) else False:
            clean_row[key] = ""
        else:
            clean_row[key] = str(value)
    return clean_row


def validate_email_address(email: str) -> tuple[bool, str]:
    """
    Validate a recipient address before SendGrid's helper classes see it.

    This intentionally rejects common CSV typos like "gmail,com" so one bad
    record becomes a row failure instead of crashing the campaign task.
    """
    email = (email or "").strip()
    if not email:
        return False, "No EMAIL_ID found in row"

    _, parsed = parseaddr(email)
    if parsed != email:
        return False, "Invalid email format"

    if "," in email or ".." in email:
        return False, "Invalid email format"

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return False, "Invalid email format"

    return True, ""


def append_progress_error(progress: dict, error_entry: dict):
    """Track recent errors in memory without letting the list grow forever."""
    progress["errors"].append(error_entry)
    if len(progress["errors"]) > 100:
        progress["errors"] = progress["errors"][-100:]


def normalize_campaign_record(record: dict) -> dict:
    """Return DB campaign records in the same shape as live progress."""
    if "total" in record:
        return record

    total = record.get("total_emails") or 0
    sent = record.get("sent_count") or 0
    failed = record.get("failed_count") or 0
    normalized = dict(record)
    normalized.update({
        "campaign_id": record.get("id") or record.get("campaign_id"),
        "total": total,
        "sent": sent,
        "failed": failed,
        "current_index": min(sent + failed, total),
        "errors": record.get("errors") or [],
        "estimated_remaining": "calculating..." if record.get("status") in {"queued", "running"} else "0h 0m 0s",
    })
    return normalized


async def record_email_result(
    campaign_id: str,
    email_index: int,
    to_email: str,
    success: bool,
    status_code: int = 0,
    error_message: Optional[str] = None,
):
    """Persist an email result using the current email_logs schema."""
    await save_email_log({
        "campaign_id": campaign_id,
        "email_index": email_index,
        "to_email": to_email,
        "status_code": status_code if success else 0,
        "success": success,
        "error_message": error_message if not success else None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    })


async def record_row_failure(
    campaign_id: str,
    progress: dict,
    email_index: int,
    to_email: str,
    error_message: str,
    status_code: int = 0,
):
    """Mark one row failed and continue the campaign."""
    progress["failed"] += 1
    error_entry = {
        "index": email_index,
        "email": to_email or "EMPTY",
        "error": error_message,
        "status_code": status_code,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    append_progress_error(progress, error_entry)
    asyncio.create_task(record_email_result(
        campaign_id=campaign_id,
        email_index=email_index,
        to_email=to_email or "EMPTY",
        success=False,
        status_code=status_code,
        error_message=error_message,
    ))


# ============================================================
# SUPABASE REST API HELPERS (using httpx, no SDK needed)
# ============================================================
# Instead of the supabase Python SDK (which has version conflicts),
# we call the Supabase REST API directly. It's just HTTP POST/PATCH.

async def supabase_request(method: str, table: str, data: dict = None, params: dict = None) -> dict:
    """
    Make a direct REST API call to Supabase PostgREST.
    
    Supabase exposes a REST API at {SUPABASE_URL}/rest/v1/{table}
    We authenticate with the API key in the headers.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",  # Don't return data to save bandwidth
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if method == "upsert":
                headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
                resp = await client.post(url, json=data, headers=headers)
            elif method == "insert":
                resp = await client.post(url, json=data, headers=headers)
            elif method == "patch":
                resp = await client.patch(url, json=data, headers=headers, params=params)
            elif method == "select":
                headers.pop("Content-Type", None)
                headers["Prefer"] = "return=representation"
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 200:
                    return resp.json()
            
            if resp.status_code not in (200, 201, 204):
                logger.error(f"Supabase {method} {table} failed: {resp.status_code} {resp.text}")
                return None
            return {"ok": True}
    except Exception as e:
        logger.error(f"Supabase request error: {e}")
        return None


async def supabase_rpc(function_name: str, data: dict = None) -> Optional[dict]:
    """Call a Supabase Postgres function via REST RPC."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None

    url = f"{SUPABASE_URL}/rest/v1/rpc/{function_name}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=data or {}, headers=headers)
            if resp.status_code not in (200, 201, 204):
                logger.error(f"Supabase RPC {function_name} failed: {resp.status_code} {resp.text}")
                return None
            if resp.status_code == 204 or not resp.text:
                return {"ok": True}
            return resp.json()
    except Exception as e:
        logger.error(f"Supabase RPC error: {e}")
        return None


async def save_campaign_to_db(campaign_data: dict):
    """Save campaign metadata to Supabase via REST API (upsert)."""
    await supabase_request("upsert", "campaigns", campaign_data)


async def save_email_log(log_entry: dict):
    """Save individual email send result to Supabase via REST API."""
    await supabase_request("insert", "email_logs", log_entry)


def queue_mode_available() -> bool:
    """Queue mode is opt-in and requires Supabase."""
    return EMAIL_QUEUE_ENABLED and bool(SUPABASE_URL and SUPABASE_KEY)


def build_campaign_config(config: dict, include_template: bool = False) -> dict:
    """Serialize campaign settings for persistence."""
    persisted_config = {
        "batch_size": config.get("batch_size", 500),
        "batch_pause": config.get("batch_pause_seconds", 10),
        "retry_count": config.get("retry_count", 3),
        "unsubscribe_group_id": config.get("unsubscribe_group_id", 25279),
        "template_syntax": config.get("template_syntax", "percent"),
        "queue_enabled": include_template,
    }
    if include_template:
        persisted_config.update({
            "html_template": config["html_template"],
            "from_email": config["from_email"],
            "subject_pattern": config["subject_pattern"],
            "rate_per_minute": config.get("rate_per_minute", 60),
        })
    return persisted_config


async def enqueue_campaign_jobs(campaign_id: str, rows: list[dict], max_attempts: int) -> bool:
    """Insert one durable queue row per CSV record."""
    jobs = []
    now = datetime.now(timezone.utc).isoformat()
    for i, row in enumerate(rows):
        clean_row = clean_csv_row(row)
        jobs.append({
            "campaign_id": campaign_id,
            "email_index": i,
            "to_email": clean_row.get("EMAIL_ID", "").strip(),
            "row_data": clean_row,
            "status": "queued",
            "attempt_count": 0,
            "max_attempts": max_attempts,
            "next_attempt_at": now,
        })

    batch_size = 500
    for start in range(0, len(jobs), batch_size):
        result = await supabase_request("insert", "email_jobs", jobs[start:start + batch_size])
        if not result:
            return False
    return True


async def claim_due_email_jobs(limit: int = QUEUE_BATCH_SIZE) -> list[dict]:
    """
    Claim due jobs for this worker.

    The Supabase migration will create this RPC using FOR UPDATE SKIP LOCKED so
    multiple Railway workers can process safely without double-sending.
    """
    result = await supabase_rpc("claim_due_email_jobs", {
        "p_worker_id": QUEUE_WORKER_ID,
        "p_limit": limit,
        "p_lock_seconds": 300,
    })
    return result if isinstance(result, list) else []


async def mark_email_job_sent(job_id: str, status_code: int):
    await supabase_request("patch", "email_jobs", {
        "status": "sent",
        "last_status_code": status_code,
        "last_error_type": None,
        "last_error_message": None,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "locked_by": None,
        "locked_until": None,
    }, params={"id": f"eq.{job_id}"})


def retry_delay_seconds(attempt_count: int, status_code: int = 0) -> int:
    if status_code == 429:
        return 90
    delays = [60, 300, 900]
    return delays[min(max(attempt_count - 1, 0), len(delays) - 1)]


def is_permanent_email_failure(error_type: str, status_code: int = 0) -> bool:
    if error_type in {"invalid_email", "template_error", "message_build_error"}:
        return True
    return status_code in {400, 401, 403}


async def schedule_email_job_retry(job: dict, error_type: str, error_message: str, status_code: int = 0):
    attempt_count = int(job.get("attempt_count") or 0) + 1
    max_attempts = int(job.get("max_attempts") or 3)

    if attempt_count >= max_attempts or is_permanent_email_failure(error_type, status_code):
        await move_email_job_to_dead_letter(job, error_type, error_message, status_code, attempt_count)
        return

    next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds(attempt_count, status_code))
    await supabase_request("patch", "email_jobs", {
        "status": "retry_scheduled",
        "attempt_count": attempt_count,
        "next_attempt_at": next_attempt_at.isoformat(),
        "last_status_code": status_code,
        "last_error_type": error_type,
        "last_error_message": error_message[:2000],
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        "locked_by": None,
        "locked_until": None,
    }, params={"id": f"eq.{job['id']}"})


async def move_email_job_to_dead_letter(
    job: dict,
    error_type: str,
    error_message: str,
    status_code: int = 0,
    attempt_count: Optional[int] = None,
):
    attempt_count = attempt_count if attempt_count is not None else int(job.get("attempt_count") or 0) + 1
    await supabase_request("insert", "email_dead_letters", {
        "campaign_id": job["campaign_id"],
        "email_job_id": job["id"],
        "email_index": job["email_index"],
        "to_email": job.get("to_email") or "",
        "row_data": job.get("row_data") or {},
        "final_status_code": status_code,
        "error_type": error_type,
        "error_message": error_message[:2000],
        "attempt_count": attempt_count,
    })
    await supabase_request("patch", "email_jobs", {
        "status": "dead_lettered",
        "attempt_count": attempt_count,
        "last_status_code": status_code,
        "last_error_type": error_type,
        "last_error_message": error_message[:2000],
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        "locked_by": None,
        "locked_until": None,
    }, params={"id": f"eq.{job['id']}"})


async def load_campaign_config(campaign_id: str) -> Optional[dict]:
    result = await supabase_request("select", "campaigns", params={
        "id": f"eq.{campaign_id}",
        "limit": "1",
    })
    if not result or not isinstance(result, list):
        return None

    raw_config = result[0].get("config_json") or "{}"
    try:
        return json.loads(raw_config)
    except json.JSONDecodeError:
        logger.error(f"Campaign {campaign_id} has invalid config_json")
        return None


async def refresh_campaign_counts(campaign_id: str):
    """Let the DB aggregate queue state into the campaigns table."""
    await supabase_rpc("refresh_campaign_counts", {"p_campaign_id": campaign_id})


async def process_email_job(job: dict, sg: SendGridAPIClient) -> None:
    campaign_id = job["campaign_id"]
    email_index = int(job["email_index"])
    to_email = (job.get("to_email") or "").strip()
    row_data = job.get("row_data") or {}
    attempt_number = int(job.get("attempt_count") or 0) + 1

    is_valid, validation_error = validate_email_address(to_email)
    if not is_valid:
        await record_email_result(campaign_id, email_index, to_email or "EMPTY", False, 0, validation_error)
        await move_email_job_to_dead_letter(job, "invalid_email", validation_error, 0, attempt_number)
        await refresh_campaign_counts(campaign_id)
        return

    config = await load_campaign_config(campaign_id)
    if not config:
        await schedule_email_job_retry(job, "campaign_config_error", "Campaign config not found", 0)
        await refresh_campaign_counts(campaign_id)
        return

    try:
        jinja_env = get_jinja_env(config.get("template_syntax", "percent"))
        template = jinja_env.from_string(config["html_template"])
        html_content = template.render(**row_data)
        subject = generate_subject_line(row_data, config["subject_pattern"], jinja_env)
    except Exception as e:
        error_message = f"Template render error: {str(e)}"
        await record_email_result(campaign_id, email_index, to_email, False, 0, error_message)
        await move_email_job_to_dead_letter(job, "template_error", error_message, 0, attempt_number)
        await refresh_campaign_counts(campaign_id)
        return

    try:
        message = Mail(
            from_email=config["from_email"],
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
        )

        unsubscribe_group_id = config.get("unsubscribe_group_id")
        if unsubscribe_group_id:
            message.asm = Asm(group_id=int(unsubscribe_group_id))
    except Exception as e:
        error_message = f"Message build error: {str(e)}"
        await record_email_result(campaign_id, email_index, to_email, False, 0, error_message)
        await move_email_job_to_dead_letter(job, "message_build_error", error_message, 0, attempt_number)
        await refresh_campaign_counts(campaign_id)
        return

    status_code = 0
    try:
        response = sg.send(message)
        status_code = response.status_code
        if status_code == 202:
            await mark_email_job_sent(job["id"], status_code)
            await record_email_result(campaign_id, email_index, to_email, True, status_code)
        else:
            error_message = f"SendGrid returned status {status_code}"
            await record_email_result(campaign_id, email_index, to_email, False, status_code, error_message)
            await schedule_email_job_retry(job, "sendgrid_status", error_message, status_code)
    except Exception as e:
        error_message = str(e)
        await record_email_result(campaign_id, email_index, to_email, False, status_code, error_message)
        await schedule_email_job_retry(job, "sendgrid_exception", error_message, status_code)

    await refresh_campaign_counts(campaign_id)


async def run_queue_worker(poll_interval: int = 5):
    """Run a durable Supabase-backed email worker."""
    if not SENDGRID_API_KEY:
        raise RuntimeError("SendGrid API key not configured")
    if not queue_mode_available():
        raise RuntimeError("Queue worker requires EMAIL_QUEUE_ENABLED=true and Supabase credentials")

    logger.info(f"Starting queue worker {QUEUE_WORKER_ID}")
    sg = SendGridAPIClient(SENDGRID_API_KEY)

    while True:
        jobs = await claim_due_email_jobs(QUEUE_BATCH_SIZE)
        if not jobs:
            await asyncio.sleep(poll_interval)
            continue

        for job in jobs:
            try:
                await process_email_job(job, sg)
                config = await load_campaign_config(job["campaign_id"])
                rate_per_minute = int((config or {}).get("rate_per_minute") or 60)
                await asyncio.sleep(max(0.0, 60.0 / max(rate_per_minute, 1)))
            except Exception as e:
                logger.exception(f"Worker failed processing job {job.get('id')}: {e}")
                await schedule_email_job_retry(job, "worker_exception", str(e), 0)


# ============================================================
# BACKGROUND CAMPAIGN RUNNER
# ============================================================

async def run_campaign(campaign_id: str, config: dict):
    """
    THE CORE FUNCTION: Sends emails one by one with rate limiting.
    
    This runs as a background asyncio task on the server.
    Even if the user closes their browser, this keeps running
    because it's a server-side task, not a client-side one.
    
    Parameters:
    - campaign_id: Unique identifier for this campaign
    - config: Dictionary containing all campaign settings:
        - html_template: The email HTML with %%field%% placeholders
        - csv_data: List of dicts, one per row from the CSV
        - from_email: Sender email address
        - subject_pattern: Subject line with %%field%% placeholders
        - unsubscribe_group_id: SendGrid unsubscribe group ID
        - rate_per_minute: How many emails to send per minute (max 600)
        - batch_size: How many emails to send before taking a longer pause
        - batch_pause_seconds: How long to pause between batches
        - retry_count: How many times to retry failed emails
    """
    
    # Initialize the SendGrid client (the service that actually delivers emails)
    sg = SendGridAPIClient(SENDGRID_API_KEY)
    
    # Create the right Jinja env for this template's syntax
    template_syntax = config.get("template_syntax", "percent")
    jinja_env = get_jinja_env(template_syntax)
    template = jinja_env.from_string(config["html_template"])
    
    total = len(config["csv_data"])
    rate_per_minute = config.get("rate_per_minute", 60)
    batch_size = config.get("batch_size", 500)
    batch_pause = config.get("batch_pause_seconds", 10)
    retry_count = config.get("retry_count", 3)
    unsubscribe_group_id = config.get("unsubscribe_group_id", 25279)
    
    # Calculate delay between each email to stay within rate limit
    # Example: 60 emails/min -> 1 second between each email
    delay_between_emails = 60.0 / rate_per_minute
    
    # Initialize progress tracking
    progress = {
        "campaign_id": campaign_id,
        "status": "running",
        "total": total,
        "sent": 0,               # Emails accepted by SendGrid (status 202)
        "failed": 0,             # Emails that failed after all retries
        "current_index": 0,      # Which row we're currently processing
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "rate_per_minute": rate_per_minute,
        "errors": [],            # List of error details (last 50)
        "avg_time_per_email": 0, # Rolling average time per email
        "estimated_remaining": "calculating...",
    }
    active_campaigns[campaign_id] = progress
    
    # Save initial campaign state to database
    await save_campaign_to_db({
        "id": campaign_id,
        "status": "running",
        "total_emails": total,
        "sent_count": 0,
        "failed_count": 0,
        "from_email": config["from_email"],
        "subject_pattern": config["subject_pattern"],
        "rate_per_minute": rate_per_minute,
        "started_at": progress["started_at"],
        "config_json": json.dumps(build_campaign_config(config, include_template=False)),
    })
    
    send_times = []  # Track time taken for each email (for ETA calculation)
    
    for i, row in enumerate(config["csv_data"]):
        # Check if campaign was cancelled by user
        if active_campaigns.get(campaign_id, {}).get("status") == "cancelled":
            progress["status"] = "cancelled"
            break
        
        progress["current_index"] = i
        email_start_time = time.time()
        
        consumer_email = "EMPTY"

        try:
            clean_row = clean_csv_row(row)
            consumer_email = clean_row.get("EMAIL_ID", "").strip()

            is_valid_email, validation_error = validate_email_address(consumer_email)
            if not is_valid_email:
                await record_row_failure(campaign_id, progress, i, consumer_email, validation_error)
            else:
                # Render the HTML template with this row's data
                try:
                    html_content = template.render(**clean_row)
                    subject = generate_subject_line(clean_row, config["subject_pattern"], jinja_env)
                except Exception as e:
                    await record_row_failure(
                        campaign_id,
                        progress,
                        i,
                        consumer_email,
                        f"Template render error: {str(e)}",
                    )
                    html_content = None
                    subject = None

                if html_content is not None and subject is not None:
                    try:
                        message = Mail(
                            from_email=config["from_email"],
                            to_emails=consumer_email,
                            subject=subject,
                            html_content=html_content,
                        )

                        if unsubscribe_group_id:
                            asm = Asm(group_id=int(unsubscribe_group_id))
                            message.asm = asm
                    except Exception as e:
                        await record_row_failure(
                            campaign_id,
                            progress,
                            i,
                            consumer_email,
                            f"Message build error: {str(e)}",
                        )
                        message = None

                    if message is not None:
                        send_success = False
                        last_error = ""
                        status_code = 0

                        for attempt in range(retry_count):
                            try:
                                response = sg.send(message)
                                status_code = response.status_code

                                if status_code == 202:
                                    # 202 = SendGrid accepted the email for delivery
                                    send_success = True
                                    break
                                elif status_code == 429:
                                    # 429 = Rate limit hit. Wait and retry.
                                    wait_time = 60
                                    logger.warning(f"Rate limit hit at index {i}. Waiting {wait_time}s...")
                                    progress["status"] = f"rate_limited (waiting {wait_time}s)"
                                    await asyncio.sleep(wait_time)
                                    progress["status"] = "running"
                                else:
                                    last_error = f"Status {status_code}"

                            except Exception as e:
                                last_error = str(e)
                                if attempt < retry_count - 1:
                                    await asyncio.sleep(5)

                        if send_success:
                            progress["sent"] += 1
                            asyncio.create_task(record_email_result(
                                campaign_id=campaign_id,
                                email_index=i,
                                to_email=consumer_email,
                                success=True,
                                status_code=status_code,
                            ))
                        else:
                            await record_row_failure(
                                campaign_id,
                                progress,
                                i,
                                consumer_email,
                                last_error or f"Status {status_code}",
                                status_code,
                            )
        except Exception as e:
            logger.exception(f"Unexpected row error in campaign {campaign_id} at index {i}: {e}")
            await record_row_failure(
                campaign_id,
                progress,
                i,
                consumer_email,
                f"Unexpected row error: {str(e)}",
            )
        
        # Calculate timing statistics
        elapsed = time.time() - email_start_time
        send_times.append(elapsed)
        # Use last 100 sends for rolling average
        recent_times = send_times[-100:]
        avg_time = sum(recent_times) / len(recent_times)
        progress["avg_time_per_email"] = round(avg_time, 3)
        
        remaining_emails = total - (i + 1)
        est_seconds = remaining_emails * avg_time
        est_hours = int(est_seconds // 3600)
        est_minutes = int((est_seconds % 3600) // 60)
        est_secs = int(est_seconds % 60)
        progress["estimated_remaining"] = f"{est_hours}h {est_minutes}m {est_secs}s"
        
        # Rate limiting: wait between emails
        sleep_time = max(0, delay_between_emails - elapsed)
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)
        
        # Batch pause: take a longer break every N emails
        if batch_size > 0 and (i + 1) % batch_size == 0 and i < total - 1:
            logger.info(f"Campaign {campaign_id}: Batch pause at {i+1}/{total}")
            progress["status"] = f"batch_pause ({batch_pause}s)"
            await asyncio.sleep(batch_pause)
            progress["status"] = "running"
        
        # Periodically update database (every 50 emails)
        if (i + 1) % 50 == 0:
            await save_campaign_to_db({
                "id": campaign_id,
                "status": "running",
                "total_emails": total,
                "sent_count": progress["sent"],
                "failed_count": progress["failed"],
                "from_email": config["from_email"],
                "subject_pattern": config["subject_pattern"],
                "rate_per_minute": rate_per_minute,
                "started_at": progress["started_at"],
            })
    
    # Campaign complete!
    if progress["status"] != "cancelled":
        progress["status"] = "completed"
    
    progress["finished_at"] = datetime.now(timezone.utc).isoformat()
    
    # Calculate final statistics
    start_dt = datetime.fromisoformat(progress["started_at"])
    end_dt = datetime.fromisoformat(progress["finished_at"])
    duration = (end_dt - start_dt).total_seconds()
    progress["duration_seconds"] = round(duration, 1)
    progress["duration_human"] = f"{int(duration//3600)}h {int((duration%3600)//60)}m {int(duration%60)}s"
    progress["actual_rate"] = round(progress["sent"] / (duration / 60), 2) if duration > 0 else 0
    progress["success_percentage"] = round((progress["sent"] / total) * 100, 2) if total > 0 else 0
    progress["estimated_remaining"] = "0h 0m 0s"
    
    # Save final state to database
    await save_campaign_to_db({
        "id": campaign_id,
        "status": progress["status"],
        "total_emails": total,
        "sent_count": progress["sent"],
        "failed_count": progress["failed"],
        "from_email": config["from_email"],
        "subject_pattern": config["subject_pattern"],
        "rate_per_minute": rate_per_minute,
        "started_at": progress["started_at"],
        "finished_at": progress["finished_at"],
        "duration_seconds": progress["duration_seconds"],
        "actual_rate": progress["actual_rate"],
        "success_percentage": progress["success_percentage"],
    })
    
    logger.info(f"Campaign {campaign_id} finished: {progress['sent']}/{total} sent, {progress['failed']} failed")


# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the main frontend page."""
    with open("static/index.html", "r") as f:
        return HTMLResponse(content=f.read())


@app.post("/api/upload-template")
async def upload_template(template_file: UploadFile = File(...)):
    """
    Upload an HTML email template.
    Returns the list of dynamic fields found in it + detected syntax.
    Auto-detects %%field%% vs {{field}} syntax.
    """
    content = await template_file.read()
    html_content = content.decode("utf-8")
    fields, syntax = extract_template_fields(html_content)
    
    syntax_display = "%%field%%" if syntax == "percent" else "{{field}}"
    
    return {
        "filename": template_file.filename,
        "fields": fields,
        "syntax": syntax,
        "syntax_display": syntax_display,
        "html_preview": html_content[:500] + "..." if len(html_content) > 500 else html_content,
        "html_full": html_content,
    }


@app.post("/api/validate-csv")
async def validate_csv(
    csv_file: UploadFile = File(...),
    template_fields: str = Form(...),  # JSON string of field names from template
):
    """
    Upload a CSV file and validate that it has all required columns
    matching the template's %%field%% placeholders.
    
    Returns: validation result + preview of first 5 rows.
    """
    content = await csv_file.read()
    csv_text = content.decode("utf-8")
    
    # Parse CSV
    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception as e:
        return {
            "valid": False,
            "error": f"CSV could not be parsed. Check for stray commas or broken rows. Details: {str(e)}",
        }
    # Strip whitespace from column names (common issue)
    df.columns = df.columns.str.strip()
    
    # Parse expected fields from template
    required_fields = json.loads(template_fields)
    
    csv_columns = list(df.columns)
    
    # Check which template fields are missing from CSV
    missing = [f for f in required_fields if f not in csv_columns]
    
    # Check if EMAIL_ID column exists (required for sending)
    has_email_column = "EMAIL_ID" in csv_columns
    
    if missing:
        return {
            "valid": False,
            "error": f"CSV is missing these columns that the template needs: {', '.join(missing)}",
            "csv_columns": csv_columns,
            "required_fields": required_fields,
            "missing_fields": missing,
        }
    
    if not has_email_column:
        return {
            "valid": False,
            "error": "CSV must have an 'EMAIL_ID' column containing recipient email addresses",
            "csv_columns": csv_columns,
            "required_fields": required_fields,
        }
    
    invalid_email_rows = []
    for index, email in enumerate(df["EMAIL_ID"].fillna("").astype(str)):
        is_valid_email, validation_error = validate_email_address(email)
        if not is_valid_email:
            invalid_email_rows.append({
                "row_number": index + 2,  # Header is row 1 in the uploaded CSV.
                "email": email,
                "error": validation_error,
            })

    if invalid_email_rows:
        examples = "; ".join(
            f"row {item['row_number']}: {item['email'] or 'EMPTY'}"
            for item in invalid_email_rows[:5]
        )
        return {
            "valid": False,
            "error": (
                f"CSV has {len(invalid_email_rows)} invalid EMAIL_ID value(s). "
                f"Fix examples: {examples}"
            ),
            "csv_columns": csv_columns,
            "required_fields": required_fields,
            "invalid_email_count": len(invalid_email_rows),
            "invalid_email_sample": invalid_email_rows[:20],
        }

    # Return success with preview
    preview = df.head(5).fillna("").to_dict(orient="records")
    
    return {
        "valid": True,
        "total_rows": len(df),
        "csv_columns": csv_columns,
        "required_fields": required_fields,
        "preview": preview,
        "csv_data_json": df.fillna("").to_json(orient="records"),
    }


@app.post("/api/send-test")
async def send_test_email(
    to_email: str = Form(...),
    from_email: str = Form(...),
    html_template: str = Form(...),
    subject_pattern: str = Form(...),
    test_data: str = Form(...),  # JSON string of field values for test
    unsubscribe_group_id: int = Form(25279),
    template_syntax: str = Form("percent"),  # "percent" or "curly"
):
    """
    Send a single test email to verify the template looks correct
    before launching the full campaign.
    """
    if not SENDGRID_API_KEY:
        raise HTTPException(status_code=500, detail="SendGrid API key not configured")

    is_valid_email, validation_error = validate_email_address(to_email)
    if not is_valid_email:
        return {
            "success": False,
            "error": validation_error,
        }
    
    try:
        # Parse test data
        data = json.loads(test_data)
        
        # Create the right Jinja env for this template
        jinja_env = get_jinja_env(template_syntax)
        
        # Render template
        template = jinja_env.from_string(html_template)
        html_content = template.render(**data)
        
        # Generate subject
        subject = generate_subject_line(data, subject_pattern, jinja_env)
        
        # Send via SendGrid
        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
        )
        
        if unsubscribe_group_id:
            asm = Asm(group_id=unsubscribe_group_id)
            message.asm = asm
        
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        
        return {
            "success": response.status_code == 202,
            "status_code": response.status_code,
            "message": "Test email sent successfully!" if response.status_code == 202 else f"Unexpected status: {response.status_code}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


@app.post("/api/start-campaign")
async def start_campaign(
    html_template: str = Form(...),
    csv_data: str = Form(...),          # JSON string of all CSV rows
    from_email: str = Form("consumer.notification@astraglobal.info"),
    subject_pattern: str = Form("VALIDATION LETTER- %%First_Name%% %%Middle_Name%% %%Last_Name%%"),
    unsubscribe_group_id: int = Form(25279),
    rate_per_minute: int = Form(60),    # Default: 60 emails/minute (safe rate)
    batch_size: int = Form(500),         # Pause every 500 emails
    batch_pause_seconds: int = Form(10), # Pause for 10 seconds between batches
    retry_count: int = Form(3),          # Retry failed emails 3 times
    template_syntax: str = Form("percent"),  # "percent" for %%field%%, "curly" for {{field}}
):
    """
    Start a new email campaign as a background task.
    
    This endpoint immediately returns a campaign_id.
    The actual email sending happens in the background.
    Use /api/campaign/{id}/progress to track progress.
    """
    if not SENDGRID_API_KEY:
        raise HTTPException(status_code=500, detail="SendGrid API key not configured")

    if EMAIL_QUEUE_ENABLED and not queue_mode_available():
        raise HTTPException(status_code=503, detail="Queue mode requires Supabase configuration")
    
    # Parse CSV data
    rows = json.loads(csv_data)
    
    if not rows:
        raise HTTPException(status_code=400, detail="No email data provided")
    
    # Enforce safe rate limits
    # SendGrid allows 600/min, but we cap at 500 to leave headroom
    rate_per_minute = min(rate_per_minute, 500)
    rate_per_minute = max(rate_per_minute, 1)
    
    # Generate unique campaign ID
    campaign_id = str(uuid.uuid4())[:12]
    
    # Configure the campaign
    config = {
        "html_template": html_template,
        "csv_data": rows,
        "from_email": from_email,
        "subject_pattern": subject_pattern,
        "unsubscribe_group_id": unsubscribe_group_id,
        "rate_per_minute": rate_per_minute,
        "batch_size": batch_size,
        "batch_pause_seconds": batch_pause_seconds,
        "retry_count": retry_count,
        "template_syntax": template_syntax,
    }

    if queue_mode_available():
        started_at = datetime.now(timezone.utc).isoformat()
        await save_campaign_to_db({
            "id": campaign_id,
            "status": "queued",
            "total_emails": len(rows),
            "sent_count": 0,
            "failed_count": 0,
            "from_email": from_email,
            "subject_pattern": subject_pattern,
            "rate_per_minute": rate_per_minute,
            "started_at": started_at,
            "config_json": json.dumps(build_campaign_config(config, include_template=True)),
        })

        enqueued = await enqueue_campaign_jobs(campaign_id, rows, retry_count)
        if not enqueued:
            await save_campaign_to_db({
                "id": campaign_id,
                "status": "failed",
                "total_emails": len(rows),
                "sent_count": 0,
                "failed_count": 0,
                "from_email": from_email,
                "subject_pattern": subject_pattern,
                "rate_per_minute": rate_per_minute,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            raise HTTPException(status_code=503, detail="Failed to enqueue campaign jobs")

        return {
            "campaign_id": campaign_id,
            "total_emails": len(rows),
            "rate_per_minute": rate_per_minute,
            "message": f"Campaign queued! Worker will send {len(rows)} emails at {rate_per_minute}/min",
        }
    
    # Launch the campaign as a background task
    # asyncio.create_task() starts the function running in the background
    # and returns immediately - the campaign keeps running on the server
    asyncio.create_task(run_campaign(campaign_id, config))
    
    return {
        "campaign_id": campaign_id,
        "total_emails": len(rows),
        "rate_per_minute": rate_per_minute,
        "message": f"Campaign started! Sending {len(rows)} emails at {rate_per_minute}/min",
    }


@app.get("/api/campaign/{campaign_id}/progress")
async def get_campaign_progress(campaign_id: str):
    """
    Get the current progress of a running campaign.
    Returns all statistics: sent, failed, rate, ETA, etc.
    """
    if campaign_id in active_campaigns:
        return active_campaigns[campaign_id]
    
    # If not in memory, check database (campaign may have finished before server restart)
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            result = await supabase_request("select", "campaigns", params={
                "id": f"eq.{campaign_id}",
                "limit": "1",
            })
            if result and isinstance(result, list) and len(result) > 0:
                return normalize_campaign_record(result[0])
        except Exception as e:
            logger.error(f"Database query error: {e}")
    
    raise HTTPException(status_code=404, detail="Campaign not found")


@app.get("/api/campaign/{campaign_id}/stream")
async def stream_campaign_progress(campaign_id: str):
    """
    Real-time progress updates using Server-Sent Events (SSE).
    
    SSE is a technology where the server keeps an HTTP connection open
    and periodically pushes data to the client. The browser automatically
    reconnects if the connection drops.
    
    The frontend uses EventSource API to listen to this endpoint.
    """
    async def event_generator():
        while True:
            if campaign_id in active_campaigns:
                progress = active_campaigns[campaign_id]
                data = json.dumps(progress)
                yield f"data: {data}\n\n"
                
                # If campaign is done, send final update and stop
                if progress["status"] in ("completed", "cancelled"):
                    yield f"data: {json.dumps(progress)}\n\n"
                    break
            else:
                progress = None
                if SUPABASE_URL and SUPABASE_KEY:
                    result = await supabase_request("select", "campaigns", params={
                        "id": f"eq.{campaign_id}",
                        "limit": "1",
                    })
                    if result and isinstance(result, list) and len(result) > 0:
                        progress = normalize_campaign_record(result[0])

                if not progress:
                    yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                    break

                yield f"data: {json.dumps(progress)}\n\n"
                if progress.get("status") in ("completed", "cancelled", "failed"):
                    break
            
            # Send updates every 2 seconds
            await asyncio.sleep(2)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/campaign/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: str):
    """Cancel a running campaign. Emails already sent cannot be un-sent."""
    if campaign_id in active_campaigns:
        active_campaigns[campaign_id]["status"] = "cancelled"
        return {"message": "Campaign cancellation requested. It will stop after the current email."}
    raise HTTPException(status_code=404, detail="Campaign not found or already finished")


@app.get("/api/campaigns")
async def list_campaigns():
    """List all campaigns from database (for history view)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        # Return from memory if no database configured
        return list(active_campaigns.values())
    
    try:
        result = await supabase_request("select", "campaigns", params={
            "order": "started_at.desc",
            "limit": "50",
        })
        if result and isinstance(result, list):
            return result
        return list(active_campaigns.values())
    except Exception as e:
        logger.error(f"Failed to list campaigns: {e}")
        return list(active_campaigns.values())


@app.get("/api/campaign/{campaign_id}/logs")
async def get_campaign_logs(campaign_id: str, limit: int = 100, offset: int = 0):
    """Get detailed email logs for a specific campaign from database."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=503, detail="Database not configured")
    
    try:
        result = await supabase_request("select", "email_logs", params={
            "campaign_id": f"eq.{campaign_id}",
            "order": "email_index",
            "limit": str(limit),
            "offset": str(offset),
        })
        return {"logs": result if isinstance(result, list) else [], "offset": offset, "limit": limit}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/health")
async def health_check():
    """Health check endpoint for Railway to verify the app is running."""
    return {
        "status": "healthy",
        "sendgrid_configured": bool(SENDGRID_API_KEY),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        "active_campaigns": len(active_campaigns),
    }


# ============================================================
# STARTUP
# ============================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
