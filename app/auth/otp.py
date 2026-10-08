"""Mocked OTP for Phase 2.

The OTP is a hard-coded constant — there is no DB lookup, no email, no
SMS. A real implementation would issue a per-request code with an expiry
and rate-limit guesses. The frontend autofills `123456` in dev via the
`debug_otp` field returned from `/auth/request-otp`.
"""

MOCK_OTP: str = "123456"
