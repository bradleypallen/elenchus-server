"""The partner strip on the landing pages is driven by a hand-edited JSON
file. A typo there — a logo that isn't in the directory, a non-https link
— would show up as a broken image or an unsafe link on the first page
anyone sees, so the file is checked here.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import elenchus
from elenchus.server import app

STATIC = Path(elenchus.__file__).parent / "static"
CONFIG = json.loads((STATIC / "partners.json").read_text(encoding="utf-8"))


def test_shape():
    assert isinstance(CONFIG.get("label", ""), str)
    partners = CONFIG["partners"]
    assert partners, "an empty strip should be expressed by removing the component, not the list"
    names = [p["name"] for p in partners]
    assert len(names) == len(set(names)), "partner names are used as React keys"
    for p in partners:
        assert set(p) <= {"name", "title", "caption", "url", "logo"}, p
        assert p["name"].strip() and p["url"].startswith("https://"), p


def test_universities_are_named_in_full():
    """Not "UvA" / "VU": a visitor from outside the Netherlands shouldn't
    have to decode an abbreviation to know who is behind the project."""
    shown = " | ".join(f"{p['name']} {p.get('caption', '')}" for p in CONFIG["partners"])
    assert "University of Amsterdam" in shown
    assert "Vrije Universiteit Amsterdam" in shown
    for p in CONFIG["partners"]:
        for abbreviation in ("UvA", "VU "):
            assert abbreviation not in f"{p['name']} ", p["name"]
        # When a lab's logo is used, the caption is what names the university.
        if "Amsterdam" in p["name"]:
            assert "Amsterdam" in p.get("caption", ""), p


def test_funding_acknowledgement():
    funding = CONFIG["funding"]
    assert set(funding) <= {"text", "link_text", "url"}
    assert "Alfred P. Sloan Foundation" in funding["text"]
    assert funding["url"].startswith("https://")
    # The linked words must actually occur in the sentence, or no link renders.
    assert funding["link_text"] in funding["text"]


def test_every_named_logo_exists_and_is_an_image():
    for p in CONFIG["partners"]:
        logo = p.get("logo")
        if logo is None:
            continue
        assert "/" not in logo and ".." not in logo, f"logo must be a bare file name: {logo!r}"
        assert logo.lower().endswith((".svg", ".png")), logo
        assert (STATIC / "logos" / logo).is_file(), f"{logo} is named in partners.json but missing"


def test_no_orphan_logo_files():
    """A logo file nobody references is usually a half-finished edit."""
    named = {p["logo"] for p in CONFIG["partners"] if p.get("logo")}
    on_disk = {
        f.name for f in (STATIC / "logos").iterdir() if f.suffix.lower() in (".svg", ".png")
    }
    assert on_disk <= named, f"unreferenced logo files: {sorted(on_disk - named)}"


def test_served_without_auth():
    """The strip is on the sign-in page, so it must load for anonymous visitors."""
    r = TestClient(app).get("/static/partners.json")
    assert r.status_code == 200
    assert r.json() == CONFIG


def test_service_worker_leaves_partner_files_to_the_browser_cache():
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "/static/partners.json" in sw and "/static/logos/" in sw
