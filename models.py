
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Booking(Base):
    """Ride booking / request."""

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    rider_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pickup: Mapped[str] = mapped_column(String(255))
    destination: Mapped[str] = mapped_column(String(255))
    cab_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="pending")

    # Fare / route fields
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fare_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    fare_actual: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Promo applied
    promo_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    discount_amount: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    payment: Mapped["Payment | None"] = relationship(
        "Payment", back_populates="booking", uselist=False
    )
    reviews: Mapped[list["Review"]] = relationship(
        "Review", back_populates="booking"
    )


class Payment(Base):
    """Stripe payment records linked to a booking."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    booking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookings.id"), unique=True, index=True
    )
    stripe_payment_intent_id: Mapped[str] = mapped_column(
        String(200), unique=True, index=True
    )
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(10), default="inr")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    booking: Mapped["Booking"] = relationship("Booking", back_populates="payment")


class Review(Base):
    """Ratings and reviews left after a completed ride."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    booking_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookings.id"), index=True
    )
    rater_id: Mapped[str] = mapped_column(String(100), index=True)
    rating: Mapped[int] = mapped_column(Integer)  # 1-5
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    booking: Mapped["Booking"] = relationship("Booking", back_populates="reviews")


class PromoCode(Base):
    """Promotional discount codes."""

    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    discount_percent: Mapped[float] = mapped_column(Float)  # e.g. 10.0 = 10%
    max_discount_inr: Mapped[float | None] = mapped_column(Float, nullable=True)  # cap
    min_fare_inr: Mapped[float | None] = mapped_column(Float, nullable=True)  # minimum fare to apply
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    uses_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class Wallet(Base):
    """Rider wallet — one row per user."""

    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    balance_inr: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class WalletTransaction(Base):
    """Audit log of all wallet credits and debits."""

    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    amount_inr: Mapped[float] = mapped_column(Float)   # positive = credit, negative = debit
    transaction_type: Mapped[str] = mapped_column(String(30))  # topup / ride_payment / refund
    reference_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # booking_id or stripe intent
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
