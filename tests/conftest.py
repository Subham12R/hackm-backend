import os

# Tests drive delivery explicitly through request background tasks.
os.environ.setdefault("EMAIL_OUTBOX_WORKER", "0")
os.environ.setdefault("UNPAID_REGISTRATION_WORKER", "0")
os.environ.setdefault("CREDENTIAL_RENDER_WORKER", "0")
# Most tests check business rules at the plain tier prices; fee pass-through has its own tests.
os.environ.setdefault("RAZORPAY_FEE_PERCENT", "0")


# RECORD_OPENAPI_EXAMPLES=1 records every TestClient exchange into src/openapi_examples.json, which feeds the
# response schemas and examples in the OpenAPI document. Run it through `scripts/export_openapi.py --record`.
if os.getenv("RECORD_OPENAPI_EXAMPLES") == "1":
    from starlette import testclient as _testclient

    from src.openapi import Recorder

    _recorder = Recorder()
    _handle_request = _testclient._TestClientTransport.handle_request

    def _recording_handle_request(self, request):
        response = _handle_request(self, request)
        body = response.read()
        try:
            _recorder.observe(
                self.app, request.method, request.url.path, request.content, response.status_code,
                response.headers.get("content-type", ""), body,
            )
        except Exception:  # recording must never fail a test
            pass
        return response

    _testclient._TestClientTransport.handle_request = _recording_handle_request

    def pytest_sessionfinish(session, exitstatus):
        _recorder.dump()
