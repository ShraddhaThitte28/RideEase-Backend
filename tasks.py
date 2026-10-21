"""
Celery application and background tasks.
Requires Redis as broker (REDIS_URL in backend/.env).

Start worker with:
  cd backend
  .venv/Scripts/celery -A tasks worker --loglevel=info --pool=solo
"""

import io
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from celery import Celery
from dotenv import load_dotenv

load_dotenv(override=False)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

celery_app = Celery(
    "rideease",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
)


# ─── PDF receipt helper ───────────────────────────────────────────────────────



def generate_receipt_pdf(booking: dict) -> bytes:
    """Generate a PDF receipt using ReportLab."""
    import io
    from html import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReceiptTitle",
        parent=styles["Title"],
        textColor=colors.HexColor("#1d4ed8"),
        alignment=1,
    ))

    story = [
        Paragraph("RideEase - Ride Receipt", styles["ReceiptTitle"]),
        Spacer(1, 8 * mm),
        Paragraph("Thank you for riding with RideEase!", styles["Normal"]),
        Spacer(1, 5 * mm),
    ]

    fare = booking.get("fare_actual") or booking.get("fare_estimate")
    fare_text = f"Rs. {fare:.2f}" if fare is not None else "Pending"

    rows = [
        ["Booking ID", f"#{booking.get('booking_id', '—')}"],
        ["Status", str(booking.get("status", "pending"))],
        ["Pickup", str(booking.get("pickup", ""))],
        ["Destination", str(booking.get("destination", ""))],
        ["Cab Type", str(booking.get("cab_type", ""))],
        ["Distance", f"{booking.get('distance_km', '—')} km"],
        ["Duration", f"~{booking.get('duration_minutes', '—')} min"],
    ]

    promo = booking.get("promo_code")
    discount = booking.get("discount_amount")
    if promo and discount is not None:
        rows.append(["Promo Code", f"{promo} (-Rs. {discount:.2f})"])

    rows.extend([
        ["Total Fare", fare_text],
        ["Date", str(booking.get("created_at", ""))],
    ])

    safe_rows = [
        [Paragraph(escape(str(cell)), styles["Normal"]) for cell in row]
        for row in rows
    ]

    table = Table(safe_rows, colWidths=[45 * mm, 110 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eff6ff")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#1e293b")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dbeafe")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, len(rows) - 2), (0, len(rows) - 2), "Helvetica-Bold"),
    ]))
    story.append(table)
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph("RideEase Cab Booking Platform - Ride safe!", styles["Normal"]))

    doc.build(story)
    return buffer.getvalue()


# ─── Celery tasks ─────────────────────────────────────────────────────────────


@celery_app.task(name="tasks.send_receipt_email", bind=True, max_retries=3)
def send_receipt_email(self, booking: dict, rider_email: str):
    """
    Background task: generate PDF receipt and email it to the rider.
    Requires SMTP_USER, SMTP_PASSWORD, SMTP_HOST in backend/.env.
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        # Log and skip silently if SMTP not configured
        print(
            f"[tasks.send_receipt_email] SMTP not configured — skipping email for booking #{booking.get('booking_id')}"
        )
        return {"status": "skipped", "reason": "SMTP not configured"}

    try:
        pdf_bytes = generate_receipt_pdf(booking)

        msg = MIMEMultipart()
        msg["From"] = FROM_EMAIL
        msg["To"] = rider_email
        msg["Subject"] = f"Your RideEase Receipt — Booking #{booking.get('booking_id')}"

        body = MIMEText(
            f"Hi,\n\nThank you for riding with RideEase!\n\n"
            f"Booking #{booking.get('booking_id')} — {booking.get('pickup')} → {booking.get('destination')}\n\n"
            f"Please find your PDF receipt attached.\n\nRide safe!\nThe RideEase Team",
            "plain",
        )
        msg.attach(body)

        pdf_attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
        pdf_attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"rideease_receipt_{booking.get('booking_id')}.pdf",
        )
        msg.attach(pdf_attachment)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, rider_email, msg.as_string())

        print(
            f"[tasks.send_receipt_email] Receipt sent to {rider_email} for booking #{booking.get('booking_id')}"
        )
        return {"status": "sent", "to": rider_email}

    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="tasks.cleanup_pending_bookings")
def cleanup_pending_bookings():
    """
    Periodic task: mark bookings that have been 'pending' for >1 hour as 'expired'.
    Schedule via Celery Beat or run manually.
    """
    from datetime import datetime, timezone, timedelta
    from database import SessionLocal
    from models import Booking

    db = SessionLocal()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        stale = (
            db.query(Booking)
            .filter(Booking.status == "pending", Booking.created_at < cutoff)
            .all()
        )
        for b in stale:
            b.status = "expired"
        db.commit()
        print(f"[cleanup] Marked {len(stale)} stale bookings as expired.")
        return {"expired": len(stale)}
    finally:
        db.close()
