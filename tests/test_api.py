"""
RideEase — pytest test suite
Tests all FastAPI endpoints using TestClient (sync) and mocks for external services.
Run with:  cd backend && .venv/Scripts/pytest tests/ -v
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Set env vars BEFORE importing main so dotenv doesn't override
os.environ["DATABASE_URL"] = "sqlite:///./test_rideease.db"
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_PUBLISHABLE_KEY", "test-key")
os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["STRIPE_WEBHOOK_SECRET"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Now import app
from database import Base, get_db
from main import app, get_current_user

# ─── In-memory SQLite test DB ────────────────────────────────────────────────

TEST_DB_URL = "sqlite:///./test_rideease.db"
engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Create all tables
Base.metadata.create_all(bind=engine)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


MOCK_USER = {"id": "test-user-123", "email": "test@example.com"}


def override_get_current_user():
    return MOCK_USER


# Apply overrides for all tests that need auth + DB
app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)

# ─── Home ────────────────────────────────────────────────────────────────────


def test_home():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert "running" in data["message"].lower()


# ─── Estimate — free OSM routing (Nominatim + OSRM) tests ───────────────────


def test_estimate_routing_service_unavailable():
    """When Nominatim is unreachable the endpoint returns 503 (never a null fare)."""
    from fastapi import HTTPException as FastAPIHTTPException

    async def fake_geocode_raises(place, client):
        raise FastAPIHTTPException(
            status_code=503,
            detail=f"Geocoding service unreachable for '{place}': connection refused.",
        )

    with patch("main._geocode", new=fake_geocode_raises):
        response = client.post(
            "/estimate",
            json={"pickup": "Mumbai", "destination": "Pune", "cab_type": "Go"},
        )
    assert response.status_code == 503
    assert "geocod" in response.json()["detail"].lower()


def test_estimate_same_pickup_destination():
    response = client.post(
        "/estimate",
        json={"pickup": "Mumbai", "destination": "Mumbai", "cab_type": "Go"},
    )
    assert response.status_code == 400


def test_estimate_invalid_cab_type():
    response = client.post(
        "/estimate",
        json={"pickup": "Delhi", "destination": "Agra", "cab_type": "Helicopter"},
    )
    assert response.status_code == 400


def test_estimate_with_osrm_mocked():
    """Mock Nominatim + OSRM: 150 km / 2 h route → Sedan fare ₹2450."""
    nominatim_payload = [{"lat": "28.6139", "lon": "77.2090"}]
    osrm_payload = {
        "code": "Ok",
        "routes": [{"distance": 150000.0, "duration": 7200.0}],
    }

    class FakeNominatimResp:
        def json(self):
            return nominatim_payload

    class FakeOSRMResp:
        def json(self):
            return osrm_payload

    async def fake_get(self, url, **kwargs):
        """Return Nominatim data for /search calls, OSRM data for /route calls."""
        if "/search" in url:
            return FakeNominatimResp()
        return FakeOSRMResp()

    with patch("httpx.AsyncClient.get", new=fake_get):
        response = client.post(
            "/estimate",
            json={"pickup": "Delhi", "destination": "Agra", "cab_type": "Sedan"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["distance_km"] == 150.0
    assert data["estimated_duration_minutes"] == 120
    # Fare formula: base_fare 50 + 150 km × 16/km = 2450
    assert data["estimated_fare"] == 2450.0
    assert data["source"] == "osrm_openstreetmap"



# ─── Booking — auth required ─────────────────────────────────────────────────


def test_create_booking_no_auth():
    # Remove auth override for this test
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.post(
            "/bookings",
            json={"pickup": "Mumbai", "destination": "Pune", "cab_type": "Go"},
        )
        assert response.status_code == 401
    finally:
        if saved:
            app.dependency_overrides[get_current_user] = saved


def test_get_bookings_no_auth():
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.get("/bookings")
        assert response.status_code == 401
    finally:
        if saved:
            app.dependency_overrides[get_current_user] = saved


def test_create_booking_same_pickup_destination_rejected():
    app.dependency_overrides[get_current_user] = override_get_current_user
    response = client.post(
        "/bookings",
        json={"pickup": "Delhi", "destination": "Delhi", "cab_type": "Go"},
    )
    assert response.status_code == 400


def test_create_booking_succeeds():
    """Booking succeeds when OSRM returns a valid route (mocked to avoid live calls)."""
    app.dependency_overrides[get_current_user] = override_get_current_user

    nominatim_payload = [{"lat": "19.0760", "lon": "72.8777"}]
    osrm_payload = {"code": "Ok", "routes": [{"distance": 150000.0, "duration": 7200.0}]}

    class FakeNominatimResp:
        def json(self):
            return nominatim_payload

    class FakeOSRMResp:
        def json(self):
            return osrm_payload

    async def fake_get(self, url, **kwargs):
        if "/search" in url:
            return FakeNominatimResp()
        return FakeOSRMResp()

    with patch("httpx.AsyncClient.get", new=fake_get):
        response = client.post(
            "/bookings",
            json={"pickup": "Mumbai", "destination": "Pune", "cab_type": "Go"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["booking_id"] > 0
    assert data["status"] == "pending"


def test_get_bookings_returns_list():
    app.dependency_overrides[get_current_user] = override_get_current_user
    response = client.get("/bookings")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# ─── Reviews ─────────────────────────────────────────────────────────────────


def test_get_reviews_nonexistent_booking():
    """Should return empty list for a booking with no reviews."""
    response = client.get("/reviews/999999")
    assert response.status_code == 200
    assert response.json() == []


def test_create_review_no_auth():
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.post(
            "/reviews",
            json={"booking_id": 1, "rating": 5, "comment": "Great ride!"},
        )
        assert response.status_code == 401
    finally:
        if saved:
            app.dependency_overrides[get_current_user] = saved


def test_review_rating_out_of_range():
    app.dependency_overrides[get_current_user] = override_get_current_user
    response = client.post(
        "/reviews",
        json={"booking_id": 1, "rating": 6},
    )
    assert response.status_code == 422  # Pydantic validation error


def test_create_review_for_own_booking():
    """Create a booking (OSRM mocked), then submit a review for it."""
    app.dependency_overrides[get_current_user] = override_get_current_user

    nominatim_payload = [{"lat": "17.3850", "lon": "78.4867"}]
    osrm_payload = {"code": "Ok", "routes": [{"distance": 150000.0, "duration": 21600.0}]}

    class FakeNominatimResp:
        def json(self):
            return nominatim_payload

    class FakeOSRMResp:
        def json(self):
            return osrm_payload

    async def fake_get(self, url, **kwargs):
        if "/search" in url:
            return FakeNominatimResp()
        return FakeOSRMResp()

    with patch("httpx.AsyncClient.get", new=fake_get):
        booking_resp = client.post(
            "/bookings",
            json={"pickup": "Hyderabad", "destination": "Bangalore", "cab_type": "SUV"},
        )
    assert booking_resp.status_code == 200
    booking_id = booking_resp.json()["booking_id"]

    # Submit review
    review_resp = client.post(
        "/reviews",
        json={"booking_id": booking_id, "rating": 4, "comment": "Smooth ride!"},
    )
    assert review_resp.status_code == 200
    data = review_resp.json()
    assert data["rating"] == 4
    assert data["comment"] == "Smooth ride!"

    # Duplicate review should 409
    dup_resp = client.post(
        "/reviews",
        json={"booking_id": booking_id, "rating": 3},
    )
    assert dup_resp.status_code == 409


# ─── Stripe ──────────────────────────────────────────────────────────────────


def test_payment_intent_no_stripe_key():
    app.dependency_overrides[get_current_user] = override_get_current_user
    # STRIPE_SECRET_KEY is "" (set at module load above)
    response = client.post(
        "/payments/create-intent",
        json={"booking_id": 1},
    )
    assert response.status_code == 503


def test_stripe_webhook_no_secret_no_op():
    """Without STRIPE_WEBHOOK_SECRET, trusts raw JSON; nonexistent PI is a no-op."""
    payload = json.dumps(
        {
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_nonexistent_test"}},
        }
    ).encode()

    response = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    # No-op (payment not in DB), should succeed with 200
    assert response.status_code == 200
    assert response.json()["received"] is True


# ─── Wallet (Day 10) ─────────────────────────────────────────────────────────


def test_get_wallet_creates_if_missing():
    """GET /wallet auto-creates a wallet with 0 balance on first access."""
    app.dependency_overrides[get_current_user] = override_get_current_user
    response = client.get("/wallet")
    assert response.status_code == 200
    data = response.json()
    assert "balance_inr" in data
    assert data["balance_inr"] >= 0.0
    assert "user_id" in data


def test_wallet_topup():
    """POST /wallet/topup adds the given amount to the wallet balance."""
    app.dependency_overrides[get_current_user] = override_get_current_user

    before = client.get("/wallet").json()["balance_inr"]

    response = client.post("/wallet/topup", json={"amount_inr": 250.0})
    assert response.status_code == 200
    data = response.json()
    assert "new_balance_inr" in data
    assert data["new_balance_inr"] == round(before + 250.0, 2)


def test_wallet_topup_invalid_amount():
    """POST /wallet/topup rejects zero or negative amounts (Pydantic gt=0)."""
    app.dependency_overrides[get_current_user] = override_get_current_user
    response = client.post("/wallet/topup", json={"amount_inr": 0})
    assert response.status_code == 422


def test_wallet_topup_exceeds_limit():
    """POST /wallet/topup rejects amounts above 10 000 INR (Pydantic le=10000)."""
    app.dependency_overrides[get_current_user] = override_get_current_user
    response = client.post("/wallet/topup", json={"amount_inr": 99999})
    assert response.status_code == 422


def test_wallet_topup_no_auth():
    """POST /wallet/topup requires authentication."""
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.post("/wallet/topup", json={"amount_inr": 100.0})
        assert response.status_code == 401
    finally:
        if saved:
            app.dependency_overrides[get_current_user] = saved


def test_get_wallet_transactions():
    """GET /wallet/transactions returns a list with at least one entry after top-up."""
    app.dependency_overrides[get_current_user] = override_get_current_user
    client.post("/wallet/topup", json={"amount_inr": 50.0})
    response = client.get("/wallet/transactions")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    txn = data[0]
    assert "amount_inr" in txn
    assert "transaction_type" in txn


def test_wallet_transactions_no_auth():
    """GET /wallet/transactions requires authentication."""
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.get("/wallet/transactions")
        assert response.status_code == 401
    finally:
        if saved:
            app.dependency_overrides[get_current_user] = saved


# ─── Promo codes (Day 10) ─────────────────────────────────────────────────────


def _seed_promo(code: str = "SAVE10", percent: float = 10.0) -> None:
    """Insert a test promo code directly into the test DB."""
    from models import PromoCode

    db = TestSessionLocal()
    try:
        existing = db.query(PromoCode).filter(PromoCode.code == code).first()
        if not existing:
            db.add(
                PromoCode(
                    code=code,
                    discount_percent=percent,
                    max_discount_inr=None,
                    min_fare_inr=None,
                    max_uses=None,
                    uses_count=0,
                    is_active=True,
                    expires_at=None,
                )
            )
            db.commit()
    finally:
        db.close()


def test_validate_promo_code():
    """POST /promos/validate returns discount details for a valid promo."""
    app.dependency_overrides[get_current_user] = override_get_current_user
    _seed_promo("SAVE10", 10.0)

    response = client.post(
        "/promos/validate",
        json={"code": "SAVE10", "fare": 500.0},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["discount_percent"] == 10.0
    assert data["discount_amount"] == 50.0
    assert data["final_fare"] == 450.0


def test_validate_promo_invalid_code():
    """POST /promos/validate returns 404 for an unknown code."""
    app.dependency_overrides[get_current_user] = override_get_current_user
    response = client.post(
        "/promos/validate",
        json={"code": "FAKECODE999", "fare": 300.0},
    )
    assert response.status_code == 404


def test_validate_promo_no_auth():
    """POST /promos/validate requires authentication."""
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.post(
            "/promos/validate",
            json={"code": "SAVE10", "fare": 300.0},
        )
        assert response.status_code == 401
    finally:
        if saved:
            app.dependency_overrides[get_current_user] = saved


def test_booking_with_invalid_promo_returns_404():
    """
    With OSRM routing active, a fare is always computed.
    Supplying an unknown promo code must return 404 (not silently skip).
    OSRM is mocked so the test is network-independent.
    """
    app.dependency_overrides[get_current_user] = override_get_current_user

    nominatim_payload = [{"lat": "13.0827", "lon": "80.2707"}]
    osrm_payload = {"code": "Ok", "routes": [{"distance": 200000.0, "duration": 9000.0}]}

    class FakeNominatimResp:
        def json(self):
            return nominatim_payload

    class FakeOSRMResp:
        def json(self):
            return osrm_payload

    async def fake_get(self, url, **kwargs):
        if "/search" in url:
            return FakeNominatimResp()
        return FakeOSRMResp()

    with patch("httpx.AsyncClient.get", new=fake_get):
        response = client.post(
            "/bookings",
            json={
                "pickup": "Chennai",
                "destination": "Coimbatore",
                "cab_type": "Sedan",
                "promo_code": "DOESNOTEXIST",
            },
        )
    # OSRM gives a fare → invalid promo code is looked up → correctly 404
    assert response.status_code == 404
    assert "promo" in response.json()["detail"].lower()


def test_booking_without_promo_unaffected():
    """Booking flow without a promo code is unaffected; OSRM mocked for reliability."""
    app.dependency_overrides[get_current_user] = override_get_current_user

    nominatim_payload = [{"lat": "22.5726", "lon": "88.3639"}]
    osrm_payload = {"code": "Ok", "routes": [{"distance": 8000.0, "duration": 1200.0}]}

    class FakeNominatimResp:
        def json(self):
            return nominatim_payload

    class FakeOSRMResp:
        def json(self):
            return osrm_payload

    async def fake_get(self, url, **kwargs):
        if "/search" in url:
            return FakeNominatimResp()
        return FakeOSRMResp()

    with patch("httpx.AsyncClient.get", new=fake_get):
        response = client.post(
            "/bookings",
            json={"pickup": "Kolkata", "destination": "Howrah", "cab_type": "Go"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["booking_id"] > 0
    assert data["promo_code"] is None
    assert data["discount_amount"] is None

