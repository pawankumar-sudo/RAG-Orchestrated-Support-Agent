import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import User, Access, AuditLog
from services import jumpcloud, slack


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables on startup."""
    init_db()
    yield


app = FastAPI(
    title="IT Access Manager",
    description="Internal tool for IT access management and user deactivation",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_frontend():
    """Serve the single-page frontend."""
    return FileResponse("templates/index.html")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def search_user(email: str = Query(..., description="User email to look up"), db: Session = Depends(get_db)):
    """
    Look up a user across JumpCloud and Slack, persist results in the DB,
    and return a unified view.
    """
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email address is required.")

    jc_user = None
    slack_user = None
    errors = []

    try:
        jc_user = await jumpcloud.get_user_by_email(email)
    except Exception as e:
        errors.append(f"JumpCloud lookup failed: {e}")

    try:
        slack_user = await slack.get_user_by_email(email)
    except Exception as e:
        errors.append(f"Slack lookup failed: {e}")

    if jc_user or slack_user:
        _upsert_user(db, email, jc_user, slack_user)

    return {
        "email": email,
        "jumpcloud": jc_user,
        "slack": slack_user,
        "errors": errors,
    }


def _upsert_user(db: Session, email: str, jc_user: dict | None, slack_user: dict | None):
    """Insert or update the users and access tables with the latest data."""
    name = (jc_user or {}).get("name") or (slack_user or {}).get("name") or ""
    status = (jc_user or {}).get("status", "unknown")

    db_user = db.query(User).filter(User.email == email).first()
    if db_user:
        db_user.name = name
        db_user.status = status
        db_user.updated_at = datetime.now(timezone.utc)
    else:
        db_user = User(email=email, name=name, status=status, source="jumpcloud")
        db.add(db_user)

    if jc_user:
        _upsert_access(db, email, "jumpcloud", jc_user.get("status", ""))
    if slack_user:
        _upsert_access(db, email, "slack", slack_user.get("role", ""))

    db.commit()


def _upsert_access(db: Session, email: str, tool: str, role: str):
    """Insert or update a single access row."""
    access = db.query(Access).filter(Access.email == email, Access.tool == tool).first()
    if access:
        access.role = role
        access.last_login = datetime.now(timezone.utc)
    else:
        access = Access(email=email, tool=tool, role=role, last_login=datetime.now(timezone.utc))
        db.add(access)


# ---------------------------------------------------------------------------
# Deactivate
# ---------------------------------------------------------------------------

@app.post("/api/deactivate")
async def deactivate_user(
    email: str = Query(...),
    performed_by: str = Query("admin@company.com"),
    db: Session = Depends(get_db),
):
    """
    Deactivate a user in both JumpCloud and Slack, log the action,
    and update the database.
    """
    results = {"jumpcloud": False, "slack": False}
    errors = []

    try:
        jc_user = await jumpcloud.get_user_by_email(email)
        if jc_user and jc_user.get("id"):
            results["jumpcloud"] = await jumpcloud.suspend_user(jc_user["id"])
        elif jc_user is None:
            errors.append("User not found in JumpCloud.")
    except Exception as e:
        errors.append(f"JumpCloud deactivation failed: {e}")

    try:
        slack_user = await slack.get_user_by_email(email)
        if slack_user and slack_user.get("id"):
            results["slack"] = await slack.deactivate_user(slack_user["id"])
        elif slack_user is None:
            errors.append("User not found in Slack.")
    except Exception as e:
        errors.append(f"Slack deactivation failed: {e}")

    db_user = db.query(User).filter(User.email == email).first()
    if db_user:
        db_user.status = "deactivated"
        db_user.updated_at = datetime.now(timezone.utc)

    detail_parts = []
    if results["jumpcloud"]:
        detail_parts.append("JumpCloud: suspended")
    if results["slack"]:
        detail_parts.append("Slack: deactivated")
    if errors:
        detail_parts.append(f"Errors: {'; '.join(errors)}")

    audit = AuditLog(
        performed_by=performed_by,
        action="deactivate",
        target_email=email,
        details=" | ".join(detail_parts) or "No changes made",
    )
    db.add(audit)
    db.commit()

    return {
        "email": email,
        "results": results,
        "errors": errors,
        "message": "Deactivation complete." if any(results.values()) else "No services were deactivated.",
    }


# ---------------------------------------------------------------------------
# All Users (Access Review)
# ---------------------------------------------------------------------------

@app.get("/api/users")
async def list_all_users(db: Session = Depends(get_db)):
    """Return all users stored in the database for access review."""
    users = db.query(User).order_by(User.email).all()
    return [u.to_dict() for u in users]


@app.get("/api/users/access")
async def list_all_access(db: Session = Depends(get_db)):
    """Return all access records for review."""
    records = db.query(Access).order_by(Access.email).all()
    return [r.to_dict() for r in records]


@app.get("/api/slack/users")
async def list_slack_users():
    """Fetch all Slack workspace users directly from the Slack API."""
    try:
        users = await slack.get_all_users()
        return {"ok": True, "users": users, "total": len(users)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Slack API error: {e}")


@app.get("/api/jumpcloud/users")
async def list_jumpcloud_users():
    """Fetch all JumpCloud users directly from the JumpCloud API."""
    try:
        users = await jumpcloud.get_all_users()
        return {"ok": True, "users": users, "total": len(users)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"JumpCloud API error: {e}")


# ---------------------------------------------------------------------------
# Audit Logs
# ---------------------------------------------------------------------------

@app.get("/api/audit-logs")
async def list_audit_logs(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Return the most recent audit log entries."""
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()
    return [l.to_dict() for l in logs]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "IT Access Manager"}
