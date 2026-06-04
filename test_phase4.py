"""Phase 4 — sellers + listings + reviews + coordinator orchestration tests."""
import os
import shutil
import tempfile
import importlib
import pytest


@pytest.fixture()
def fresh(monkeypatch):
    d = tempfile.mkdtemp(prefix="dg_p4_")
    from decisiongraph import config
    monkeypatch.setattr(config, "STORAGE_DIR", d)
    from decisiongraph import sellers, listings, reviews
    importlib.reload(sellers); importlib.reload(listings); importlib.reload(reviews)
    monkeypatch.setattr(listings, "AUTO_APPROVE", True)
    monkeypatch.setenv("DG_ALLOW_LOCAL_WEBHOOKS", "1")
    yield d, sellers, listings, reviews
    shutil.rmtree(d, ignore_errors=True)


# ── Sellers ────────────────────────────────────────────────────────────
def test_seller_signup_returns_key_once(fresh):
    _, sellers, _, _ = fresh
    s = sellers.signup("AcmeAI Labs", email="hi@acme.io", bio="we ship agents")
    assert s["seller_key"].startswith("sk_")
    assert s["slug"] == "acmeai-labs"
    assert s["display_name"] == "AcmeAI Labs"
    # subsequent get does NOT return the key
    g = sellers.get(s["id"])
    assert "seller_key" not in g and "key_hash" not in g


def test_seller_authenticate(fresh):
    _, sellers, _, _ = fresh
    s = sellers.signup("X")
    assert sellers.authenticate(s["seller_key"])["id"] == s["id"]
    assert sellers.authenticate("sk_wrong") is None


def test_seller_slug_uniqueness(fresh):
    _, sellers, _, _ = fresh
    a = sellers.signup("Acme")
    b = sellers.signup("Acme")  # same display name
    assert a["slug"] != b["slug"]


# ── Listings ───────────────────────────────────────────────────────────
def test_submit_listing_and_appears_public(fresh):
    _, sellers, listings, _ = fresh
    s = sellers.signup("S")
    l = listings.submit(s["id"], name="SQL Sage",
                         description="writes SQL",
                         webhook_url="https://example.com/hire",
                         tags=["sql"], suggested_topics=["metrics"])
    assert l["approved"] is True
    public = listings.list_public()
    assert any(p["id"] == l["id"] for p in public)


def test_webhook_validation(fresh):
    _, sellers, listings, _ = fresh
    s = sellers.signup("S")
    with pytest.raises(ValueError):
        listings.submit(s["id"], name="X", webhook_url="not-a-url")
    with pytest.raises(ValueError):
        listings.submit(s["id"], name="X", webhook_url="")


def test_listing_unapproved_not_public(fresh):
    _, sellers, listings, _ = fresh
    # disable auto-approve
    listings.AUTO_APPROVE = False
    s = sellers.signup("S")
    l = listings.submit(s["id"], name="N",
                         webhook_url="https://example.com/x")
    assert l["approved"] is False
    assert not any(p["id"] == l["id"] for p in listings.list_public())
    listings.approve(l["id"], True)
    assert any(p["id"] == l["id"] for p in listings.list_public())


def test_listing_update_and_delete(fresh):
    _, sellers, listings, _ = fresh
    s = sellers.signup("S")
    l = listings.submit(s["id"], name="N",
                         webhook_url="https://example.com/x")
    listings.update(l["id"], description="new")
    assert listings.get(l["id"])["description"] == "new"
    listings.delete(l["id"])
    assert listings.get(l["id"]) is None


# ── Reviews ────────────────────────────────────────────────────────────
def test_reviews_aggregate(fresh):
    _, sellers, listings, reviews = fresh
    s = sellers.signup("S")
    l = listings.submit(s["id"], name="N",
                         webhook_url="https://example.com/x")
    reviews.submit(l["id"], buyer_workspace="ws1", stars=5, comment="great")
    reviews.submit(l["id"], buyer_workspace="ws2", stars=3, comment="ok")
    refreshed = listings.get(l["id"])
    assert refreshed["rating_count"] == 2
    assert abs(listings.average_rating(refreshed) - 4.0) < 0.01


def test_reviews_clamp_stars(fresh):
    _, sellers, listings, reviews = fresh
    s = sellers.signup("S")
    l = listings.submit(s["id"], name="N",
                         webhook_url="https://example.com/x")
    reviews.submit(l["id"], buyer_workspace="w", stars=99)
    reviews.submit(l["id"], buyer_workspace="w", stars=0)
    # both clamped to 1..5
    items = reviews.for_listing(l["id"])
    assert all(1 <= r["stars"] <= 5 for r in items)


def test_review_for_nonexistent_listing(fresh):
    _, _, _, reviews = fresh
    with pytest.raises(ValueError):
        reviews.submit("lst_nope", buyer_workspace="w", stars=4)
