# init_db.py (ROOT)

from agencyvault_app.database import engine
from agencyvault_app.models import Base  # noqa: F401


def main():
    Base.metadata.create_all(bind=engine)
    print("Database tables created or verified.")


if __name__ == "__main__":
    main()
