"""Judge-controlled scenarios through the real API (in-process service, temporary DB, synthetic simulator plant).

python scripts/judge_scenarios.py          -> results/judge_scenarios/summary.{json,md}; exits non-zero on a failed check

The "judge" chooses the actual switch positions at each interval (the simulated manager's hands); the service only
sees its own observations. Each scenario checks the advisory workflow:
- measured feedback produces a revised proposal when execution deviates;
- a revision stays PENDING and the active commitment is unchanged until the manager accepts it;
- missing readings stay unknown (STALE / null), never zero.
Pump durations, dry-run protection and the plant are demonstration assumptions; nothing here is hardware evidence.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp())
os.environ["NIVAWATT_DB"] = str(tmp / "judge.db")
(tmp / "codes.json").write_text(json.dumps({"operator_code": "op", "manager_codes": {"APT_1": "m1"}, "device_tokens": {}}))
os.environ["NIVAWATT_CODES"] = str(tmp / "codes.json")

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402

OUT = Path(os.environ.get("NIVAWATT_JUDGE_OUT", ROOT / "results" / "judge_scenarios"))
CLOCK = "2026-08-18T18:30:00Z"
c = TestClient(app)


def call(method, path, tok=None, **body):
    r = c.request(method, "/api/v1" + path, json=body or None, headers={"Authorization": "Bearer " + tok} if tok else {})
    if r.status_code != 200:
        raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text}")
    return r.json()


OP = call("POST", "/demo/session", role="operator", access_code="op")["token"]
MG = call("POST", "/demo/session", role="manager", apartment_id="APT_1", access_code="m1")["token"]


def pump_on_intervals(option):
    return [a["interval_index"] for a in option["actions"] if a["pump"] == "ON"]


class Run:
    def __init__(self, name, params=None):
        self.name, self.checks, self.log = name, [], []
        self.acceptances = self.claims = 0
        self.run = call("POST", "/runs", OP, request_id=f"{name}-run", clock_start_utc=CLOCK,
                        apartment_params=params or {})["run_id"]
        p = call("POST", "/requirements", MG, run_id=self.run, request_id=f"{name}-req", ev_sessions=[])["proposal"]
        self.first_proposal = p
        self.accept(p)

    def state(self):
        return call("GET", f"/state?run_id={self.run}&apartment_id=APT_1", MG)

    def accept(self, p):
        self.acceptances += 1
        return call("POST", f"/proposals/{p['proposal_id']}/decision", MG, run_id=self.run, expected_revision=p["revision"],
                    decision="ACCEPT", option_id=p["options"][0]["option_id"], request_id=f"{self.name}-acc{self.acceptances}")

    def planned(self, i):
        s = self.state()
        a = next(x for x in s["active_commitment"]["option"]["actions"] if x["interval_index"] == i)
        return a, s["active_commitment"]

    def claim(self, i, kind):
        _, com = self.planned(i)
        self.claims += 1
        call("POST", "/execution/claims", MG, run_id=self.run, commitment_id=com["commitment_id"], interval_index=i,
             action_id=f"{com['commitment_id']}:{i}:{kind}", request_id=f"{self.name}-cl{self.claims}")

    def step(self, i, pump=None, ev=None, drop=False):
        a, com = self.planned(i)
        sw = {"pump": pump or a["pump"], "ev": ev or ("ON" if a["ev"] != "OFF" else "OFF")}
        body = {"request_id": f"{self.name}-s{i}", "actual_switches": sw}
        if drop:
            body["drop_observation"] = True
        out = call("POST", f"/runs/{self.run}/step", OP, **body)
        ex, st = out["execution"] or {}, out["state"]
        self.log.append({"interval": i, "switches": sw, "status": ex.get("observation_status"), "reason": ex.get("reason"),
                         "pending": [p["reason"] for p in st["pending_proposals"]],
                         "active_commitment": st["active_commitment"]["commitment_id"],
                         "revision_gate": {k: (st.get("revision_gate") or {}).get(k) for k in
                                           ("decision", "urgent", "trigger", "at_interval", "proposal_status")},
                         "pump_state": st["readiness"].get("pump_state") if st.get("readiness") else None,
                         "aggregate_model_kw": (st.get("observation") or {}).get("aggregate_model_kw"),
                         "allowance_model_kw": st["apartment"]["allowance_model_kw"],
                         "operator_intervention": (st.get("operator_intervention") or {}).get("reason")})
        return out, com

    def check(self, label, ok):
        self.checks.append({"check": label, "passed": bool(ok)})

    def follow(self, start, stop):
        """The person executes the active plan; a responsive manager accepts each new revision as it arrives."""
        for i in range(start, stop):
            out, com = self.step(i)
            pend = out["state"]["pending_proposals"]
            if pend:
                self.check(f"interval {i}: revision pending, commitment unchanged before acceptance",
                           out["state"]["active_commitment"]["commitment_id"] == com["commitment_id"])
                self.accept(pend[0])

    def summary(self):
        statuses = {}
        for e in self.log:
            statuses[e["status"]] = statuses.get(e["status"], 0) + 1
        rep = call("GET", f"/runs/{self.run}/report", OP)
        return {"scenario": self.name, "run_id": self.run, "verdict_counts": statuses, "manager_acceptances": self.acceptances,
                "manager_done_reports": self.claims, "checks": self.checks,
                "all_checks_passed": all(x["passed"] for x in self.checks),
                "report_metrics": {k: rep.get(k) for k in ("reserve_violation_intervals", "unmet_withdrawal_l",
                                                           "pump_starts", "peak_participating_model_kw",
                                                           "allowance_exceedance_model_kwh",
                                                           "ev_shortfall_at_departure_model_kwh")}}


def first_pump(r):
    return pump_on_intervals(r.state()["active_commitment"]["option"])[0]


def accept_pending_and_check(r, before_commitment, before_pump):
    s = r.state()
    pend = s["pending_proposals"]
    r.check("revision proposed after the deviation", len(pend) == 1)
    r.check("active commitment unchanged until the manager accepts", s["active_commitment"]["commitment_id"] == before_commitment)
    new = r.accept(pend[0])
    r.check("revision active only after acceptance", new["active_commitment"]["commitment_id"] != before_commitment)
    r.check("measured feedback changed the pump schedule",
            pump_on_intervals(new["active_commitment"]["option"]) != before_pump)
    return new


def normal():
    r = Run("normal")
    r.follow(0, 96)
    r.check("every interval OBSERVED_MATCH", all(e["status"] == "OBSERVED_MATCH" for e in r.log))
    r.check("no revision was ever proposed", not any(e["pending"] for e in r.log))
    return r


def false_done():
    r = Run("false_done_pump_left_off")
    k = first_pump(r)
    r.follow(0, k)
    before = r.state()["active_commitment"]
    r.claim(k, "pump")
    out, _ = r.step(k, pump="OFF")
    r.check("done report contradicted by readings", out["execution"]["observation_status"] == "OBSERVED_DEVIATION")
    r.check("revision reason names the unsupported claim",
            out["state"]["pending_proposals"][0]["reason"] == "CLAIM_WITHOUT_OBSERVED_EXECUTION")
    new = accept_pending_and_check(r, before["commitment_id"], pump_on_intervals(before["option"]))
    r.follow(new["clock"]["interval_index"], 96)
    r.check("after acceptance the revised plan executes", r.log[-1]["status"] == "OBSERVED_MATCH")
    return r


def delayed():
    r = Run("delayed_pump_start")
    k = first_pump(r)
    r.follow(0, k)
    before = r.state()["active_commitment"]
    out, _ = r.step(k, pump="OFF")  # person starts one interval late
    r.check("late start is a deviation", out["execution"]["observation_status"] == "OBSERVED_DEVIATION")
    new = accept_pending_and_check(r, before["commitment_id"], pump_on_intervals(before["option"]))
    r.follow(new["clock"]["interval_index"], 96)
    r.check("after acceptance the revised plan executes", r.log[-1]["status"] == "OBSERVED_MATCH")
    return r


def stale():
    r = Run("missing_readings")
    k = first_pump(r)
    r.follow(0, k)
    before = r.state()["active_commitment"]["commitment_id"]
    out1, _ = r.step(k, drop=True)
    out2, _ = r.step(k + 1, drop=True)
    for out in (out1, out2):
        obs = out["state"]["observation"]
        r.check("missing reading is STALE, not zero", out["execution"]["observation_status"] == "STALE"
                and obs["tank_l"] is None and obs["aggregate_model_kw"] is None)
    r.check("pump state reported UNKNOWN while readings are missing", r.log[-1]["pump_state"] in (None, "UNKNOWN"))
    r.check("missing data alone proposes no revision", not out2["state"]["pending_proposals"])
    r.check("commitment unchanged", out2["state"]["active_commitment"]["commitment_id"] == before)
    r.follow(k + 2, 96)
    r.check("readings resume and match", r.log[-1]["status"] == "OBSERVED_MATCH")
    return r


def outage(protected):
    r0 = Run("probe-outage-" + ("p" if protected else "u"))  # same default plan: find the first run to cut
    run_iv = [i for i in pump_on_intervals(r0.state()["active_commitment"]["option"])][:4]
    r = Run("outage_during_run_" + ("protected" if protected else "unprotected"),
            {"source_outage_intervals": run_iv + [run_iv[-1] + 1, run_iv[-1] + 2], "pump_dry_run_protection": protected})
    k = run_iv[0]
    r.follow(0, k)
    before = r.state()["active_commitment"]
    out, _ = r.step(k)  # the judge switches the pump ON as planned; the source is dry
    ex = out["execution"]
    r.check("outage during the run is detected from readings", ex["observation_status"] == "OBSERVED_DEVIATION")
    r.check("reason matches the plant behaviour",
            ex["reason"] == ("PUMP_NOT_OBSERVED_ON" if protected else "PUMP_POWER_WITHOUT_WATER_DELIVERY"))
    g = out["state"]["revision_gate"] or {}
    r.check("revision gate decision recorded (proposed now, or withheld as not yet actionable / cooldown)",
            g.get("decision") in ("PROPOSE", "SUPPRESSED_NOT_ACTIONABLE", "DEFERRED_COOLDOWN"))
    r.check("a withheld revision is not pending and the commitment is unchanged",
            g.get("decision") == "PROPOSE" or (not out["state"]["pending_proposals"]
                                              and out["state"]["active_commitment"]["commitment_id"] == before["commitment_id"]))
    if out["state"]["pending_proposals"]:  # responsive manager accepts now; otherwise follow() accepts when proposed
        r.check("proposed revision pending until accepted",
                out["state"]["active_commitment"]["commitment_id"] == before["commitment_id"])
        r.accept(out["state"]["pending_proposals"][0])
    r.follow(k + 1, 96)
    final = r.state()["active_commitment"]["option"]
    r.check("a revised schedule was accepted by the manager", r.acceptances >= 2)
    r.check("measured feedback changed the pump schedule", pump_on_intervals(final) != pump_on_intervals(before["option"]))
    return r


DAY_EV = {"ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 36, "departure_interval": 72, "requested_model_kwh": 10}]}


def within_cap(r):
    return all(e["aggregate_model_kw"] is None or e["aggregate_model_kw"] <= e["allowance_model_kw"] + 1e-6 for e in r.log)


def allowance_rescheduled():
    """Tight cap (4 kW): the tank starts low (3,200 L) so the pump is needed at once while the EV arrives at the same
    time; pump (2.2) + charger (3.3) cannot run together under 4 kW, so the scheduler must sequence them."""
    r = Run("allowance_conflict_rescheduled", {"allowance_model_kw": 4.0, "tank_init_l": 3200})
    opt = r.first_proposal["options"][0]
    row = next(x for x in opt["constraint_report"] if x["class"] == "allowance")
    r.check("conflict detected before execution: allowance binds", row["status"] == "binding_resolved_by_rescheduling")
    r.check("feasible alternative inside the cap, no operator intervention needed",
            opt["operator_intervention"] is None and opt["predicted_peak_model_kw"] <= 4.0)
    r.follow(0, 96)
    r.check("executed load never above the cap", within_cap(r))
    r.check("EV served by its deadline", (r.summary()["report_metrics"]["ev_shortfall_at_departure_model_kwh"] or {})
            .get("EV_1") == 0)
    return r


def allowance_escalation():
    """3 kW cap below the 3.3 kW charger: no schedule inside the cap charges the EV, so the operator must decide."""
    r = Run("allowance_conflict_escalated", {"allowance_model_kw": 3.0})
    opt = r.first_proposal["options"][0]
    oi = opt["operator_intervention"] or {}
    r.check("conflict flagged before execution with binding constraint and shortfall",
            oi.get("reason") == "ALLOWANCE_BLOCKS_SERVICE" and oi.get("binding_constraint") == "allowance"
            and oi["shortfall_within_allowance"]["ev_shortfall_model_kwh"] > 0)
    r.check("no manufactured success: the plan reports the EV shortfall",
            any(x["kind"] == "EV_ENERGY" for x in r.first_proposal["infeasibilities"]))
    r.check("plan itself stays inside the cap", opt["predicted_peak_model_kw"] <= 3.0)
    r.check("escalation names the allowance that would help and that it is not physically verified",
            oi.get("allowance_that_would_reduce_shortfall_model_kw") == 3.3 and "NOT VERIFIED" in oi.get("physical_check", ""))
    s = r.state()
    r.check("risk visible in state after manager acceptance", (s.get("operator_intervention") or {}).get("reason")
            == "ALLOWANCE_BLOCKS_SERVICE" and s["apartment"]["allowance_model_kw"] == 3.0)
    bad = c.request("POST", "/api/v1/requirements", json={"run_id": r.run, "request_id": "mgr-cap", "ev_sessions": [],
                                                           "allowance_model_kw": 6.0},
                    headers={"Authorization": "Bearer " + MG})
    r.check("manager cannot raise the operator cap", bad.status_code == 403)
    r.follow(0, 4)
    r.check("cap retained until the operator decides", all(e["allowance_model_kw"] == 3.0 for e in r.log))
    i = r.state()["clock"]["interval_index"]
    call("POST", "/events", OP, request_id="op-cap", run_id=r.run, kind="allowance", known_from_interval=i,
         allowance_model_kw=3.3)
    out, com = r.step(i)  # the judge executes this interval before the manager reacts
    pend = out["state"]["pending_proposals"]
    r.check("operator approval produces a revision that still needs the manager",
            len(pend) == 1 and pend[0]["reason"] == "ALLOWANCE_CHANGED"
            and out["state"]["active_commitment"]["commitment_id"] == com["commitment_id"])
    resp = c.request("POST", f"/api/v1/proposals/{pend[0]['proposal_id']}/decision",
                     json={"run_id": r.run, "expected_revision": pend[0]["revision"], "decision": "ACCEPT",
                           "option_id": "o1", "request_id": "esc-late-accept"}, headers={"Authorization": "Bearer " + MG})
    r.acceptances += 1
    if resp.status_code == 409:  # the clock passed part of that plan: refused, refreshed proposal offered instead
        s = r.state()
        r.check("late acceptance of a plan that no longer meets its promise is refused, refreshed plan offered",
                resp.json()["error"]["code"] == "PROPOSAL_STALE" and len(s["pending_proposals"]) == 1
                and s["pending_proposals"][0]["reason"] == "PROPOSAL_STALE_AT_ACCEPTANCE")
        new = r.accept(s["pending_proposals"][0])
    else:
        new = resp.json()
    r.check("revised plan clears the shortfall within the new cap",
            new["active_commitment"]["option"]["operator_intervention"] is None
            and new["active_commitment"]["option"]["predicted_peak_model_kw"] <= 3.3)
    r.follow(new["clock"]["interval_index"], 96)
    r.check("executed load never above the cap in force", within_cap(r))
    r.check("EV served by its deadline after the operator decision",
            (r.summary()["report_metrics"]["ev_shortfall_at_departure_model_kwh"] or {}).get("EV_1") == 0)
    return r


if __name__ == "__main__":
    runs = [normal(), false_done(), delayed(), stale(), outage(True), outage(False), allowance_rescheduled(),
            allowance_escalation()]
    res = [x.summary() for x in runs]
    for x, r in zip(res, runs):
        x["first_deviation_reason"] = next((e["reason"] for e in r.log if e["status"] == "OBSERVED_DEVIATION"), None)
        x["interval_log"] = r.log
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps({"kind": "synthetic simulator runs through the real API; not hardware evidence",
                                                  "scenarios": res}, indent=2) + "\n")
    lines = ["# Judge-controlled scenarios (synthetic, through the real API)", "",
             "`python scripts/judge_scenarios.py`. The judge chooses actual switch positions; the service sees only its "
             "observations. Pump durations and dry-run protection are demonstration assumptions. Not hardware evidence.", "",
             "| Scenario | Verdicts | First deviation reason | Manager acceptances | Done reports | Reserve-violation intervals | Unmet water L | Pump starts | Checks |",
             "|---|---|---|---|---|---|---|---|---|"]
    for x in res:
        m = x["report_metrics"]
        lines.append(f"| {x['scenario']} | {x['verdict_counts']} | {x['first_deviation_reason'] or '-'} | "
                     f"{x['manager_acceptances']} | {x['manager_done_reports']} | {m['reserve_violation_intervals']} | "
                     f"{m['unmet_withdrawal_l']} | {m['pump_starts']} | "
                     f"{sum(c['passed'] for c in x['checks'])}/{len(x['checks'])} |")
    lines += ["", "Checks per scenario are listed in `summary.json`."]
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    failed = [(x["scenario"], c["check"]) for x in res for c in x["checks"] if not c["passed"]]
    print("\n".join(lines[4:]))
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
    print("ALL JUDGE SCENARIO CHECKS PASSED")
