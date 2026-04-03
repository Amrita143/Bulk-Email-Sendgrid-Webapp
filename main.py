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
- Supabase: Cloud database to store campaign data permanently
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
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from jinja2 import Environment
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Asm
from supabase import create_client, Client
import pandas as pd

# ============================================================
# CONFIGURATION
# ============================================================

# These are read from environment variables (set in Railway dashboard)
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # Use the "anon" or "service_role" key

# Initialize Supabase client (our cloud database)
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Jinja2 template engine configured to use %%field%% syntax (matching your existing templates)
jinja_env = Environment(
    variable_start_string='%%',
    variable_end_string='%%',
)

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

def extract_template_fields(html_content: str) -> list:
    """
    Extract all %%field_name%% placeholders from an HTML template.
    Returns a list of unique field names found in the template.
    
    Example: "Hello %%First_Name%% %%Last_Name%%" -> ["First_Name", "Last_Name"]
    """
    # Find all occurrences of %%...%% in the template
    fields = re.findall(r'%%(\w+)%%', html_content)
    # Remove duplicates while preserving order
    seen = set()
    unique_fields = []
    for f in fields:
        if f not in seen:
            seen.add(f)
            unique_fields.append(f)
    return unique_fields


def generate_subject_line(row: dict, subject_pattern: str) -> str:
    """
    Generate the email subject line by replacing %%field%% placeholders.
    Default pattern: 'VALIDATION LETTER- %%First_Name%% %%Middle_Name%% %%Last_Name%%'
    """
    template = jinja_env.from_string(subject_pattern)
    rendered = template.render(**row)
    # Clean up extra spaces from empty middle names
    return ' '.join(rendered.split())


async def save_campaign_to_db(campaign_data: dict):
    """
    Save campaign metadata and results to Supabase.
    This is called when a campaign starts and when it finishes.
    """
    if not supabase:
        logger.warning("Supabase not configured, skipping database save")
        return
    
    try:
        # Upsert = insert if new, update if exists (based on campaign_id)
        supabase.table("campaigns").upsert(campaign_data).execute()
    except Exception as e:
        logger.error(f"Failed to save campaign to database: {e}")


async def save_email_log(log_entry: dict):
    """
    Save individual email send result to Supabase.
    Each row = one email attempt with status code, error message, timestamp.
    """
    if not supabase:
        return
    
    try:
        supabase.table("email_logs").insert(log_entry).execute()
    except Exception as e:
        logger.error(f"Failed to save email log: {e}")


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
        "config_json": json.dumps({
            "batch_size": batch_size,
            "batch_pause": batch_pause,
            "retry_count": retry_count,
            "unsubscribe_group_id": unsubscribe_group_id,
        }),
    })
    
    send_times = []  # Track time taken for each email (for ETA calculation)
    
    for i, row in enumerate(config["csv_data"]):
        # Check if campaign was cancelled by user
        if active_campaigns.get(campaign_id, {}).get("status") == "cancelled":
            progress["status"] = "cancelled"
            break
        
        progress["current_index"] = i
        email_start_time = time.time()
        
        # Prepare the row data: replace NaN/None with empty string
        clean_row = {}
        for key, value in row.items():
            key = key.strip()  # Remove whitespace from column names
            if pd.isna(value) if not isinstance(value, str) else False:
                clean_row[key] = ""
            else:
                clean_row[key] = str(value)
        
        consumer_email = clean_row.get("EMAIL_ID", "").strip()
        
        if not consumer_email:
            progress["failed"] += 1
            progress["errors"].append({
                "index": i,
                "email": "EMPTY",
                "error": "No EMAIL_ID found in row",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            continue
        
        # Render the HTML template with this row's data
        try:
            html_content = template.render(**clean_row)
        except Exception as e:
            progress["failed"] += 1
            progress["errors"].append({
                "index": i,
                "email": consumer_email,
                "error": f"Template render error: {str(e)}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            continue
        
        # Generate subject line
        subject = generate_subject_line(clean_row, config["subject_pattern"])
        
        # Build the SendGrid email message
        message = Mail(
            from_email=config["from_email"],
            to_emails=consumer_email,
            subject=subject,
            html_content=html_content,
        )
        
        # Add unsubscribe group (required for compliance)
        if unsubscribe_group_id:
            asm = Asm(group_id=int(unsubscribe_group_id))
            message.asm = asm
        
        # Try to send with retries
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
                    wait_time = 60  # Wait 60 seconds
                    logger.warning(f"Rate limit hit at index {i}. Waiting {wait_time}s...")
                    progress["status"] = f"rate_limited (waiting {wait_time}s)"
                    await asyncio.sleep(wait_time)
                    progress["status"] = "running"
                else:
                    last_error = f"Status {status_code}"
                    
            except Exception as e:
                last_error = str(e)
                # Wait before retry on network errors
                if attempt < retry_count - 1:
                    await asyncio.sleep(5)
        
        if send_success:
            progress["sent"] += 1
        else:
            progress["failed"] += 1
            error_entry = {
                "index": i,
                "email": consumer_email,
                "error": last_error,
                "status_code": status_code,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            progress["errors"].append(error_entry)
            # Keep only last 100 errors in memory
            if len(progress["errors"]) > 100:
                progress["errors"] = progress["errors"][-100:]
        
        # Save individual email log to database (async, don't wait)
        asyncio.create_task(save_email_log({
            "campaign_id": campaign_id,
            "email_index": i,
            "to_email": consumer_email,
            "status_code": status_code if send_success else 0,
            "success": send_success,
            "error_message": last_error if not send_success else None,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }))
        
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
    Returns the list of dynamic fields (%%field%%) found in it.
    """
    content = await template_file.read()
    html_content = content.decode("utf-8")
    fields = extract_template_fields(html_content)
    
    return {
        "filename": template_file.filename,
        "fields": fields,
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
    df = pd.read_csv(io.StringIO(csv_text))
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
):
    """
    Send a single test email to verify the template looks correct
    before launching the full campaign.
    """
    if not SENDGRID_API_KEY:
        raise HTTPException(status_code=500, detail="SendGrid API key not configured")
    
    try:
        # Parse test data
        data = json.loads(test_data)
        
        # Render template
        template = jinja_env.from_string(html_template)
        html_content = template.render(**data)
        
        # Generate subject
        subject = generate_subject_line(data, subject_pattern)
        
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
):
    """
    Start a new email campaign as a background task.
    
    This endpoint immediately returns a campaign_id.
    The actual email sending happens in the background.
    Use /api/campaign/{id}/progress to track progress.
    """
    if not SENDGRID_API_KEY:
        raise HTTPException(status_code=500, detail="SendGrid API key not configured")
    
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
    if supabase:
        try:
            result = supabase.table("campaigns").select("*").eq("id", campaign_id).execute()
            if result.data:
                return result.data[0]
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
                yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
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
    if not supabase:
        # Return from memory if no database
        return list(active_campaigns.values())
    
    try:
        result = supabase.table("campaigns").select("*").order("started_at", desc=True).limit(50).execute()
        return result.data
    except Exception as e:
        logger.error(f"Failed to list campaigns: {e}")
        return list(active_campaigns.values())


@app.get("/api/campaign/{campaign_id}/logs")
async def get_campaign_logs(campaign_id: str, limit: int = 100, offset: int = 0):
    """Get detailed email logs for a specific campaign from database."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Database not configured")
    
    try:
        result = (
            supabase.table("email_logs")
            .select("*")
            .eq("campaign_id", campaign_id)
            .order("email_index")
            .range(offset, offset + limit - 1)
            .execute()
        )
        return {"logs": result.data, "offset": offset, "limit": limit}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/health")
async def health_check():
    """Health check endpoint for Railway to verify the app is running."""
    return {
        "status": "healthy",
        "sendgrid_configured": bool(SENDGRID_API_KEY),
        "supabase_configured": bool(supabase),
        "active_campaigns": len(active_campaigns),
    }


# ============================================================
# STARTUP
# ============================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
