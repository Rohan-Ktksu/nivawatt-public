"""Issue #31: what the service guarantees when a decision is processed, independent of the browser.

Authorization and proposal/run binding are checked when the request is processed; duplicates create one commitment;
client sign-out does not revoke the demo session on the server (documented semantics, not a guarantee of rejection).
"""
import uuid

from tests.core.test_core import FS, api, state  # noqa: F401  (api is a fixture)


def run_with_proposal(c):
    tag = uuid.uuid4().hex[:8]
    r = c.post("/api/v1/runs", headers=c.op, json={"request_id": f"run-{tag}", "clock_start_utc": FS, "apartment_params": {}})
    assert r.status_code == 200, r.text
    run = r.json()["run_id"]
    p = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": f"q-{tag}", "ev_sessions": []})
    assert p.status_code == 200, p.text
    return run, p.json()["proposal"]


def decide(c, prop, run_id, rid, headers=None, decision="ACCEPT"):
    return c.post(f"/api/v1/proposals/{prop['proposal_id']}/decision", headers=headers or c.mgr, json={
        "run_id": run_id, "expected_revision": prop["revision"], "decision": decision,
        "option_id": prop["options"][0]["option_id"], "request_id": rid})


def commitment(c, run):
    return state(c, run)["active_commitment"]


def test_acceptance_cannot_be_redirected_to_another_run(api):
    c = api
    run_a, prop_a = run_with_proposal(c)
    run_b, prop_b = run_with_proposal(c)
    # proposal A with run B in the body (e.g. a stale screen after switching runs): refused, nothing changes
    r = decide(c, prop_a, run_b, "x-1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "WRONG_RUN"
    assert commitment(c, run_a) is None and commitment(c, run_b) is None
    # and the other way round
    r = decide(c, prop_b, run_a, "x-2")
    assert r.status_code == 409 and r.json()["error"]["code"] == "WRONG_RUN"
    assert commitment(c, run_a) is None and commitment(c, run_b) is None


def test_acceptance_requires_a_manager_of_that_apartment(api):
    c = api
    run, prop = run_with_proposal(c)
    r = decide(c, prop, run, "y-1", headers=c.mgr2)  # manager of APT_2 deciding APT_1's proposal
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN_APARTMENT"
    r = decide(c, prop, run, "y-2", headers=c.op)  # operator cannot accept for the apartment
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN_ROLE"
    r = decide(c, prop, run, "y-3", headers={"Authorization": "Bearer not-a-session"})
    assert r.status_code == 401
    assert commitment(c, run) is None


def test_duplicate_acceptances_create_one_commitment(api):
    c = api
    run, prop = run_with_proposal(c)
    first = decide(c, prop, run, "dup-1")
    assert first.status_code == 200
    cid = commitment(c, run)["commitment_id"]
    # the same request again (a retried tap): the stored response, no second commitment
    again = decide(c, prop, run, "dup-1")
    assert again.status_code == 200 and again.json() == first.json()
    # a new request id for the same, already accepted proposal: refused
    other = decide(c, prop, run, "dup-2")
    assert other.status_code == 409 and other.json()["error"]["code"] == "STALE_PROPOSAL"
    from backend import db
    rows = db.q("SELECT commitment_id FROM commitments WHERE run_id=?", run)
    assert [x["commitment_id"] for x in rows] == [cid]
    # reusing a request id for a different command is refused, not silently replayed
    clash = decide(c, prop, run, "dup-1", decision="DECLINE")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "REQUEST_ID_REUSED"


def test_client_sign_out_does_not_revoke_the_session_on_the_server(api):
    """Documents the actual semantics: the page's sign-out only forgets the token; there is no server revocation, so a
    request already sent with it is processed normally. Nothing here promises that in-flight requests are rejected."""
    c = api
    run, prop = run_with_proposal(c)
    token_headers = dict(c.mgr)  # the token a page held before "signing out" (it simply stops sending it)
    r = decide(c, prop, run, "late-1", headers=token_headers)
    assert r.status_code == 200 and commitment(c, run)["proposal_id"] == prop["proposal_id"]
    paths = [route.path for route in __import__("backend.main", fromlist=["app"]).app.routes]
    assert not any("logout" in p or "sign-out" in p or "signout" in p for p in paths)  # no revocation endpoint exists
