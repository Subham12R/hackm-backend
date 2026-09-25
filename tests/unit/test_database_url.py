from src.db.session import create_db_engine


def test_database_engine_uses_installed_psycopg_driver_for_neon_urls() -> None:
    engine = create_db_engine("postgresql://user:password@example.com/database")

    assert engine.url.drivername == "postgresql+psycopg"
