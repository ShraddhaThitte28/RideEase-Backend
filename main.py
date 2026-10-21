"""
RideEase — FastAPI backend
Covers:
  Day 1:  auth middleware (Supabase token verification)
  Day 2:  Booking model / DB
  Day 3:  /estimate with OSM/OSRM free road routing, /bookings ride request
  Day 4:  Stripe PaymentIntent + webhook (test mode)
  Day 5:  Ratings & reviews, rate limiting, ride history
  Day 8:  PDF receipt download endpoint (WeasyPrint)
  Day 10: Promo codes, wallet balance, wallet top-up
  Day 12: CORS configurable via ALLOWED_ORIGINS env var
"""

import os
import json

import httpx
import stripe
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from database import get_db
from models import Booking, Payment, PromoCode, Review, Wallet, WalletTransaction

load_dotenv(override=False)

# ─── Config ──────────────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Free OSM routing config (no API key required)
# Nominatim geocoding: https://nominatim.org/release-docs/develop/api/Search/
# OSRM routing:        https://project-osrm.org/ (public demo server)
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
OSRM_URL = os.getenv("OSRM_URL", "https://router.project-osrm.org")
ROUTING_USER_AGENT = os.getenv("ROUTING_USER_AGENT", "RideEase-CabBooking/1.0")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Fare rates (INR)
CAB_RATES = {
    "Go": {"base_fare": 30, "per_km": 12},
    "Sedan": {"base_fare": 50, "per_km": 16},
    "SUV": {"base_fare": 80, "per_km": 20},
}

# ─── CORS — configurable via ALLOWED_ORIGINS env var (Day 12) ─────────────────
# Comma-separated list of allowed origins.  Defaults to localhost dev URLs.
# Example: ALLOWED_ORIGINS=https://myapp.vercel.app,https://api.myapp.com
_raw_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000",
)
ALLOWED_ORIGINS: list[str] = [
    o.strip() for o in _raw_origins.split(",") if o.strip()
]

# ─── App setup ────────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="RideEase Cab Booking Platform API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# ─── Auth helper ─────────────────────────────────────────────────────────────


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Please log in to continue.",
        )

    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="Supabase configuration is missing on the server.",
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "apikey": SUPABASE_PUBLISHABLE_KEY,
                    "Authorization": f"Bearer {credentials.credentials}",
                },
            )

        if response.status_code != 200:
            print("Supabase auth verification failed:", response.status_code)
            raise HTTPException(
                status_code=401,
                detail="Your session is invalid or expired. Please log in again.",
            )

        return response.json()

    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Could not verify your session with Supabase. Please try again.",
        )


# ─── Pydantic schemas ─────────────────────────────────────────────────────────


class EstimateRequest(BaseModel):
    pickup: str = Field(..., min_length=2)
    destination: str = Field(..., min_length=2)
    cab_type: str


class BookingRequest(BaseModel):
    pickup: str = Field(..., min_length=2)
    destination: str = Field(..., min_length=2)
    cab_type: str
    promo_code: str | None = None  # Day 10: optional promo code applied at booking


class PaymentIntentRequest(BaseModel):
    booking_id: int


class ReviewRequest(BaseModel):
    booking_id: int
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = None


# ─── Free OSM road-routing helpers (replaces Google Maps) ───────────────────
#
# Pipeline:
#   1. Nominatim (OSM) geocodes each address to lat/lon — free, no key needed
#   2. OSRM public server returns real road distance + drive time — free
#
# Policy — NEVER return straight-line distance as road distance:
#   • Geocoding failure  → raise HTTP 503 with clear message
#   • OSRM failure       → raise HTTP 503 with clear message
#   • No-route found     → raise HTTP 503 with clear message
#
# Rate limits (public servers):
#   Nominatim: ≤1 req/s, must send a descriptive User-Agent
#   OSRM demo: unmetered for light use; self-host for production




async def _geocode(place: str, client: httpx.AsyncClient) -> tuple[float, float]:
    """Geocode a location, prioritizing Pune when appropriate."""
    headers = {"User-Agent": ROUTING_USER_AGENT}
    place = place.strip()

    queries = [place]
    if "pune" not in place.lower():
        queries.append(f"{place}, Pune, Maharashtra, India")

    for query in queries:
        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "countrycodes": "in",
        }

        try:
            resp = await client.get(
                f"{NOMINATIM_URL}/search",
                params=params,
                headers=headers,
                timeout=10.0,
            )
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            results = resp.json()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Geocoding failed for '{place}': {type(exc).__name__}: {exc}",
            ) from exc

        if results:
            result = results[0]
            return float(result["lat"]), float(result["lon"])

    raise HTTPException(
        status_code=400,
        detail=f"Could not find '{place}' in Pune. Try a nearby landmark or full address.",
    )


async def get_route_data(origin: str, destination: str) -> dict:
    """
    Return real road distance (km) and drive time (minutes) using free OSM tools:
      • Nominatim — address → lat/lon  (https://nominatim.org)
      • OSRM      — lat/lon → route    (https://project-osrm.org)

    Raises HTTP 503 on any failure. Never returns straight-line distance.
    """
    async with httpx.AsyncClient() as client:
        # Step 1 — geocode both endpoints (sequential; Nominatim rate-limit is 1 req/s)
        pickup_lat, pickup_lon = await _geocode(origin, client)
        dest_lat, dest_lon = await _geocode(destination, client)

        # Step 2 — OSRM driving route
        # Format: /route/v1/driving/{lon1},{lat1};{lon2},{lat2}
        osrm_url = (
            f"{OSRM_URL}/route/v1/driving/"
            f"{pickup_lon},{pickup_lat};{dest_lon},{dest_lat}"
        )
        try:
            resp = await client.get(
                osrm_url,
                params={"overview": "false", "alternatives": "false"},
                timeout=10.0,
            )
            data = resp.json()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Road-routing service (OSRM) is temporarily unavailable: {exc}. "
                    "Please try again shortly."
                ),
            ) from exc

        if data.get("code") != "Ok" or not data.get("routes"):
            raise HTTPException(
                status_code=503,
                detail=(
                    "No driving route found between the given locations. "
                    "Please verify pickup and destination addresses."
                ),
            )

        route = data["routes"][0]
        distance_km = route["distance"] / 1000
        duration_minutes = route["duration"] / 60

        # Reject suspiciously long routes for this local cab-booking app.
        if distance_km > 500:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The route returned is {distance_km:.2f} km, which seems too long. "
                    "Please enter more specific pickup and destination locations."
                ),
            )
        return {
            "distance_km": round(distance_km, 2),   # metres → km
            "duration_minutes": max(1, round(duration_minutes)), # seconds → min
            "source": "osrm_openstreetmap",
        }


def calculate_fare(distance_km: float, cab_type: str) -> float:
    cab = CAB_RATES.get(cab_type)
    if not cab:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cab type '{cab_type}'. Choose: {', '.join(CAB_RATES)}.",
        )
    return round(cab["base_fare"] + distance_km * cab["per_km"], 2)



# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/")
def home():
    return {"message": "RideEase Cab Booking Platform API is running!"}


# ── Day 3: Estimate ──────────────────────────────────────────────────────────


@app.get("/estimate")
@limiter.limit("30/minute")
async def estimate_fare_get(
    request: Request,
    pickup: str,
    destination: str,
    cab_type: str = "Go",
):
    """GET /estimate?pickup=...&destination=...&cab_type=..."""
    return await _estimate_fare(pickup, destination, cab_type)


@app.post("/estimate")
@limiter.limit("30/minute")
async def estimate_fare_post(request: Request, body: EstimateRequest):
    """POST /estimate — accepts JSON body."""
    return await _estimate_fare(body.pickup, body.destination, body.cab_type)


async def _estimate_fare(pickup: str, destination: str, cab_type: str) -> dict:
    pickup = pickup.strip()
    destination = destination.strip()

    if not pickup or not destination:
        raise HTTPException(
            status_code=400, detail="Pickup and destination are required."
        )
    if pickup.lower() == destination.lower():
        raise HTTPException(
            status_code=400, detail="Pickup and destination must be different."
        )
    if cab_type not in CAB_RATES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cab type. Choose: {', '.join(CAB_RATES)}.",
        )

    route = await get_route_data(pickup, destination)
    distance_km = route["distance_km"]
    duration_minutes = route["duration_minutes"]
    source = route["source"]

    if distance_km is None:
        # This branch is kept for safety but OSRM raises 503 on failure
        # rather than returning None, so this should not be reached in normal flow.
        return {
            "pickup": pickup,
            "destination": destination,
            "cab_type": cab_type,
            "distance_km": None,
            "estimated_duration_minutes": None,
            "estimated_fare": None,
            "currency": "INR",
            "note": "Road distance unavailable. Please retry.",
            "source": source,
        }

    fare = calculate_fare(distance_km, cab_type)

    return {
        "pickup": pickup,
        "destination": destination,
        "cab_type": cab_type,
        "distance_km": distance_km,
        "estimated_duration_minutes": duration_minutes,
        "estimated_fare": fare,
        "currency": "INR",
        "note": "Road distance via OpenStreetMap/OSRM. Final fare may vary due to traffic and waiting time.",
        "source": source,
    }


# ── Day 3: Book a ride ───────────────────────────────────────────────────────


@app.post("/bookings")
@limiter.limit("10/minute")
async def create_booking(
    request: Request,
    booking: BookingRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a ride booking. Fetches real distance/fare from Google Maps if key is set.

    Optionally accepts a promo_code that is validated and applied to the fare
    (Day 10: promo application logic).
    """
    from datetime import datetime, timezone

    pickup = booking.pickup.strip()
    destination = booking.destination.strip()
    cab_type = booking.cab_type

    if not pickup or not destination:
        raise HTTPException(
            status_code=400, detail="Pickup and destination are required."
        )
    if pickup.lower() == destination.lower():
        raise HTTPException(
            status_code=400, detail="Pickup and destination must be different."
        )
    if cab_type not in CAB_RATES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cab type. Choose: {', '.join(CAB_RATES)}.",
        )

    route = await get_route_data(pickup, destination)
    distance_km = route["distance_km"]
    duration_minutes = route["duration_minutes"]
    fare_estimate = calculate_fare(distance_km, cab_type) if distance_km else None

    # ── Day 10: Promo code application logic ─────────────────────────────────
    applied_promo_code: str | None = None
    discount_amount: float | None = None
    final_fare: float | None = fare_estimate

    raw_promo = (booking.promo_code or "").strip().upper()
    if raw_promo and fare_estimate is not None:
        promo = (
            db.query(PromoCode)
            .filter(PromoCode.code == raw_promo, PromoCode.is_active == True)
            .first()
        )
        if not promo:
            raise HTTPException(status_code=404, detail="Invalid or expired promo code.")
        if promo.expires_at and promo.expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="This promo code has expired.")
        if promo.max_uses is not None and promo.uses_count >= promo.max_uses:
            raise HTTPException(status_code=400, detail="Promo code usage limit reached.")
        if promo.min_fare_inr and fare_estimate < promo.min_fare_inr:
            raise HTTPException(
                status_code=400,
                detail=f"Minimum fare of \u20b9{promo.min_fare_inr:.0f} required for this promo.",
            )
        disc = round(fare_estimate * promo.discount_percent / 100, 2)
        if promo.max_discount_inr:
            disc = min(disc, promo.max_discount_inr)
        applied_promo_code = promo.code
        discount_amount = disc
        final_fare = round(fare_estimate - disc, 2)
        # Atomic increment — committed together with the booking below
        promo.uses_count += 1
    elif raw_promo and fare_estimate is None:
        # Promo requested but no fare available — ignore silently
        pass

    new_booking = Booking(
        user_id=current_user.get("id"),
        rider_email=current_user.get("email"),  # stored for Celery email task
        pickup=pickup,
        destination=destination,
        cab_type=cab_type,
        status="pending",
        distance_km=distance_km,
        duration_minutes=duration_minutes,
        fare_estimate=final_fare,  # store post-discount fare as the estimate
        promo_code=applied_promo_code,
        discount_amount=discount_amount,
    )

    try:
        db.add(new_booking)
        db.commit()  # commits booking + promo uses_count together
        db.refresh(new_booking)
    except Exception:
        db.rollback()
        raise

    return {
        "message": "Booking created successfully!",
        "booking_id": new_booking.id,
        "user_id": new_booking.user_id,
        "pickup": new_booking.pickup,
        "destination": new_booking.destination,
        "cab_type": new_booking.cab_type,
        "status": new_booking.status,
        "distance_km": new_booking.distance_km,
        "estimated_duration_minutes": new_booking.duration_minutes,
        "fare_estimate": new_booking.fare_estimate,
        "promo_code": new_booking.promo_code,
        "discount_amount": new_booking.discount_amount,
        "original_fare": fare_estimate,
        "currency": "INR",
    }


# ── Day 5: Ride history ───────────────────────────────────────────────────────


@app.get("/bookings")
@limiter.limit("30/minute")
async def get_bookings(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all bookings for the authenticated user, newest first."""
    bookings = (
        db.query(Booking)
        .filter(Booking.user_id == current_user.get("id"))
        .order_by(Booking.id.desc())
        .all()
    )

    return [
        {
            "booking_id": item.id,
            "pickup": item.pickup,
            "destination": item.destination,
            "cab_type": item.cab_type,
            "status": item.status,
            "distance_km": item.distance_km,
            "estimated_duration_minutes": item.duration_minutes,
            "fare_estimate": item.fare_estimate,
            "fare_actual": item.fare_actual,
            "currency": "INR",
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in bookings
    ]


@app.get("/bookings/{booking_id}")
@limiter.limit("30/minute")
async def get_booking(
    request: Request,
    booking_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get status of a specific booking (rider polls this for updates)."""
    booking = (
        db.query(Booking)
        .filter(
            Booking.id == booking_id,
            Booking.user_id == current_user.get("id"),
        )
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")

    payment = None
    if booking.payment:
        payment = {
            "stripe_payment_intent_id": booking.payment.stripe_payment_intent_id,
            "amount": booking.payment.amount,
            "status": booking.payment.status,
        }

    return {
        "booking_id": booking.id,
        "pickup": booking.pickup,
        "destination": booking.destination,
        "cab_type": booking.cab_type,
        "status": booking.status,
        "distance_km": booking.distance_km,
        "estimated_duration_minutes": booking.duration_minutes,
        "fare_estimate": booking.fare_estimate,
        "fare_actual": booking.fare_actual,
        "currency": "INR",
        "created_at": booking.created_at.isoformat() if booking.created_at else None,
        "payment": payment,
    }


# ── Day 4: Stripe PaymentIntent ───────────────────────────────────────────────


@app.post("/payments/create-intent")
@limiter.limit("10/minute")
async def create_payment_intent(
    request: Request,
    body: PaymentIntentRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a Stripe PaymentIntent for a booking.
    Requires STRIPE_SECRET_KEY in backend/.env.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "Stripe is not configured on this server. "
                "Add STRIPE_SECRET_KEY to backend/.env."
            ),
        )

    booking = (
        db.query(Booking)
        .filter(
            Booking.id == body.booking_id,
            Booking.user_id == current_user.get("id"),
        )
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")

    fare = booking.fare_estimate
    if not fare:
        raise HTTPException(
            status_code=400,
            detail="Fare estimate is not available for this booking. Cannot create payment.",
        )

    # Stripe amounts are in the smallest currency unit (paise for INR)
    amount_paise = int(fare * 100)

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_paise,
            currency="inr",
            metadata={
                "booking_id": booking.id,
                "user_id": current_user.get("id"),
                "cab_type": booking.cab_type,
            },
            description=f"RideEase booking #{booking.id}",
        )
    except stripe.StripeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Persist payment record
    payment = Payment(
        booking_id=booking.id,
        stripe_payment_intent_id=intent["id"],
        amount=fare,
        currency="inr",
        status="pending",
    )
    db.add(payment)
    booking.status = "payment_pending"
    db.commit()

    return {
        "client_secret": intent["client_secret"],
        "payment_intent_id": intent["id"],
        "amount": fare,
        "currency": "INR",
        "booking_id": booking.id,
    }


@app.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(None, alias="stripe-signature"),
    db: Session = Depends(get_db),
):
    """
    Handle Stripe webhook events (payment confirmation).
    Requires STRIPE_WEBHOOK_SECRET in backend/.env.
    """
    payload = await request.body()

    if not STRIPE_WEBHOOK_SECRET:
        # In dev without a webhook secret, trust the raw event
        try:
            event = json.loads(payload)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid payload.")
    else:
        try:
            event = stripe.Webhook.construct_event(
                payload, stripe_signature, STRIPE_WEBHOOK_SECRET
            )
        except stripe.SignatureVerificationError:
            raise HTTPException(status_code=400, detail="Invalid Stripe signature.")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid payload.")

    event_type = event.get("type", "")
    intent_data = event.get("data", {}).get("object", {})
    intent_id = intent_data.get("id", "")

    payment = (
        db.query(Payment)
        .filter(Payment.stripe_payment_intent_id == intent_id)
        .first()
    )

    if payment:
        if event_type == "payment_intent.succeeded":
            # Guard: only act if this is the first success (prevents duplicate processing)
            already_succeeded = payment.status == "succeeded"
            payment.status = "succeeded"
            if payment.booking:
                payment.booking.status = "confirmed"
            db.commit()

            # Day 8: Trigger Celery receipt-email task exactly once per payment
            if not already_succeeded and payment.booking:
                booking_obj = payment.booking
                rider_email = booking_obj.rider_email
                if rider_email:
                    try:
                        from tasks import send_receipt_email
                        booking_dict = {
                            "booking_id": booking_obj.id,
                            "pickup": booking_obj.pickup,
                            "destination": booking_obj.destination,
                            "cab_type": booking_obj.cab_type,
                            "status": booking_obj.status,
                            "distance_km": booking_obj.distance_km,
                            "duration_minutes": booking_obj.duration_minutes,
                            "fare_estimate": booking_obj.fare_estimate,
                            "fare_actual": booking_obj.fare_actual,
                            "promo_code": booking_obj.promo_code,
                            "discount_amount": booking_obj.discount_amount,
                            "created_at": (
                                booking_obj.created_at.strftime("%d %b %Y, %I:%M %p")
                                if booking_obj.created_at else ""
                            ),
                        }
                        send_receipt_email.delay(booking_dict, rider_email)
                    except Exception as email_exc:
                        # Never let email failure block the webhook response
                        print(f"[webhook] Email task enqueue failed: {email_exc}")

        elif event_type in (
            "payment_intent.payment_failed",
            "payment_intent.canceled",
        ):
            payment.status = "failed"
            if payment.booking:
                payment.booking.status = "payment_failed"
            db.commit()

    return Response(content=json.dumps({"received": True}), media_type="application/json")


# ── Day 5: Ratings & Reviews ─────────────────────────────────────────────────


@app.post("/reviews")
@limiter.limit("10/minute")
async def create_review(
    request: Request,
    body: ReviewRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a rating (1-5) and optional comment for a completed booking."""
    booking = (
        db.query(Booking)
        .filter(Booking.id == body.booking_id)
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")

    # Only the rider who made the booking can rate
    if booking.user_id != current_user.get("id"):
        raise HTTPException(
            status_code=403,
            detail="You can only review your own bookings.",
        )

    # Prevent duplicate reviews
    existing = (
        db.query(Review)
        .filter(
            Review.booking_id == body.booking_id,
            Review.rater_id == current_user.get("id"),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You have already reviewed this booking.",
        )

    review = Review(
        booking_id=body.booking_id,
        rater_id=current_user.get("id"),
        rating=body.rating,
        comment=body.comment,
    )
    db.add(review)
    db.commit()
    db.refresh(review)

    return {
        "review_id": review.id,
        "booking_id": review.booking_id,
        "rating": review.rating,
        "comment": review.comment,
        "created_at": review.created_at.isoformat(),
    }


@app.get("/reviews/{booking_id}")
@limiter.limit("30/minute")
async def get_reviews(
    request: Request,
    booking_id: int,
    db: Session = Depends(get_db),
):
    """Return all reviews for a booking (public endpoint)."""
    reviews = (
        db.query(Review).filter(Review.booking_id == booking_id).all()
    )
    return [
        {
            "review_id": r.id,
            "rating": r.rating,
            "comment": r.comment,
            "created_at": r.created_at.isoformat(),
        }
        for r in reviews
    ]


# ── Day 8: PDF Receipt download ───────────────────────────────────────────────


@app.get("/bookings/{booking_id}/receipt")
@limiter.limit("10/minute")
async def download_receipt(
    request: Request,
    booking_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Download a PDF receipt for a completed booking.
    Returns the PDF as a file download.
    """
    booking = (
        db.query(Booking)
        .filter(
            Booking.id == booking_id,
            Booking.user_id == current_user.get("id"),
        )
        .first()
    )
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found.")

    booking_dict = {
        "booking_id": booking.id,
        "pickup": booking.pickup,
        "destination": booking.destination,
        "cab_type": booking.cab_type,
        "status": booking.status,
        "distance_km": booking.distance_km,
        "duration_minutes": booking.duration_minutes,
        "fare_estimate": booking.fare_estimate,
        "fare_actual": booking.fare_actual,
        "promo_code": booking.promo_code,
        "discount_amount": booking.discount_amount,
        "created_at": booking.created_at.strftime("%d %b %Y, %I:%M %p") if booking.created_at else "",
    }

    try:
        from tasks import generate_receipt_pdf
        pdf_bytes = generate_receipt_pdf(booking_dict)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"PDF generation failed: {exc}. Ensure WeasyPrint is installed.",
        )

    from fastapi.responses import Response as FileResponse
    return FileResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="receipt_{booking_id}.pdf"'
        },
    )


# ── Day 10: Promo Codes ───────────────────────────────────────────────────────


class PromoValidateRequest(BaseModel):
    code: str
    fare: float


@app.post("/promos/validate")
@limiter.limit("20/minute")
async def validate_promo(
    request: Request,
    body: PromoValidateRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate a promo code against a fare and return the discount amount."""
    from datetime import datetime, timezone

    promo = (
        db.query(PromoCode)
        .filter(PromoCode.code == body.code.upper().strip(), PromoCode.is_active == True)
        .first()
    )
    if not promo:
        raise HTTPException(status_code=404, detail="Invalid or expired promo code.")

    # Check expiry
    if promo.expires_at and promo.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="This promo code has expired.")

    # Check usage limit
    if promo.max_uses is not None and promo.uses_count >= promo.max_uses:
        raise HTTPException(status_code=400, detail="This promo code has reached its usage limit.")

    # Check minimum fare
    if promo.min_fare_inr and body.fare < promo.min_fare_inr:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum fare of ₹{promo.min_fare_inr:.0f} required for this promo.",
        )

    discount = round(body.fare * promo.discount_percent / 100, 2)
    if promo.max_discount_inr:
        discount = min(discount, promo.max_discount_inr)

    return {
        "code": promo.code,
        "discount_percent": promo.discount_percent,
        "discount_amount": discount,
        "final_fare": round(body.fare - discount, 2),
    }


# ── Day 10: Wallet ────────────────────────────────────────────────────────────


@app.get("/wallet")
@limiter.limit("30/minute")
async def get_wallet(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's wallet balance."""
    user_id = current_user.get("id")
    wallet = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    if not wallet:
        # Auto-create wallet with 0 balance on first access
        wallet = Wallet(user_id=user_id, balance_inr=0.0)
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    return {
        "user_id": user_id,
        "balance_inr": wallet.balance_inr,
        "updated_at": wallet.updated_at.isoformat() if wallet.updated_at else None,
    }


class WalletTopUpRequest(BaseModel):
    amount_inr: float = Field(..., gt=0, le=10000)
    stripe_payment_intent_id: str | None = None  # provided after Stripe payment succeeds


@app.post("/wallet/topup")
@limiter.limit("5/minute")
async def topup_wallet(
    request: Request,
    body: WalletTopUpRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Top up the wallet balance.
    In production, call /payments/create-intent first to get a Stripe PaymentIntent,
    confirm it on the client, then call this endpoint with the stripe_payment_intent_id.
    For development/testing without Stripe, it credits the wallet directly.
    """
    user_id = current_user.get("id")
    wallet = db.query(Wallet).filter(Wallet.user_id == user_id).first()
    if not wallet:
        wallet = Wallet(user_id=user_id, balance_inr=0.0)
        db.add(wallet)

    wallet.balance_inr = round(wallet.balance_inr + body.amount_inr, 2)

    txn = WalletTransaction(
        user_id=user_id,
        amount_inr=body.amount_inr,
        transaction_type="topup",
        reference_id=body.stripe_payment_intent_id,
    )
    db.add(txn)
    db.commit()
    db.refresh(wallet)

    return {
        "message": f"Wallet topped up by ₹{body.amount_inr:.2f}",
        "new_balance_inr": wallet.balance_inr,
    }


@app.get("/wallet/transactions")
@limiter.limit("20/minute")
async def get_wallet_transactions(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return wallet transaction history for the authenticated user."""
    user_id = current_user.get("id")
    txns = (
        db.query(WalletTransaction)
        .filter(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.id.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": t.id,
            "amount_inr": t.amount_inr,
            "transaction_type": t.transaction_type,
            "reference_id": t.reference_id,
            "created_at": t.created_at.isoformat(),
        }
        for t in txns
    ]
