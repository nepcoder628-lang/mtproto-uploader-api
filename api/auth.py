"""
Auth API
========
Multi-step session string generation via Pyrogram.

Flow:
    1. POST /auth/send-code   { api_id, api_hash, phone }
       → Sends OTP to the phone number via Telegram
       → Returns { phone_code_hash }  (pass this to step 2)

    2. POST /auth/verify      { api_id, api_hash, phone, phone_code_hash, code }
       → Verifies the OTP
       → Returns { session_string }   (save this as SESSION_STRING env var)

    Optional:
    POST /auth/verify with two_factor_password if account has 2FA enabled.

The session_string can then be used as the SESSION_STRING environment
variable so the uploader API never needs to re-authenticate.
"""

import asyncio
import logging
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pyrogram import Client
from pyrogram.errors import (
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
    PhoneNumberInvalid,
    PhoneNumberBanned,
    ApiIdInvalid,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# In-memory store: phone -> Client (waiting for code verification)
# On Render free tier the process stays alive, so this works fine.
_pending: Dict[str, Client] = {}


# ── Request models ─────────────────────────────────────────────────────────

class SendCodeRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str          # e.g. "+977981234567"


class VerifyRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str
    phone_code_hash: str
    code: str           # OTP received on Telegram/SMS
    two_factor_password: Optional[str] = None  # Only if 2FA is enabled


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/send-code")
async def send_code(req: SendCodeRequest):
    """
    Step 1: Send OTP to the phone number.

    Example:
        curl -X POST https://your-api.onrender.com/auth/send-code \\
          -H "Content-Type: application/json" \\
          -d '{"api_id": 12345678, "api_hash": "abc...", "phone": "+977981234567"}'

    Response:
        { "phone_code_hash": "...", "message": "OTP sent to +977..." }

    Pass phone_code_hash to /auth/verify along with the OTP you receive.
    """
    phone = req.phone.strip()

    # Clean up any previous pending session for this phone
    if phone in _pending:
        try:
            await _pending[phone].disconnect()
        except Exception:
            pass
        del _pending[phone]

    client = Client(
        name=":memory:",
        api_id=req.api_id,
        api_hash=req.api_hash,
        in_memory=True,
    )

    try:
        await client.connect()
    except ApiIdInvalid:
        raise HTTPException(status_code=400, detail="Invalid api_id or api_hash")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection failed: {e}")

    try:
        sent = await client.send_code(phone)
    except PhoneNumberInvalid:
        await client.disconnect()
        raise HTTPException(status_code=400, detail="Invalid phone number format (use +countrycode...)")
    except PhoneNumberBanned:
        await client.disconnect()
        raise HTTPException(status_code=403, detail="This phone number is banned on Telegram")
    except Exception as e:
        await client.disconnect()
        raise HTTPException(status_code=500, detail=f"Failed to send code: {e}")

    # Keep the connected client alive for the verify step
    _pending[phone] = client

    return {
        "phone_code_hash": sent.phone_code_hash,
        "message": f"OTP sent to {phone} via Telegram. Use /auth/verify to complete.",
    }


@router.post("/verify")
async def verify(req: VerifyRequest):
    """
    Step 2: Submit the OTP to get your session string.

    Example:
        curl -X POST https://your-api.onrender.com/auth/verify \\
          -H "Content-Type: application/json" \\
          -d '{
            "api_id": 12345678,
            "api_hash": "abc...",
            "phone": "+977981234567",
            "phone_code_hash": "abc123...",
            "code": "12345"
          }'

    Response:
        { "session_string": "BQA...", "message": "Save this as SESSION_STRING env var" }

    If your account has 2FA, also include:
        "two_factor_password": "your2FApassword"
    """
    phone = req.phone.strip()

    client = _pending.get(phone)
    if client is None:
        raise HTTPException(
            status_code=400,
            detail="No pending auth for this phone. Call /auth/send-code first."
        )

    try:
        await client.sign_in(
            phone_number=phone,
            phone_code_hash=req.phone_code_hash,
            phone_code=req.code.strip(),
        )
    except PhoneCodeInvalid:
        raise HTTPException(status_code=400, detail="Incorrect OTP code")
    except PhoneCodeExpired:
        # Clean up so user can restart
        await client.disconnect()
        del _pending[phone]
        raise HTTPException(status_code=400, detail="OTP expired. Call /auth/send-code again.")
    except SessionPasswordNeeded:
        if not req.two_factor_password:
            raise HTTPException(
                status_code=400,
                detail="2FA is enabled on this account. Retry with two_factor_password field set."
            )
        try:
            await client.check_password(req.two_factor_password)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"2FA password incorrect: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sign-in failed: {e}")

    try:
        session_string = await client.export_session_string()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export session: {e}")
    finally:
        await client.disconnect()
        _pending.pop(phone, None)

    return {
        "session_string": session_string,
        "message": (
            "Session generated successfully. "
            "Set this as the SESSION_STRING environment variable in your Render service. "
            "Do NOT share this string — it grants full access to your Telegram account."
        ),
    }


@router.get("/status")
async def status():
    """Check how many auth sessions are currently pending."""
    return {"pending_sessions": list(_pending.keys())}
