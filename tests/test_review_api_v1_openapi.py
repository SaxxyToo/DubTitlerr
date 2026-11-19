"""The OpenAPI file and the route table cannot drift apart.

Adding or removing a /api/v1 route without editing dubtitlerr/review/openapi.json makes
test_the_spec_and_the_route_table_agree red; changing a route's role without editing the
operation's x-dubtitlerr-role makes test_each_operation_documents_its_registered_role red.
"""

import json

import pytest

from dubtitlerr.review import review_api_v1, review_auth
from dubtitlerr.review import review_server as rs

KEY = "openapi-key"
HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


@pytest.fixture(autouse=True)
def _token(tmp_path, monkeypatch):
    """The served-document test must never mint a token into a real /config."""
    monkeypatch.setenv("REVIEW_TOKEN", KEY)
    monkeypatch.delenv("REVIEW_AUTH", raising=False)
    monkeypatch.setattr(rs, "TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(rs, "_GENERATED", None)


def spec():
    return json.loads(review_api_v1.OPENAPI_PATH.read_text(encoding="utf-8"))


def operations(document):
    """{(METHOD, /api/v1/path with {name} -> *): operation} for every operation in the spec."""
    out = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            out[(method.upper(), path.replace("{name}", "*"))] = operation
    return out


def registered_v1():
    return {(method, path): role for (method, path), role in review_auth.ROUTE_ROLES.items() if path.startswith("/api/v1")}


def test_the_file_is_a_valid_openapi_document():
    document = spec()
    assert document["openapi"].startswith("3.")
    assert document["info"]["title"] and document["info"]["version"]
    assert document["components"]["securitySchemes"]["apiKey"]["name"] == review_auth.API_KEY_HEADER
    assert document["components"]["securitySchemes"]["apiKey"]["in"] == "header"


def test_the_spec_and_the_route_table_agree():
    assert set(operations(spec())) == set(registered_v1())


def test_the_guard_would_notice_a_route_removed_from_the_spec():
    """The comparison above is only worth having if a missing entry actually shows up."""
    document = spec()
    document["paths"].pop("/api/v1/library")
    assert set(operations(document)) - set(registered_v1()) == set()
    assert set(registered_v1()) - set(operations(document)) == {("GET", "/api/v1/library")}


def test_the_guard_would_notice_a_route_added_without_the_spec():
    document = spec()
    document["paths"]["/api/v1/unplanned"] = {"get": {"responses": {"200": {"description": "x"}}}}
    assert set(operations(document)) - set(registered_v1()) == {("GET", "/api/v1/unplanned")}


def test_each_operation_documents_its_registered_role():
    table = registered_v1()
    for route, operation in operations(spec()).items():
        assert operation.get("x-dubtitlerr-role") == table[route], route


def test_each_operation_documents_a_summary_and_responses():
    for route, operation in operations(spec()).items():
        assert operation.get("summary"), route
        assert operation.get("operationId"), route
        assert operation["responses"], route
        assert all(str(code).isdigit() for code in operation["responses"]), route


def test_every_path_is_under_the_versioned_prefix():
    assert all(path.startswith("/api/v1/") for path in spec()["paths"])


def test_the_documented_roles_match_the_written_rule_of_user_reads_and_admin_writes():
    table = registered_v1()
    reads = {route for route, role in table.items() if role == "user"}
    writes = {route for route, role in table.items() if role == "admin"}
    assert all(method == "GET" for method, _path in reads)
    assert all(method != "GET" for method, _path in writes)
    assert writes  # the surface actually has mutations to protect


def test_history_paging_matches_the_modules_bounds():
    operation = spec()["paths"]["/api/v1/history"]["get"]
    params = {p["name"]: p["schema"] for p in operation["parameters"]}
    assert params["limit"]["minimum"] == 1
    assert params["limit"]["maximum"] == review_api_v1.MAX_HISTORY_LIMIT
    assert params["limit"]["default"] == review_api_v1.DEFAULT_HISTORY_LIMIT
    assert params["offset"]["minimum"] == 0
    assert params["offset"]["default"] == 0


def test_a_path_parameter_is_declared_where_the_route_has_one():
    operation = spec()["paths"]["/api/v1/notification/{name}"]["get"]
    assert [p["name"] for p in operation["parameters"] if p["in"] == "path"] == ["name"]


def test_the_served_document_is_the_file():
    document = spec()
    status, served = rs.route("GET", "/api/v1/openapi.json", {}, None, headers={"X-Api-Key": KEY})
    assert status == 200
    assert served == document


def test_the_commands_in_the_spec_are_the_commands_the_handler_accepts():
    enum = spec()["components"]["schemas"]["CommandRequest"]["properties"]["name"]["enum"]
    assert tuple(enum) == review_api_v1.COMMANDS


def test_the_spec_is_not_inlined_in_the_handler():
    """One source of truth: the handler reads the file, it does not carry a second document."""
    assert review_api_v1.OPENAPI_PATH.is_file()
    assert review_api_v1.OPENAPI_PATH.name == "openapi.json"
    assert review_api_v1.OPENAPI_PATH.parent.name == "review"
