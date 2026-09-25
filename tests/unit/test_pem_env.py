from src.config import decode_pem_env


def test_pem_env_converts_escaped_newlines() -> None:
    decoded = decode_pem_env("-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----")

    assert decoded == "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
