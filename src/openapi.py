"""OpenAPI document for the HackMatrix API.

FastAPI already derives paths, parameters and request bodies from the route signatures. Most handlers return
plain dicts, so on their own the responses would be documented as "any JSON". This module adds what the
signatures cannot say:

* response schemas and examples recorded from the integration tests (``src/openapi_examples.json``, refreshed
  with ``scripts/export_openapi.py --record``),
* bearer auth in place of the raw ``authorization`` header parameter, plus the roles each route accepts, read
  from the role constant the handler passes to ``authenticated_user``,
* the two error shapes and a description for every tag.

``/openapi.json``, ``/docs`` and ``/redoc`` serve this document, and ``docs/api/openapi.json`` is a committed
copy of it that a unit test keeps current.
"""

from __future__ import annotations

import inspect
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from src.auth import policies

EXAMPLES_PATH = Path(__file__).with_name("openapi_examples.json")
API_VERSION = "1.0.0"
# Only the committed copy lists servers: a served /docs page should "Try it out" against itself.
SERVERS = [
    {"url": "https://api.hackmatrix.codes", "description": "Production"},
    {"url": "http://127.0.0.1:8000", "description": "Local uvicorn"},
]

DESCRIPTION = """\
REST API behind the HackMatrix participant site and the ORG. admin panel.

* Guide (auth flow, errors, frontend integration, troubleshooting): `docs/api/README.md` in the
  `hackm-backend` repository.
* Every route lives under `/api/v1`. Bodies are JSON unless a route says otherwise.
* Signed-in routes take `Authorization: Bearer <access_token>`. Access tokens last 15 minutes; refresh them with
  `POST /api/v1/auth/refresh`.
* Send `X-Request-ID` to trace a call. The API echoes it (or a fresh one) on every response and in error bodies.
"""

TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "Liveness and database reachability."},
    {"name": "public", "description": "No sign-in. Event schedule, leaderboard, gallery, site notices, Instagram feed, certificate verification and the contact form."},
    {"name": "registrations", "description": "Team registration, RSVP, team invites and the registration's payment status. No sign-in."},
    {"name": "payments", "description": "Razorpay checkout confirmation and the Razorpay webhook."},
    {"name": "auth", "description": "Login, token refresh, logout, account activation, password reset and change, staff invites."},
    {"name": "participant", "description": "The signed-in team leader's dashboard: roster, events, submissions, results, certificates and live notifications."},
    {"name": "judge", "description": "A judge's assigned projects and scorecards. Team identity is never exposed."},
    {"name": "admin", "description": "Organiser read models: overview, registrations, teams, submissions, evaluations and audit logs."},
    {"name": "admin-payments", "description": "Registration detail, payment review, refunds, cancellation and roster edits by staff."},
    {"name": "admin-event", "description": "Event settings, the legacy single challenge, and announcements."},
    {"name": "admin-stages", "description": "Events inside the edition (\"stages\"), problem statements (\"tracks\"), submission forms, extra time and awards."},
    {"name": "admin-judging", "description": "Judging rounds, rubrics, judge assignments, scorecards and results."},
    {"name": "admin-checkin", "description": "Finals acceptance and venue check-ins."},
    {"name": "admin-credentials", "description": "Certificates, badges and their templates."},
    {"name": "admin-ops", "description": "Contact inbox, staff management and analytics."},
    {"name": "admin-gallery", "description": "Photo gallery uploads (presigned to S3 or R2)."},
    {"name": "admin-team-invites", "description": "Invite links that let a team register without paying."},
    {"name": "admin-site-notices", "description": "Banners shown on the public site."},
    {"name": "admin-email-campaigns", "description": "Staff emails to teams, members and staff."},
    {"name": "admin-exports", "description": "CSV exports built in the background."},
    {"name": "admin-owner", "description": "Owner-only data flush, guarded by an emailed code."},
]

# Routes that have no tag in code get one here, so every route sits in a documented group.
UNTAGGED: dict[str, str] = {"/healthz": "health", "/api/v1/payments/": "payments", "/api/v1/registrations": "registrations"}

API_ERROR = {
    "type": "object",
    "description": "Raised by `ApiError`. `code` is a stable machine-readable string.",
    "properties": {
        "error": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "request_id": {"type": "string"}},
            "required": ["code", "request_id"],
        }
    },
    "required": ["error"],
}
HTTP_ERROR = {
    "type": "object",
    "description": "Raised by `HTTPException`. `detail` is a short human-readable reason, stable enough to branch on.",
    "properties": {"detail": {"type": "string"}},
    "required": ["detail"],
}


def install(app: FastAPI) -> None:
    """Serve the enriched document from ``/openapi.json`` (and therefore ``/docs`` and ``/redoc``)."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build(app)
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def build(app: FastAPI, examples: dict[str, Any] | None = None) -> dict[str, Any]:
    examples = load_examples() if examples is None else examples
    spec = get_openapi(title="HackMatrix API", version=API_VERSION, description=DESCRIPTION, routes=app.routes)
    spec["tags"] = TAGS
    components = spec.setdefault("components", {})
    components.setdefault("schemas", {}).update({"ApiError": API_ERROR, "HttpError": HTTP_ERROR})
    components["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT (EdDSA)",
            "description": "Access token from `/api/v1/auth/login`, `/refresh` or `/activate`. Expires after 15 minutes.",
        }
    }
    roles = _route_roles(app)
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            _tag(path, operation)
            secured = _bearer(operation)
            allowed = roles.get((method.upper(), path))
            if secured:
                operation["security"] = [{"bearerAuth": []}]
                operation["x-roles"] = allowed or []
                note = f"**Roles:** {', '.join(f'`{role}`' for role in allowed)}" if allowed else "**Auth:** bearer token"
                operation["description"] = f"{note}\n\n{operation.get('description', '')}".strip()
            else:
                operation["security"] = []
            _responses(method.upper(), path, operation, examples, secured, components["schemas"])
    return spec


def load_examples() -> dict[str, Any]:
    try:
        return json.loads(EXAMPLES_PATH.read_text())
    except FileNotFoundError:
        return {}


def _tag(path: str, operation: dict[str, Any]) -> None:
    if operation.get("tags"):
        return
    for prefix, tag in UNTAGGED.items():
        if path.startswith(prefix):
            operation["tags"] = [tag]
            return


def _bearer(operation: dict[str, Any]) -> bool:
    parameters = operation.get("parameters", [])
    kept = [p for p in parameters if not (p.get("in") == "header" and p.get("name") == "authorization")]
    if len(kept) == len(parameters):
        return False
    if kept:
        operation["parameters"] = kept
    else:
        operation.pop("parameters", None)
    return True


def _responses(method: str, path: str, operation: dict[str, Any], examples: dict[str, Any], secured: bool,
               spec_components: dict[str, Any]) -> None:
    responses = operation.setdefault("responses", {})
    recorded = examples.get(f"{method} {path}", {})
    request = recorded.get("request")
    body = operation.get("requestBody", {}).get("content", {}).get("application/json")
    if request is not None and body is not None and _fits(request, body.get("schema", {}), spec_components):
        body.setdefault("example", request)
    for status, sample in sorted(recorded.get("responses", {}).items()):
        entry = responses.setdefault(status, {"description": _reason(int(status))})
        if sample.get("example") is None and not sample.get("schema"):
            continue
        content = entry.setdefault("content", {}).setdefault(sample.get("media_type", "application/json"), {})
        example = sample.get("example")
        if isinstance(example, dict) and isinstance(example.get("error"), dict):
            content["schema"] = {"$ref": "#/components/schemas/ApiError"}
        elif isinstance(example, dict) and isinstance(example.get("detail"), str):
            content["schema"] = {"$ref": "#/components/schemas/HttpError"}
        elif not content.get("schema") and sample.get("schema"):
            content["schema"] = sample["schema"]
        if sample.get("example") is not None:
            content["example"] = sample["example"]
    if secured:
        responses.setdefault("401", _error_ref("Missing, expired or invalid access token (`invalid access token`).", "HttpError"))
        if operation.get("x-roles") and set(operation["x-roles"]) != {"PARTICIPANT"}:
            responses.setdefault("403", _error_ref("Signed in, but the role is not allowed (`admin access required`).", "HttpError"))
    if "422" in responses:
        # FastAPI's field errors are a list; business rules answer 422 with a plain string.
        responses["422"]["description"] = "Validation failed: FastAPI's `detail` list for bad fields, or a `detail` string for a business rule."
        content = responses["422"].setdefault("content", {}).setdefault("application/json", {})
        shapes = [{"$ref": "#/components/schemas/HttpError"}]
        if "HTTPValidationError" in spec_components:
            shapes.insert(0, {"$ref": "#/components/schemas/HTTPValidationError"})
        content["schema"] = {"anyOf": shapes} if len(shapes) > 1 else shapes[0]


def _fits(example: Any, schema: dict[str, Any], schemas: dict[str, Any]) -> bool:
    """False when a top-level field of a recorded request uses a value the schema's enum no longer allows."""
    schema = _resolve(schema, schemas)
    if not isinstance(example, dict):
        return True
    for name, value in example.items():
        options = [_resolve(option, schemas) for option in _options(_resolve(schema.get("properties", {}).get(name, {}), schemas))]
        allowed = [option["enum"] for option in options if "enum" in option]
        if allowed and value is not None and not any(value in enum for enum in allowed):
            return False
    return True


def _resolve(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref", "")
    return schemas.get(ref.rsplit("/", 1)[-1], {}) if ref else schema


def _options(schema: dict[str, Any]) -> list[dict[str, Any]]:
    return schema.get("anyOf") or schema.get("oneOf") or [schema]


def _error_ref(description: str, schema: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}}}


def _reason(status: int) -> str:
    return {
        200: "OK", 201: "Created", 202: "Accepted", 204: "No content", 400: "Bad request", 401: "Unauthorized",
        403: "Forbidden", 404: "Not found", 409: "Conflict", 410: "Gone", 413: "Too large", 422: "Validation failed",
        423: "Locked", 429: "Too many requests", 502: "Upstream failed", 503: "Unavailable",
    }.get(status, "Response")


def _roles_of(function: Any, depth: int = 0) -> set[str]:
    """Role constants named in a handler, or failing that in the auth helper it calls (``admin_user``, ``staff``...)."""
    try:
        source = inspect.getsource(function)
        names = inspect.getclosurevars(function)
    except (OSError, TypeError, ValueError):
        return set()
    scope = {**names.globals, **names.nonlocals}
    roles: set[str] = set()
    # Only lines that hand the bearer header on: other RoleCode mentions are business rules, not access rules.
    # Helpers it calls are small auth checks, so they are read whole.
    access = source if depth else "\n".join(line for line in source.splitlines() if "authorization" in line)
    for constant, single in _ROLE_CONSTANT.findall(access):
        value = scope.get(constant, getattr(policies, constant, None)) if constant else None
        if isinstance(value, (set, frozenset)):
            roles |= {str(role) for role in value}
        elif single:
            roles.add(single)
    if roles or depth >= 2:
        return roles
    for name, value in names.nonlocals.items():
        if inspect.isfunction(value) and re.search(rf"\b{name}\(", source):
            roles |= _roles_of(value, depth + 1)
    return roles


def api_routes(routes: list[Any]) -> list[APIRoute]:
    """Every APIRoute, including those behind FastAPI's lazily included routers."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif getattr(route, "original_router", None) is not None:
            found += api_routes(route.original_router.routes)
    return found


_ROLE_CONSTANT = re.compile(r"\b([A-Z_]+_ROLES)\b|RoleCode\.([A-Z_]+)")


def _route_roles(app: FastAPI) -> dict[tuple[str, str], list[str]]:
    """Roles each route accepts, read from the policy constant its handler hands to ``authenticated_user``."""
    found: dict[tuple[str, str], list[str]] = {}
    for route in api_routes(app.routes):
        roles = _roles_of(route.endpoint)
        if not roles and "participant" in route.tags:
            roles = {"PARTICIPANT"}
        if not roles and "judge" in route.tags:
            roles = {"JUDGE"}
        if "admin-owner" in route.tags:
            roles = {"SUPER_ADMIN"}
        order = list(policies.RoleCode)
        for method in route.methods:
            found[(method, route.path_format)] = sorted(roles, key=lambda role: order.index(policies.RoleCode(role)))
    return found


# ---------------------------------------------------------------------------------------------------------
# Recording: the test suite feeds every TestClient exchange through ``Recorder.observe`` when
# RECORD_OPENAPI_EXAMPLES is set (see tests/conftest.py).

_SECRET_KEYS = re.compile(r"(token|secret|password|signature|otp)$", re.IGNORECASE)
_JWT = re.compile(r"^eyJ[\w-]+\.[\w-]+\.[\w-]+$")
_TOKEN_QUERY = re.compile(r"([?&](?:token|code|signature|X-Amz-[A-Za-z]+)=)[^&\s\"]+")


class Recorder:
    def __init__(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}

    def observe(self, app: FastAPI, method: str, path: str, request_body: bytes, status: int, media_type: str, body: bytes) -> None:
        template = _template(app, method, path)
        if template is None:
            return
        entry = self.calls.setdefault(f"{method} {template}", {"responses": {}})
        if request_body and 200 <= status < 300:
            try:
                sent = json.loads(request_body)
            except ValueError:
                sent = None
            if sent is not None and _richness(sent) >= _richness(entry.get("request")):
                entry["request"] = scrub(sent)
        sample = entry["responses"].setdefault(str(status), {"media_type": media_type or "application/json"})
        if not media_type.startswith("application/json"):
            return
        try:
            value = json.loads(body) if body else None
        except ValueError:
            return
        if value is None:
            return
        sample["schema"] = merge(sample.get("schema"), infer(value))
        if _richness(value) > _richness(sample.get("example")):
            sample["example"] = trim(scrub(value))

    def dump(self, path: Path = EXAMPLES_PATH) -> None:
        path.write_text(json.dumps(dict(sorted(self.calls.items())), indent=1, sort_keys=True) + "\n")


def _template(app: FastAPI, method: str, path: str) -> str | None:
    from starlette.routing import Match

    scope = {"type": "http", "path": path, "method": method, "root_path": ""}
    for route in api_routes(app.routes):
        if route.matches(scope)[0] == Match.FULL:
            return route.path_format
    return None


def scrub(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str):
        if key and _SECRET_KEYS.search(key) and value:
            return f"<{key}>"
        if _JWT.match(value):
            return "<jwt>"
        return _TOKEN_QUERY.sub(r"\1<redacted>", value)
    return value


def trim(value: Any, depth: int = 0) -> Any:
    """Keep examples readable: two items per list, long strings cut."""
    if isinstance(value, dict):
        return {k: trim(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [trim(v, depth + 1) for v in value[:2]]
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "…"
    return value


def _richness(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(1 + _richness(v) for v in value.values())
    if isinstance(value, list):
        return 1 + sum(_richness(v) for v in value[:3])
    return 0 if value is None else 1


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def infer(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        if _UUID.match(value):
            return {"type": "string", "format": "uuid"}
        if _DATETIME.match(value):
            return {"type": "string", "format": "date-time"}
        if _DATE.match(value):
            return {"type": "string", "format": "date"}
        return {"type": "string"}
    if isinstance(value, list):
        items: dict[str, Any] | None = None
        for item in value:
            items = merge(items, infer(item))
        return {"type": "array", "items": items or {}}
    return {"type": "object", "properties": {k: infer(v) for k, v in value.items()}}


def merge(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """Union of two inferred schemas: object properties are combined, a seen ``null`` makes a field nullable."""
    if not left:
        return deepcopy(right or {})
    if not right:
        return deepcopy(left)
    left_types, right_types = _types(left), _types(right)
    types = sorted(left_types | right_types, key=lambda t: (t == "null", t))
    merged: dict[str, Any] = {}
    if "object" in types:
        properties = dict(left.get("properties", {}))
        for name, schema in right.get("properties", {}).items():
            properties[name] = merge(properties.get(name), schema)
        merged["properties"] = properties
    if "array" in types:
        merged["items"] = merge(left.get("items"), right.get("items"))
    if "integer" in types and "number" in types:
        types.remove("integer")
    formats = {s.get("format") for s in (left, right) if "string" in _types(s)}
    if len(formats) == 1 and None not in formats:
        merged["format"] = formats.pop()
    merged["type"] = types[0] if len(types) == 1 else types
    return dict(sorted(merged.items()))


def _types(schema: dict[str, Any]) -> set[str]:
    kind = schema.get("type")
    if kind is None:
        return set()
    return set(kind) if isinstance(kind, list) else {kind}
