"""Router authentication regression tests — no AWS calls, no deployed stack.

Guards the two properties that keep the public API from becoming a cross-tenant
execution path (HackerOne 3982589):

  1. There is no unauthenticated route that takes a tenantId from the URL and
     runs a prompt in that tenant's MicroVM.
  2. /tg/<tenantId> fails CLOSED — a tenant without a webhookSecret is not a
     tenant that anyone may drive over HTTP.

handler.py builds its boto3 clients at import time (one of them from the
lambda-microvms model overlay, which is absent from stock botocore), so a stub
boto3 is installed first. Any AWS call the router makes that the test did not
explicitly allow raises, which is what makes assertion 1 meaningful.

Run:  uv run --with pytest python -m pytest tests -q
"""
import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest

HANDLER_PATH = pathlib.Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "handler.py"

ENV = {
    "AWS_REGION": "us-east-1",
    "TENANTS_TABLE": "test-tenants",
    "AWS_LAMBDA_FUNCTION_NAME": "test-orchestrator",
    "IMAGE_ARN": "arn:aws:lambda:us-east-1:111122223333:microvm-image:test",
    "IMAGE_VERSION": "1",
    "EXEC_ROLE_ARN": "arn:aws:iam::111122223333:role/test",
    "INGRESS_CONNECTOR": "arn:aws:lambda:us-east-1:aws:network-connector:test:ALL_INGRESS",
    "EGRESS_CONNECTOR": "arn:aws:lambda:us-east-1:111122223333:network-connector:test",
}


class _NoAws:
    """Any attribute access yields a callable that fails the test."""

    def Table(self, *_a, **_k):
        return self

    def __getattr__(self, name):
        def fail(*_a, **_k):
            raise AssertionError(f"router reached AWS unexpectedly: {name}()")
        return fail


def _install_boto3_stub():
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *_a, **_k: _NoAws()
    boto3.resource = lambda *_a, **_k: _NoAws()

    class Attr:
        def __init__(self, *_a):
            pass

        def ne(self, *_a):
            return None

    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Attr = Attr
    dynamodb = types.ModuleType("boto3.dynamodb")
    dynamodb.conditions = conditions
    boto3.dynamodb = dynamodb
    sys.modules.update({"boto3": boto3, "boto3.dynamodb": dynamodb,
                        "boto3.dynamodb.conditions": conditions})


@pytest.fixture(scope="module")
def handler():
    _install_boto3_stub()
    os.environ.update(ENV)
    spec = importlib.util.spec_from_file_location("handler_under_test", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tenants(handler, monkeypatch):
    """Registry stub that records every lookup the router performs."""
    registry = {
        "with-secret": {"tenantId": "with-secret", "webhookSecret": "s3cret",
                        "botToken": "bot", "microvmId": "mv-1", "endpoint": "vm.example"},
        "no-secret": {"tenantId": "no-secret", "microvmId": "mv-2", "endpoint": "vm2.example"},
    }
    looked_up = []

    def get_tenant(tid):
        looked_up.append(tid)
        return registry.get(tid)

    monkeypatch.setattr(handler, "get_tenant", get_tenant)
    return looked_up


@pytest.fixture
def invokes(handler, monkeypatch):
    """Capture worker hand-offs instead of calling Lambda."""
    calls = []

    class Lam:
        def invoke(self, **kw):
            calls.append(json.loads(kw["Payload"]))
            return {}

    monkeypatch.setattr(handler, "lam", Lam())
    return calls


def _get(path, headers=None, query=""):
    return {"rawPath": path, "rawQueryString": query, "headers": headers or {}}


def _post(path, body, headers=None):
    return {"rawPath": path, "rawQueryString": "", "headers": headers or {}, "body": body}


# ---------- 1. no unauthenticated tenant-scoped execution route ----------
def test_chat_route_is_gone(handler, tenants, invokes):
    """/chat/<tenantId> must not exist: it ran caller prompts in any tenant's VM."""
    r = handler.router(_get("/chat/with-secret", query="m=pwn"))
    assert r["statusCode"] == 404
    assert r["body"] == "not found"
    assert tenants == [], "removed route must not even look the tenant up"
    assert invokes == []


def test_unmapped_path_denied_by_default(handler, tenants, invokes):
    for path in ("/", "/chat", "/tg", "/tg/with-secret/extra", "/admin"):
        assert handler.router(_get(path))["statusCode"] == 404
    assert invokes == []


# ---------- 2. /tg fails closed ----------
def test_tg_rejects_tenant_without_webhook_secret(handler, tenants, invokes):
    """A secret-less tenant has no webhook registered — nobody may drive it."""
    r = handler.router(_post("/tg/no-secret", '{"message":{"chat":{"id":1},"text":"pwn"}}'))
    assert r["statusCode"] == 403
    assert invokes == [], "must not reach the worker"


def test_tg_rejects_missing_secret_header(handler, tenants, invokes):
    r = handler.router(_post("/tg/with-secret", '{"message":{"chat":{"id":1},"text":"pwn"}}'))
    assert r["statusCode"] == 403
    assert invokes == []


def test_tg_rejects_wrong_secret_header(handler, tenants, invokes):
    r = handler.router(_post(
        "/tg/with-secret", '{"message":{"chat":{"id":1},"text":"pwn"}}',
        {"X-Telegram-Bot-Api-Secret-Token": "wrong"}))
    assert r["statusCode"] == 403
    assert invokes == []


def test_tg_unknown_tenant(handler, tenants, invokes):
    r = handler.router(_post("/tg/nope", "{}",
                             {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}))
    assert r["statusCode"] == 404
    assert invokes == []


def test_tg_accepts_correct_secret(handler, tenants, invokes):
    r = handler.router(_post(
        "/tg/with-secret", '{"message":{"chat":{"id":7},"text":"hi"}}',
        {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}))
    assert r["statusCode"] == 200
    assert [c["_worker"]["tenantId"] for c in invokes] == ["with-secret"]


def test_health_needs_no_auth(handler, tenants, invokes):
    r = handler.router(_get("/health"))
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["role"] == "router"
    assert tenants == []
