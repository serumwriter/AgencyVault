# init_db.py (ROOT)
from database import engine
from models import Base  # noqa: F401

def main():
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created/verified.")

if __name__ == "__main__":
    main()
