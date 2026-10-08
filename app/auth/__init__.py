"""Auth package: mocked-OTP login + JWT + protected routes.

The router is mounted from `app.main` via `from app.auth.router import
router`. We deliberately don't re-export it here — re-exporting the
submodule would shadow the APIRouter instance with a module reference,
and `app.include_router` would then blow up at startup.
"""
