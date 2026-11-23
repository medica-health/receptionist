import os

# Must be set before any receptionist module reads settings.
os.environ["APP_ENV"] = "test"
os.environ.setdefault("PUBLIC_BASE_URL", "https://receptionist.test")
os.environ.setdefault("JWT_SECRET", "x" * 32)
