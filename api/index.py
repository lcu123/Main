"""Vercel serverless entry point — imports the FastAPI app from the connector."""
import sys
import os

# Make the connector package importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "quo_voip_connector"))

from server.remote_mcp import app  # noqa: F401  (Vercel picks up `app`)
