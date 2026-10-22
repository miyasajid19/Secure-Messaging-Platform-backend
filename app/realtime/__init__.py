"""Realtime package: WebSocket endpoint + in-memory connection manager.

The `connection_manager` is a module-level singleton so every router
that imports it gets the same instance. Per-process state is fine
because the project runs as a single uvicorn worker.
"""

from app.realtime.manager import connection_manager

__all__ = ["connection_manager"]
