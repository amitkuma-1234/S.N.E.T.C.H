"""
premium.py — S.N.E.T.C.H Premium subscriptions
=======================================================================
Plans, payment claims, and feature entitlement — all stored in the same
Postgres database as pg_storage.py, keyed by the user's EMAIL. So exactly
like every file feature: sign in with a new email -> no plan, nothing
unlocked. Sign back in with the email that bought premium -> the plan
(and however much time is left on it) is right there.

IMPORTANT — how payment actually gets confirmed:
  There is no bank/UPI API integration here — this app cannot "see" money
  land in a personal UPI account by itself; that would require a real
  payment gateway (Razorpay/Cashfree/PayU/a UPI collect-request API) with
  its own merchant account and webhook, which is a different, much bigger
  integration than what's set up here. What IS implemented is the
  practical version of the same flow:
    1. User picks a plan, pays via PhonePe/Paytm/Google Pay to the UPI
       number shown on the Premium page.
    2. User submits the transaction reference (UTR/Txn ID) — this creates
       a "pending" claim.
    3. YOU (the account holder / admin) check your UPI app, confirm the
       payment really landed, and approve the claim from the small admin
       panel (/api/premium/admin/...). That's the moment the plan
       actually activates.
  This keeps it honest: nothing claims to be "automatic bank detection"
  that isn't really there. If you later want true automatic activation,
  that means plugging in a real payment gateway's webhook instead of
  manual approval — a good next step, but a separate piece of work.
"""

import os
import json
import time
import datetime

import jwt

import pg_storage as pg
import email_utils

# ─────────────────────────── PLAN CATALOG ───────────────────────────
# Exactly as specified: each tier is its own fixed bundle of features —
# NOT cumulative beyond what's listed (e.g. the ₹30/₹90 tier gets Astro
# Insights but not Media Download; the ₹40/₹100 tier gets Media Download
# but not Astro Insights).

FEATURE_LABELS = {
    "music_download": "Music (Song) Download",
    "video_download": "Video Download",
    "astro_insights": "Astro Insights",
    "media_download": "Media Download",
}

_TIERS = [
    ("tier1", "Music Download",                              ["music_download"]),
    ("tier2", "Music + Video Download",                       ["music_download", "video_download"]),
    ("tier3", "Music + Video Download + Astro Insights",       ["music_download", "video_download", "astro_insights"]),
    ("tier4", "Music + Video Download + Media Download",       ["music_download", "video_download", "media_download"]),
]

_MONTHLY_PRICES = {"tier1": 10, "tier2": 20, "tier3": 30, "tier4": 40}
_YEARLY_PRICES  = {"tier1": 70, "tier2": 80, "tier3": 90, "tier4": 100}

PLANS = {}
for tier_id, label, features in _TIERS:
    PLANS[f"monthly_{tier_id}"] = {
        "plan_id": f"monthly_{tier_id}", "cycle": "monthly", "tier": tier_id,
        "label": label, "price": _MONTHLY_PRICES[tier_id], "features": features,
        "duration_days": 30,
    }
    PLANS[f"yearly_{tier_id}"] = {
        "plan_id": f"yearly_{tier_id}", "cycle": "yearly", "tier": tier_id,
        "label": label, "price": _YEARLY_PRICES[tier_id], "features": features,
        "duration_days": 365,
    }

# ─────────────────────────── PAYMENT DESTINATION ───────────────────────────
# Where the user is told to pay. Fill PREMIUM_UPI_ID in .env with your real
# UPI VPA (e.g. "9610087974@ybl") once you know it for a precise "Pay" deep
# link; the phone number alone already works fine for PhonePe/GPay/Paytm's
# "pay by mobile number" flow, which is what's shown by default.
PREMIUM_PAYEE_NAME = os.environ.get("PREMIUM_PAYEE_NAME", "S.N.E.T.C.H").strip()
PREMIUM_PHONE = os.environ.get("PREMIUM_UPI_PHONE", "+919610087974").strip()
PREMIUM_UPI_ID = os.environ.get("PREMIUM_UPI_ID", "").strip()  # optional exact VPA, e.g. "9610087974@ybl"

# Who is allowed to approve/reject payment claims, AND gets every premium
# feature free for life (see get_active_subscription/has_feature below).
# The predefined admin account (snetch258014@gmail.com) is included by
# default even with no .env changes — add more admins via PREMIUM_ADMIN_EMAILS.
ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get(
        "PREMIUM_ADMIN_EMAILS", "snetch258014@gmail.com"
    ).split(",") if e.strip()
}

ALL_FEATURES = ["music_download", "video_download", "astro_insights", "media_download"]

# Where the "new payment claim" notification email is sent, with one-click
# Approve / Not Approve buttons. Defaults to the address you gave.
ADMIN_NOTIFY_EMAIL = os.environ.get("PREMIUM_ADMIN_NOTIFY_EMAIL", "snetch258014@gmail.com").strip()

# Same secret the rest of the app already signs JWTs with (app.py's
# app.secret_key / passwordsave.py's JWT_SECRET both come from this same
# .env value) — reused here so the Approve/Not Approve email links can't
# be forged by anyone who doesn't have your server's SECRET_KEY.
JWT_SECRET = os.environ.get("SECRET_KEY", "dev-secret-change-this-please")
JWT_ALGO = "HS256"
ACTION_TOKEN_TTL_DAYS = 14


def is_admin(email: str) -> bool:
    return bool(email) and email.strip().lower() in ADMIN_EMAILS


def _make_action_token(claim_id: int, action: str) -> str:
    """A signed, single-purpose link token — clicking it from the email
    is itself the authorization (no login needed on the admin's phone).
    It only ever grants ONE thing: 'approve claim #7' or 'reject claim
    #7', and stops working once that claim is no longer pending (see
    verify_action_token / the approve_claim & reject_claim guards)."""
    payload = {
        "claim_id": claim_id,
        "action": action,  # "approve" | "reject"
        "purpose": "premium_claim_action",
        "exp": int(time.time()) + ACTION_TOKEN_TTL_DAYS * 86400,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def verify_action_token(token: str):
    """Returns {"claim_id": int, "action": "approve"|"reject"} or raises
    ValueError with a human-readable reason."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise ValueError("This link has expired.")
    except Exception:
        raise ValueError("This link is invalid.")
    if payload.get("purpose") != "premium_claim_action" or payload.get("action") not in ("approve", "reject"):
        raise ValueError("This link is invalid.")
    return {"claim_id": int(payload["claim_id"]), "action": payload["action"]}


def notify_admin_new_claim(claim: dict, base_url: str) -> None:
    """Emails ADMIN_NOTIFY_EMAIL the moment a user clicks 'I've Paid' —
    with the plan/amount/UTR they entered, plus one-click Approve and
    Not Approve buttons. Clicking a button hits /api/premium/admin/action
    directly (no login required — the link itself is the authorization)
    and immediately activates or rejects that one claim.

    NOTE: this only tells you a claim WAS SUBMITTED — it is not proof the
    money actually arrived. Please check your bank/UPI app yourself
    before pressing Approve, exactly as discussed."""
    base_url = base_url.rstrip("/")
    approve_url = f"{base_url}/api/premium/admin/action?token={_make_action_token(claim['claim_id'], 'approve')}"
    reject_url = f"{base_url}/api/premium/admin/action?token={_make_action_token(claim['claim_id'], 'reject')}"

    plan = PLANS.get(claim["plan_id"], {})
    html = f"""
    <div style="font-family:Arial,sans-serif; max-width:480px; margin:0 auto; color:#222;">
      <h2 style="color:#6a3ff5;">New S.N.E.T.C.H Premium payment claim</h2>
      <p><b>User:</b> {claim['user_email']}</p>
      <p><b>Plan:</b> {plan.get('label', claim['plan_id'])} ({claim['cycle']})</p>
      <p><b>Amount:</b> ₹{claim['price']}</p>
      <p><b>Paid via:</b> {claim.get('payment_method', 'UPI')}</p>
      <p><b>Transaction ID / UTR:</b> {claim.get('payment_ref', '—')}</p>
      <p style="color:#888; font-size:13px;">Please check your bank/UPI app first to confirm this payment actually
        landed before approving.</p>
      <div style="margin-top:24px;">
        <a href="{approve_url}"
           style="background:#2fbf71; color:#fff; padding:12px 22px; border-radius:8px;
                  text-decoration:none; font-weight:bold; margin-right:12px;">✅ Approve</a>
        <a href="{reject_url}"
           style="background:#e05252; color:#fff; padding:12px 22px; border-radius:8px;
                  text-decoration:none; font-weight:bold;">❌ Not Approve</a>
      </div>
    </div>
    """
    plain = (
        f"New premium payment claim from {claim['user_email']}\n"
        f"Plan: {plan.get('label', claim['plan_id'])} ({claim['cycle']}) — Rs.{claim['price']}\n"
        f"Paid via: {claim.get('payment_method', 'UPI')} | Ref: {claim.get('payment_ref', '-')}\n\n"
        f"Approve: {approve_url}\nNot Approve: {reject_url}\n"
    )
    email_utils.send_html_email(
        ADMIN_NOTIFY_EMAIL,
        f"New Premium claim — {claim['user_email']} — ₹{claim['price']}",
        html,
        plain_fallback=plain,
    )


def get_plans() -> list:
    """Plan catalog for the frontend, monthly first then yearly, cheapest first."""
    ordered = sorted(PLANS.values(), key=lambda p: (p["cycle"] != "monthly", p["price"]))
    return [
        {**p, "feature_labels": [FEATURE_LABELS.get(f, f) for f in p["features"]]}
        for p in ordered
    ]


def get_payment_info() -> dict:
    return {
        "payee_name": PREMIUM_PAYEE_NAME,
        "phone": PREMIUM_PHONE,
        "upi_id": PREMIUM_UPI_ID or None,
        "apps": ["PhonePe", "Google Pay", "Paytm"],
    }


def init_db():
    """Create the premium_subscriptions table (idempotent). Call once at app startup."""
    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS premium_subscriptions (
                id             BIGSERIAL PRIMARY KEY,
                user_email     TEXT NOT NULL,
                plan_id        TEXT NOT NULL,
                cycle          TEXT NOT NULL,
                price          INTEGER NOT NULL,
                features       JSONB NOT NULL DEFAULT '[]'::jsonb,
                payment_method TEXT,
                payment_ref    TEXT,
                status         TEXT NOT NULL DEFAULT 'pending',  -- pending | active | rejected | expired | superseded
                reject_reason  TEXT,
                requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                activated_at   TIMESTAMPTZ,
                expires_at     TIMESTAMPTZ,
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_premium_subs_email
            ON premium_subscriptions (user_email, status);
        """)
        conn.commit()


def _norm(email):
    if not email:
        raise ValueError("premium: user_email is required")
    return email.strip().lower()


def get_active_subscription(user_email: str):
    """Return the user's currently active plan as a dict, or None. A row
    whose expires_at has already passed is lazily flipped to 'expired'
    right here — so the very next feature check after a plan lapses
    already sees it as gone, no cron job required.

    The admin account never needs to buy anything: it always gets a
    synthetic 'lifetime, every feature' plan here, with no database row
    behind it at all."""
    if not user_email:
        return None
    user_email = _norm(user_email)

    if is_admin(user_email):
        return {
            "id": None, "plan_id": "admin_lifetime", "cycle": "lifetime", "price": 0,
            "features": list(ALL_FEATURES), "label": "Admin — Lifetime Access",
            "activated_at": None, "expires_at": None,
        }

    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, plan_id, cycle, price, features, status, activated_at, expires_at
            FROM premium_subscriptions
            WHERE user_email=%s AND status='active'
            ORDER BY activated_at DESC LIMIT 1
        """, (user_email,))
        row = cur.fetchone()
        if not row:
            return None
        sub_id, plan_id, cycle, price, features, status, activated_at, expires_at = row

        if expires_at is not None and expires_at < datetime.datetime.now(datetime.timezone.utc):
            cur.execute(
                "UPDATE premium_subscriptions SET status='expired', updated_at=now() WHERE id=%s",
                (sub_id,),
            )
            conn.commit()
            return None

        plan = PLANS.get(plan_id, {})
        return {
            "id": sub_id, "plan_id": plan_id, "cycle": cycle, "price": price,
            "features": features if isinstance(features, list) else json.loads(features),
            "label": plan.get("label", plan_id),
            "activated_at": activated_at.isoformat() if activated_at else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }


def has_feature(user_email: str, feature: str) -> bool:
    sub = get_active_subscription(user_email)
    return bool(sub and feature in sub.get("features", []))


def submit_payment_claim(user_email: str, plan_id: str, payment_method: str, payment_ref: str) -> dict:
    """User says 'I've paid' — records a pending claim for the admin to
    verify against their actual UPI account and approve."""
    user_email = _norm(user_email)
    plan = PLANS.get(plan_id)
    if not plan:
        raise ValueError("Unknown plan.")
    payment_method = (payment_method or "").strip() or "UPI"
    payment_ref = (payment_ref or "").strip()
    if not payment_ref:
        raise ValueError("Please enter the transaction ID / UTR number from your payment app.")

    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO premium_subscriptions
                (user_email, plan_id, cycle, price, features, payment_method, payment_ref, status)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, 'pending')
            RETURNING id, requested_at;
        """, (user_email, plan_id, plan["cycle"], plan["price"], json.dumps(plan["features"]),
              payment_method, payment_ref))
        claim_id, requested_at = cur.fetchone()
        conn.commit()
    return {
        "claim_id": claim_id, "requested_at": requested_at.isoformat(), "status": "pending",
        "user_email": user_email, "plan_id": plan_id, "cycle": plan["cycle"], "price": plan["price"],
        "payment_method": payment_method, "payment_ref": payment_ref,
    }


def get_history(user_email: str) -> list:
    """Every claim (pending/active/rejected/expired/superseded) this user
    has ever made, newest first — shown on their Premium page."""
    user_email = _norm(user_email)
    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, plan_id, cycle, price, status, payment_method, payment_ref,
                   reject_reason, requested_at, activated_at, expires_at
            FROM premium_subscriptions WHERE user_email=%s
            ORDER BY requested_at DESC
        """, (user_email,))
        cols = ["id", "plan_id", "cycle", "price", "status", "payment_method", "payment_ref",
                "reject_reason", "requested_at", "activated_at", "expires_at"]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            for k in ("requested_at", "activated_at", "expires_at"):
                if d[k] is not None:
                    d[k] = d[k].isoformat()
            plan = PLANS.get(d["plan_id"], {})
            d["label"] = plan.get("label", d["plan_id"])
            out.append(d)
        return out


def list_all_latest_status(emails: list) -> dict:
    """For the admin dashboard: given every registered user's email, look
    up each one's most relevant subscription row (prefer 'active', else
    the most recent of anything) — so the dashboard can show, per user,
    what plan they're on and how much time is left (or that they've never
    bought anything)."""
    if not emails:
        return {}
    normalized = [_norm(e) for e in emails]
    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (user_email) user_email, plan_id, cycle, price, status,
                   payment_method, payment_ref, requested_at, activated_at, expires_at
            FROM premium_subscriptions
            WHERE user_email = ANY(%s)
            ORDER BY user_email,
                     (status = 'active') DESC,
                     requested_at DESC
        """, (normalized,))
        cols = ["user_email", "plan_id", "cycle", "price", "status", "payment_method",
                "payment_ref", "requested_at", "activated_at", "expires_at"]
        result = {}
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            for k in ("requested_at", "activated_at", "expires_at"):
                if d[k] is not None:
                    d[k] = d[k].isoformat()
            plan = PLANS.get(d["plan_id"], {})
            d["label"] = plan.get("label", d["plan_id"])
            # A previously-active row whose expiry has quietly passed
            # (no one has hit get_active_subscription for that user since)
            # should read as expired here too, not stale 'active'.
            if d["status"] == "active" and d["expires_at"] and d["expires_at"] < datetime.datetime.now(datetime.timezone.utc).isoformat():
                d["status"] = "expired"
            result[d["user_email"]] = d
        return result


# ─────────────────────────── ADMIN ───────────────────────────

def list_pending_claims() -> list:
    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, user_email, plan_id, cycle, price, payment_method, payment_ref, requested_at
            FROM premium_subscriptions WHERE status='pending'
            ORDER BY requested_at ASC
        """)
        cols = ["id", "user_email", "plan_id", "cycle", "price", "payment_method", "payment_ref", "requested_at"]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["requested_at"] = d["requested_at"].isoformat()
            plan = PLANS.get(d["plan_id"], {})
            d["label"] = plan.get("label", d["plan_id"])
            out.append(d)
        return out


def get_claim(claim_id: int):
    """Fetch one claim's current state — used by the email-link action
    route to show a proper 'already handled' page instead of erroring."""
    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, user_email, plan_id, cycle, price, status FROM premium_subscriptions WHERE id=%s",
            (claim_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cid, user_email, plan_id, cycle, price, status = row
        plan = PLANS.get(plan_id, {})
        return {
            "id": cid, "user_email": user_email, "plan_id": plan_id, "cycle": cycle,
            "price": price, "status": status, "label": plan.get("label", plan_id),
        }


def approve_claim(claim_id: int) -> dict:
    """Confirms a payment really landed (you checked your UPI app) and
    activates the plan. Any other still-active plan the same user has is
    superseded — only one active plan per user at a time."""
    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_email, plan_id, cycle FROM premium_subscriptions WHERE id=%s AND status='pending'", (claim_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Claim not found or already handled.")
        user_email, plan_id, cycle = row
        plan = PLANS.get(plan_id)
        if not plan:
            raise ValueError("Unknown plan on this claim.")

        cur.execute(
            "UPDATE premium_subscriptions SET status='superseded', updated_at=now() "
            "WHERE user_email=%s AND status='active'",
            (user_email,),
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = now + datetime.timedelta(days=plan["duration_days"])
        cur.execute("""
            UPDATE premium_subscriptions
            SET status='active', activated_at=%s, expires_at=%s, updated_at=now()
            WHERE id=%s
            RETURNING id;
        """, (now, expires_at, claim_id))
        conn.commit()
    return {"claim_id": claim_id, "user_email": user_email, "activated_at": now.isoformat(), "expires_at": expires_at.isoformat()}


def reject_claim(claim_id: int, reason: str = "") -> None:
    with pg.db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE premium_subscriptions SET status='rejected', reject_reason=%s, updated_at=now() "
            "WHERE id=%s AND status='pending'",
            (reason or "Payment could not be verified.", claim_id),
        )
        if cur.rowcount == 0:
            raise ValueError("Claim not found or already handled.")
        conn.commit()
