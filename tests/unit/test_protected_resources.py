import pytest
from fastapi import HTTPException

from src.events import resources

ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize(("url", "expected"), [
    (f"https://docs.google.com/document/d/{ID}/edit?usp=sharing", f"https://docs.google.com/document/d/{ID}/export?format=pdf"),
    (f"https://docs.google.com/document/u/1/d/{ID}/view", f"https://docs.google.com/document/d/{ID}/export?format=pdf"),
    (f"https://docs.google.com/spreadsheets/d/{ID}/edit#gid=0", f"https://docs.google.com/spreadsheets/d/{ID}/export?format=pdf"),
    (f"https://docs.google.com/presentation/d/{ID}/edit", f"https://docs.google.com/presentation/d/{ID}/export/pdf"),
    (f"https://drive.google.com/file/d/{ID}/view?usp=drive_link", f"https://drive.google.com/uc?export=download&id={ID}"),
    (f"https://drive.google.com/open?id={ID}", f"https://drive.google.com/uc?export=download&id={ID}"),
    ("https://docs.google.com/document/d/e/2PACX-1vT/pub", None),
    (f"https://evil.example/document/d/{ID}/edit", None),
    (f"https://docs.google.com.evil.example/document/d/{ID}/edit", None),
    ("https://drive.google.com/drive/folders/abc", None),
    (f"ftp://docs.google.com/document/d/{ID}", None),
])
def test_google_links_map_to_their_pdf_export(url, expected) -> None:
    assert resources.pdf_url(url) == expected


def test_private_files_are_refused(monkeypatch) -> None:
    class Page:
        status_code = 200

        def iter_bytes(self):
            yield b"<html>Sign in</html>"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def stream(self, *_):
            return Page()

    monkeypatch.setattr(resources.httpx, "Client", Client)
    resources.clear_cache()
    with pytest.raises(HTTPException) as error:
        resources.fetch_pdf(f"https://docs.google.com/document/d/{ID}/edit")
    assert error.value.status_code == 502 and "Anyone with the link" in error.value.detail
