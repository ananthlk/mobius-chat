"""Run: python -m app.db.seed to seed the RAG DB with sample document + chunk + embedding."""
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from app.db.seed import main

if __name__ == "__main__":
    main()
