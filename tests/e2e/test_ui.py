"""
Browser-driven UI E2E — drives the real served React frontend in a
headless chromium against a live `elenchus` server.

Where the access-probe phase checks the *API's* auth posture, these
check that the *user experience* holds up: the login/signup/magic-link
forms actually submit and route, a participant's study link drops them
into the briefing, the judge view renders a blinded package, and a
wrong password produces a graceful error rather than a broken page.

OFF by default — see conftest.py for how to enable.
"""

from __future__ import annotations

from playwright.sync_api import expect


def test_login_rejects_bad_password_then_accepts_good(page, live_server):
    page.goto(live_server.base_url)
    # The login form renders.
    expect(page.get_by_placeholder("email")).to_be_visible()

    # Wrong password → a visible error, and we stay on the login form
    # (no white screen, no crash).
    page.get_by_placeholder("email").fill(live_server.admin_email)
    page.get_by_placeholder("password").fill("definitely-wrong")
    page.get_by_role("button", name="SIGN IN").click()
    expect(page.get_by_text("Invalid email or password")).to_be_visible()
    expect(page.get_by_placeholder("email")).to_be_visible()

    # Correct credentials → we leave the login form (land in the app).
    page.get_by_placeholder("password").fill(live_server.admin_password)
    page.get_by_role("button", name="SIGN IN").click()
    expect(page.get_by_placeholder("password")).to_have_count(0)


def test_signup_from_invite_url(page, live_server):
    # A ?token= link routes straight to the signup form with the token
    # pre-filled; completing it lands the new user in the app.
    page.goto(f"{live_server.base_url}/?token={live_server.invite_token}")
    expect(page.get_by_placeholder("display name")).to_be_visible()
    page.get_by_placeholder("display name").fill("New User")
    page.get_by_placeholder("choose a password").fill("Newuser-pw-123456")
    page.get_by_role("button", name="CREATE ACCOUNT").click()
    # Signup succeeded → the auth form is gone.
    expect(page.get_by_role("button", name="CREATE ACCOUNT")).to_have_count(0)


def test_participant_study_link_routes_into_briefing(page, live_server):
    # A ?study= link consumes the token and drops the participant into
    # the briefing — no login step.
    page.goto(f"{live_server.base_url}/?study={live_server.study_token}")
    expect(page.get_by_text("Welcome to the study")).to_be_visible()
    expect(page.get_by_role("button", name="BEGIN TUTORIAL")).to_be_visible()


def test_magic_link_request_is_graceful(page, live_server):
    page.goto(live_server.base_url)
    page.get_by_role("button", name="email me a login link").click()
    page.get_by_placeholder("email").fill(live_server.admin_email)
    page.get_by_role("button", name="SEND LINK").click()
    # The confirmation copy renders (no enumeration, no error).
    expect(page.get_by_text("magic link has been sent")).to_be_visible()


def test_judge_view_renders_blinded(page, live_server):
    # Inject the seeded judge session so this test is about the blinded
    # VIEW, not the login form (which the admin/signup tests already
    # cover). Going through the form here would couple the blinding
    # assertion to login-form timing.
    page.context.add_cookies(
        [
            {
                "name": "elenchus_session",
                "value": live_server.judge_session,
                "url": live_server.base_url,
            }
        ]
    )
    page.goto(live_server.base_url)

    # The judge lands on their queue and opens the seeded assignment.
    expect(page.get_by_text("Your evaluation queue")).to_be_visible()
    page.get_by_text(f"Evaluation #{live_server.assignment_id}").click()

    # Both outputs render under NEUTRAL slot headers.
    expect(page.get_by_text("OUTPUT A", exact=True)).to_be_visible()
    expect(page.get_by_text("OUTPUT B", exact=True)).to_be_visible()
    # Content from both seeded reports is present.
    expect(page.get_by_text("widget")).to_have_count(1)
    expect(page.get_by_text("gadget")).to_have_count(1)

    # Blinding holds in the rendered UI: the ground-truth condition word
    # 'baseline' never appears (the guess control is labelled
    # 'dialogue'/'free-form', not by condition name). A leak of the
    # slot→condition mapping into the DOM would surface it here.
    body = page.locator("body").inner_text().lower()
    assert "baseline" not in body, "condition word 'baseline' leaked into the judge view"
