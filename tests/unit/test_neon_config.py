from src.main import create_app


def test_app_uses_neon_database_url_when_database_url_is_not_set(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://user:password@example.com/database")

    app = create_app()

    assert app.state.database_url == "postgresql://user:password@example.com/database"
