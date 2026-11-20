"""NivaWatt local API (contract v0.2). Advisory mode only: nothing here operates equipment."""
import contextlib
import hashlib
import json
import os
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import db
from billing import calculator as billing_calc
from billing import connection as billing_conn
from scheduler import demand_forecast, revision, rules
from simulation import meter, plant

SCHEMA_VERSION = "0.2"
ROOT = db.ROOT
WEB = ROOT / "web"
PROPOSAL_TTL_S = int(os.environ.get("NIVAWATT_PROPOSAL_TTL_S", "900"))
EV_ON_KW = 1e-6

app = FastAPI(title="NivaWatt", version=SCHEMA_VERSION)


# ---------- errors, time, transactions ----------
class ApiError(Exception):
    def __init__(self, status, code, message, revision=None):
        self.status, self.code, self.message, self.revision = status, code, message, revision


@app.exception_handler(ApiError)
def _api_error(_, e: ApiError):
    return JSONResponse({"error": {"code": e.code, "message": e.message}, "revision": e.revision}, status_code=e.status)


@app.exception_handler(HTTPException)
def _http_error(_, e: HTTPException):
    return JSONResponse({"error": {"code": "HTTP_" + str(e.status_code), "message": str(e.detail)}, "revision": None},
                        status_code=e.status_code)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s, field):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        raise ApiError(422, "INVALID_TIMESTAMP", f"{field} must be ISO-8601 UTC")


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@contextlib.contextmanager
def tx():
    with db.LOCK:
        db.ex("BEGIN IMMEDIATE")
        try:
            yield
            db.ex("COMMIT")
        except BaseException:
            db.ex("ROLLBACK")
            raise


def idempotent(scope, request_id, body, fn):
    """Same request_id + same body -> stored response. Same id, different body -> 409."""
    if not request_id or not isinstance(request_id, str):
        raise ApiError(400, "MISSING_REQUEST_ID", "request_id is required")
    h = hashlib.sha256(db.dumps(body).encode()).hexdigest()
    with tx():
        row = db.one("SELECT * FROM idempotency WHERE request_id=?", request_id)
        if row:
            if row["scope"] != scope or row["body_hash"] != h:
                raise ApiError(409, "REQUEST_ID_REUSED", "request_id already used for a different command")
            return json.loads(row["response"])
        result = fn()
        db.ex("INSERT INTO idempotency VALUES(?,?,?,?,?)", request_id, scope, h, 200, db.dumps(result))
        return result


# ---------- local demo sessions ----------
def codes_path():
    return Path(os.environ.get("NIVAWATT_CODES", ROOT / "config" / "local.json"))


def codes():
    path = codes_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "operator_code": secrets.token_urlsafe(6),
            "manager_codes": {"APT_1": secrets.token_urlsafe(6)},
            "device_tokens": {"bench-1": secrets.token_urlsafe(12)},
            "_note": "Local demonstration codes only; not production identity. Do not commit."}, indent=2))
        print(f"[nivawatt] generated demo access codes in {path}")
    return json.loads(path.read_text())



CAMPUS_APARTMENTS = ("APT_1", "APT_2", "APT_3", "APT_4")


def ensure_manager_codes(apartments=CAMPUS_APARTMENTS):
    """Give each campus apartment its own manager code in the ignored local config. Existing codes are never changed."""
    c = codes()
    have = c.setdefault("manager_codes", {})
    missing = [a for a in apartments if a not in have]
    for a in missing:
        have[a] = secrets.token_urlsafe(6)
    if missing:
        codes_path().write_text(json.dumps(c, indent=2))
        print(f"[nivawatt] added manager codes for {', '.join(missing)} in {codes_path()}")
    return missing

def session(authorization: str = Header(default="")):
    tok = authorization.removeprefix("Bearer ").strip()
    row = db.one("SELECT * FROM sessions WHERE token=?", tok) if tok else None
    if row:
        return dict(row)
    dev = {v: k for k, v in codes().get("device_tokens", {}).items()}
    if tok in dev:
        return {"role": "device", "device_id": dev[tok], "apartment_id": None}
    raise ApiError(401, "UNAUTHENTICATED", "missing or invalid demo session")


def require(sess, role, apartment_id=None):
    if sess["role"] != role:
        raise ApiError(403, "FORBIDDEN_ROLE", f"{role} role required")
    if apartment_id is not None and role == "manager" and sess["apartment_id"] != apartment_id:
        raise ApiError(403, "FORBIDDEN_APARTMENT", "session is scoped to another apartment")


async def body(request: Request):
    try:
        b = await request.json()
    except Exception:
        raise ApiError(400, "MALFORMED_JSON", "request body must be JSON")
    if not isinstance(b, dict):
        raise ApiError(400, "MALFORMED_JSON", "request body must be an object")
    return b


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "schema_version": SCHEMA_VERSION, "mode": "advisory",
            "hardware_connected": False, "note": "No hardware connection is claimed; telemetry is accepted if posted."}


@app.post("/api/v1/demo/session")
def demo_session(b: dict = Depends(body)):
    c, role = codes(), b.get("role")
    if role == "operator":
        ok, apt = secrets.compare_digest(str(b.get("access_code", "")), c["operator_code"]), None
    elif role == "manager":
        apt = b.get("apartment_id")
        expected = c.get("manager_codes", {}).get(apt)
        ok = expected is not None and secrets.compare_digest(str(b.get("access_code", "")), expected)
    else:
        raise ApiError(422, "INVALID_ROLE", "role must be operator or manager")
    if not ok:
        raise ApiError(401, "INVALID_ACCESS_CODE", "access code rejected")
    tok = secrets.token_urlsafe(24)
    with tx():
        db.ex("INSERT INTO sessions VALUES(?,?,?,?,?)", tok, role, apt, None, iso(now()))
    return {"token": tok, "role": role, "apartment_id": apt,
            "note": "Local demonstration session; not production authentication."}


# ---------- forecasts ----------
_series = {}


def dataset():
    if "s" not in _series:
        from forecasting import data
        try:
            _series["s"], _series["rep"] = data.load_validated()
        except FileNotFoundError as e:
            raise ApiError(503, "DATASET_UNAVAILABLE", str(e))
    return _series["s"], _series["rep"]


def get_forecast(fid):
    row = db.one("SELECT body FROM forecasts WHERE forecast_id=?", fid)
    if not row:
        raise ApiError(404, "UNKNOWN_FORECAST", f"forecast {fid} not found")
    return json.loads(row["body"])


@app.post("/api/v1/forecasts/replay")
def forecast_replay(b: dict = Depends(body), sess=Depends(session)):
    require(sess, "operator")
    from forecasting import evaluate
    if b.get("dataset_id") != "kptcl-load-curve":
        raise ApiError(422, "UNKNOWN_DATASET", "registered datasets: kptcl-load-curve")

    def make():
        s, rep = dataset()
        try:
            snap = evaluate.build_snapshot(s, rep, b.get("model_name"), b.get("forecast_start"),
                                           "results/forecast_evaluation.json" if evaluate.OUT.exists() else None)
        except ValueError as e:
            raise ApiError(422, "INVALID_FORECAST_REQUEST", str(e))
        snap = {"forecast_id": new_id("fc"), **snap}
        db.ex("INSERT INTO forecasts VALUES(?,?,?)", snap["forecast_id"], db.dumps(snap), snap["issued_at"])
        return snap
    return idempotent("forecast_replay", b.get("request_id"), b, make)


@app.get("/api/v1/forecasts/evaluation")
def forecast_evaluation(sess=Depends(session)):
    from forecasting import evaluate
    if not evaluate.OUT.exists():
        raise ApiError(404, "NO_EVALUATION", "run: python -m forecasting.evaluate")
    return json.loads(evaluate.OUT.read_text())


@app.get("/api/v1/forecasts/{fid}")
def forecast_get(fid: str, sess=Depends(session)):
    return get_forecast(fid)


@app.get("/api/v1/forecasts/{fid}/actuals")
def forecast_actuals(fid: str, sess=Depends(session)):
    """Evaluator view: historical actuals for the forecast day. Never a planning input."""
    import pandas as pd
    f = get_forecast(fid)
    if f["source_kind"] != "historical_replay":
        raise ApiError(422, "NO_ACTUALS", "synthetic forecasts have no actuals")
    s, _ = dataset()
    pts = []
    for p in f["points"]:
        v = s.get(pd.Timestamp(p["start"]).tz_localize(None))
        pts.append({"start": p["start"], "end": p["end"], "predicted_mw": p["predicted_mw"],
                    "actual_mw": None if v is None or pd.isna(v) else float(v)})
    errs = [abs(p["predicted_mw"] - p["actual_mw"]) for p in pts if p["actual_mw"] is not None]
    return {"forecast_id": fid, "use": "evaluation_only_not_a_planning_input", "points": pts,
            "mae_mw": round(sum(errs) / len(errs), 1) if errs else None}


# ---------- runs ----------
def load_run(run_id):
    row = db.one("SELECT * FROM runs WHERE run_id=?", run_id)
    if not row:
        raise ApiError(404, "UNKNOWN_RUN", f"run {run_id} not found")
    r = dict(row)
    r["params"], r["truth"] = json.loads(r["params"]), json.loads(r["truth"])
    return r


def save_run(r):
    db.ex("UPDATE runs SET params=?, truth=?, interval_index=?, revision=?, active_advisory_id=? WHERE run_id=?",
          db.dumps(r["params"]), db.dumps(r["truth"]), r["interval_index"], r["revision"],
          r["active_advisory_id"], r["run_id"])


def interval_time(r, i):
    return parse_ts(r["clock_start_utc"], "clock_start_utc") + timedelta(minutes=i * r["params"]["model_dt_minutes"])


# ---------- scheduling policy (causal demand-aware is the default for NEW runs; experimental: validated in synthetic scenarios) ----------
POLICY_LABELS = {
    "advisory_only": "Advisory-only scheduler",
    "demand_aware_causal_experimental": "Demand-aware scheduler with a causal whole-building forecast "
                                        "(Experimental: validated in synthetic scenarios)",
}
# Default for NEW runs (authorized 2026-11-08). Runs created earlier keep their stored policy; a run stored without one
# was created when advisory-only was the only scheduler. The perfect-mean-profile benchmark is research-only and has
# no API selection.
DEFAULT_POLICY = "demand_aware_causal_experimental"
DEFAULT_CHANGED = "2026-11-08"
MODE_LABELS = {"demand_aware": "Demand-aware planning", "fallback_advisory_only": "Fallback: advisory-only planning",
               "advisory_only": "Advisory-only planning"}
WC_SERVER_FIELDS = ("history", "day_start_interval", "spec", "pf")


def flats100_params(policy):
    """Fictional 100-flat building (flats-100-reference-v2 assumptions) on one simulated day from the run clock."""
    from billing import reference as ref
    raw = plant.default_withdrawals(96)
    k = ref.FLATS_100["daily_withdrawal_l"] / sum(raw)
    return {**ref.FLATS_100["apartment_params"], "fine_physics": dict(ref.FINE),
            "withdrawal_profile_l": [round(x * k, 1) for x in raw], "tank_init_l": ref.FLATS_100["initial_tank_l"],
            # one-day run from the run clock: the 00:00-07:00 part of the overnight window (a 19:00-24:00 part would
            # leave too few slots after the evening peak inside one simulated day)
            "ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 28, "requested_model_kwh": 15}],
            "scheduling_policy": policy,
            "whole_connection": {"source": "synthetic", "category": "HT-4", "contract_demand_kva": ref.SIZING["cd_tight_kva"],
                                 "load": "whole_building", "history_days": 7, "day_index": 30, "noise_seed": 9000}}


SCENARIO_PRESETS = {"flats100_advisory_only": "advisory_only",
                    "flats100_demand_aware_experimental": "demand_aware_causal_experimental"}


def setup_whole_connection(p, clock_start):
    """Server-side fields of the synthetic whole-connection channel: clock offset, load model and SYNTHETIC history of
    non-flexible readings for the days before the run (what a monitor would have stored). Never client-supplied."""
    from billing import reference as ref
    wc = p["whole_connection"]
    t = parse_ts(clock_start, "clock_start_utc") + timedelta(hours=5, minutes=30)  # IST
    wc["day_start_interval"] = (t.hour * 60 + t.minute) // p["model_dt_minutes"]
    wc["spec"] = {**ref.CONN_SPEC, "noise_seed": int(wc.get("noise_seed", 9000))}
    wc["pf"] = dict(ref.PF)
    hist = []
    for d in range(int(wc["day_index"]) - int(wc.get("history_days", 7)), int(wc["day_index"])):
        day = billing_conn.nonflex_series(wc.get("load", "whole_building"), d, wc["day_start_interval"], wc["spec"])
        hist.append([nonflex_reading(x, wc["pf"]) for x in day])
    wc["history"] = hist


def nonflex_reading(x, pf):
    kw = x["flats_kw"] + x["common_kw"] + x["event_kw"]
    kvar = billing_conn._q(x["flats_kw"], pf["flats"]) + billing_conn._q(x["common_kw"], pf["common"]) \
        + billing_conn._q(x["event_kw"], pf["event"])
    return {"kw": round(kw, 4), "kvar": round(kvar, 4)}


def synth_whole_connection(p, i, rec, lost):
    """Synthetic main-incomer reading for interval i. It INCLUDES the pump and EV branches (actual device power)."""
    wc = p["whole_connection"]
    x = billing_conn.nonflex_series(wc.get("load", "whole_building"), int(wc["day_index"]), wc["day_start_interval"],
                                    wc["spec"])[i]
    nf = nonflex_reading(x, wc["pf"])
    pump, ev = rec["pump_model_kw"], rec["ev_model_kw"]
    kw = nf["kw"] + pump + ev
    kvar = nf["kvar"] + billing_conn._q(pump, wc["pf"]["pump"]) + billing_conn._q(ev, wc["pf"]["ev"])
    return {"meter_id": "connection-1", "channel_id": "whole_connection", "source_kind": "synthetic",
            "scope": "whole building at the main incomer; includes the pump and EV branches",
            "average_power_model_kw": None if lost else round(kw, 4),
            "average_reactive_model_kvar": None if lost else round(kvar, 4),
            "quality": "MISSING" if lost else "VALID"}


def whole_connection_block(p, reading, electrical, quality):
    """Observation block: the incomer reading and the non-flexible load derived from it by SUBTRACTING the branch
    channels (never added to them). Unknown when the incomer or a branch reading is missing."""
    if reading is None:
        return None
    wc = p["whole_connection"]
    kw, kvar = reading["average_power_model_kw"], reading["average_reactive_model_kvar"]
    pump, ev = electrical.get("pump_model_kw"), electrical.get("ev_model_kw")
    known = quality in USABLE and kw is not None and pump is not None and ev is not None
    return {**reading, "quality": reading["quality"] if quality in USABLE else "MISSING",
            "non_flexible_model_kw": round(kw - pump - ev, 4) if known else None,
            "non_flexible_model_kvar": round(kvar - billing_conn._q(pump, wc["pf"]["pump"])
                                             - billing_conn._q(ev, wc["pf"]["ev"]), 4) if known else None,
            "derivation": "whole connection minus the pump and EV-group channels (subtracted, never added)"}


def demand_policy(r, cons):
    """Causal demand-aware decision from observations only; sets the planning cap or leaves the advisory-only plan."""
    p = r["params"]
    if p.get("scheduling_policy", "advisory_only") != "demand_aware_causal_experimental":
        return None
    wc, i = p.get("whole_connection"), r["interval_index"]
    if wc is None:  # no whole-building measurement on this run: never invented, so the documented fallback applies
        return {"mode": "fallback_advisory_only", "confidence": "unavailable",
                "reasons": ["no whole-building measurement is configured for this run"], "target_kva": None,
                "cap_min_model_kw": None, "cap_max_model_kw": None,
                "forecast": {"method": None, "status": "unavailable", "history_days_available": 0, "history_source": None},
                "at_interval": i}
    today = [None] * p["horizon_intervals"]
    for o in observed(r):
        b = o.get("whole_connection") or {}
        if o["interval_index"] < i and b.get("non_flexible_model_kw") is not None:
            today[o["interval_index"]] = {"kw": b["non_flexible_model_kw"], "kvar": b["non_flexible_model_kvar"]}
    d = demand_forecast.decide(wc["history"], today, i, wc["category"], contract_demand_kva=wc["contract_demand_kva"])
    if d["cap"] is not None:
        cons["demand_aware_allowance_kw"] = d["cap"]
    fc = d["forecast"]
    return {"mode": d["mode"], "confidence": d["confidence"], "reasons": d["reasons"], "target_kva": d["target_kva"],
            "cap_min_model_kw": min(d["cap"]) if d["cap"] else None, "cap_max_model_kw": max(d["cap"]) if d["cap"] else None,
            "forecast": {"method": fc["method"], "status": fc["status"], "history_days_available": fc["days_available"],
                         "history_source": "synthetic readings generated for the days before this run"},
            "at_interval": i}


def scheduling_block(r):
    p = r["params"]
    pol = p.get("scheduling_policy", "advisory_only")
    prov = p.get("policy_provenance") or {
        "source": "legacy", "note": f"created before {DEFAULT_CHANGED}, when advisory-only was the only scheduler"}
    status = demand_policy(r, {}) if pol == "demand_aware_causal_experimental" else None
    mode = status["mode"] if status else "advisory_only"
    out = {"policy": pol, "label": POLICY_LABELS[pol], "experimental": pol != "advisory_only",
           "requested_policy": pol, "requested_policy_label": POLICY_LABELS[pol], "requested_source": prov["source"],
           "provenance": prov, "effective_mode": mode, "effective_mode_label": MODE_LABELS[mode],
           "default_policy_for_new_runs": DEFAULT_POLICY,
           "whole_connection_measurement": None if not p.get("whole_connection") else
           "synthetic main-incomer channel (simulation only). A physical deployment needs an authorized data interface "
           "or independent whole-connection monitoring; the utility smart meter is not assumed to be accessible."}
    if pol == "demand_aware_causal_experimental":
        out["status"] = status
        out["target_note"] = ("The billing-demand target is a planning target, not an electrical safety limit; the hard "
                              "participating-load allowance and equipment protections are unchanged.")
        out["fallback_note"] = ("When whole-building readings or history are missing, insufficient or stale, planning "
                                "falls back to the original advisory-only scheduler. The fallback preserves service; it "
                                "does not guarantee avoiding demand charges. A change of mode never alters an accepted "
                                "plan: a new plan still needs the manager's acceptance.")
    return out


@app.post("/api/v1/runs")
def create_run(b: dict = Depends(body), sess=Depends(session)):
    require(sess, "operator")

    def make():
        overrides = dict(b.get("apartment_params") or {})
        if not overrides and b.get("scenario") in SCENARIO_PRESETS:
            overrides = flats100_params(SCENARIO_PRESETS[b["scenario"]])
        # an explicit selection always overrides the default for new runs
        source = "explicit" if "scheduling_policy" in overrides else "default"
        overrides.setdefault("scheduling_policy", DEFAULT_POLICY)
        if set(WC_SERVER_FIELDS) & set(overrides.get("whole_connection") or {}):
            raise ApiError(422, "INVALID_FIELDS", f"whole_connection fields {list(WC_SERVER_FIELDS)} are set by the server")
        forbidden = {"source_kind"} & set(overrides)
        if forbidden:
            raise ApiError(422, "INVALID_FIELDS", f"cannot override {sorted(forbidden)}")
        try:
            p = plant.make_params(overrides)
        except (ValueError, KeyError, TypeError) as e:
            raise ApiError(422, "INVALID_APARTMENT_PARAMS", str(e))
        start = b.get("clock_start_utc")
        if not start:
            raise ApiError(422, "MISSING_CLOCK_START", "clock_start_utc required (e.g. a forecast_start)")
        parse_ts(start, "clock_start_utc")
        if p["whole_connection"] is not None:
            if p["observation_source"] == "telemetry":
                raise ApiError(422, "WHOLE_CONNECTION_UNAVAILABLE",
                               "telemetry devices do not report whole-connection readings; that needs an authorized data "
                               "interface or independent monitoring")
            setup_whole_connection(p, start)
        p["policy_provenance"] = {"source": source, "requested_policy": p["scheduling_policy"],
                                  "default_policy_at_creation": DEFAULT_POLICY, "created_at": iso(now()),
                                  "candidate_validation": "flats-100-causal-v1 (synthetic)"}
        rid = new_id("run")
        db.ex("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)", rid, db.dumps(p), db.dumps(plant.initial_truth(p)),
              0, 0, start, None, iso(now()))
        return {"run_id": rid, "scenario": b.get("scenario", "synthetic_single_apartment"), "source_kind": "synthetic",
                "scheduling_policy": p["scheduling_policy"], "scheduling_policy_source": source,
                "model_dt_minutes": p["model_dt_minutes"], "horizon_intervals": p["horizon_intervals"]}
    return idempotent("create_run", b.get("request_id"), b, make)


# ---------- observation-derived planning state (never truth) ----------
def observed(r):
    rows = db.q("SELECT body FROM observations WHERE run_id=? ORDER BY interval_index", r["run_id"])
    return [json.loads(x["body"]) for x in rows]


def ev_interval_kwh(o, dt_h):
    """EV energy an observation measured: the EV channel's interval energy when the meter reports it, else power x
    interval (simulator readings, where both are equal)."""
    ch = next((c for c in (o.get("meter") or {}).get("channels", []) if c["channel_id"] == meter.EV_GROUP), None)
    if ch is not None and ch.get("interval_energy_model_kwh") is not None:
        return ch["interval_energy_model_kwh"]
    return o["ev_model_kw"] * dt_h


def planning_state(r):
    p, obs = r["params"], observed(r)
    valid = [o for o in obs if o["quality"] in USABLE]
    last = valid[-1] if valid else None
    delivered = {s["ev_id"]: 0.0 for s in p["ev_sessions"]}
    unattributed = 0.0  # EV-group energy measured while more than one EV session was present: not credited to any EV
    dt_h = p["model_dt_minutes"] / 60
    starts, prev = 0, False
    for o in valid:
        if o.get("ev_id"):
            delivered[o["ev_id"]] = delivered.get(o["ev_id"], 0) + ev_interval_kwh(o, dt_h)
        elif (o.get("ev_model_kw") or 0) > EV_ON_KW:
            unattributed += ev_interval_kwh(o, dt_h)
        if o["pump_model_kw"] is not None:  # unknown under aggregate-only metering
            on = o["pump_model_kw"] > 0
            starts += on and not prev
            prev = on
    # Current run / off length from consecutive measured intervals. A missing, stale or aggregate-only reading makes
    # them unknown (None) until a new measured start or stop; the run begins with the pump off and no stop yet.
    known_prev, run_len, off_len = False, None, None
    for o in obs:
        kw = o.get("pump_model_kw")
        if o["quality"] not in USABLE or kw is None:
            known_prev, run_len, off_len = None, None, None
            continue
        on = kw > 0
        if on:
            run_len = run_len + 1 if known_prev is True and run_len is not None else (1 if known_prev is False else None)
        else:
            off_len = off_len + 1 if known_prev is False and off_len is not None else (1 if known_prev is True else None)
        known_prev = on
    agg = p["observation_scope"] == "aggregate"
    return {
        "interval_index": r["interval_index"],
        "tank_l": last["tank_l"] if last else p["tank_init_l"],
        "tank_source": "observation" if last else "initial_parameter",
        "stale": bool(obs) and obs[-1]["quality"] not in USABLE,
        "aggregate_only": agg,
        "pump_on": None if agg or (last and last["pump_model_kw"] is None) else bool(last and last["pump_model_kw"] > 0),
        "pump_starts_history": None if agg else starts,
        "pump_run_intervals": None if agg or known_prev is not True else run_len,
        "pump_off_intervals": None if agg or known_prev is not False else off_len,
        "ev_delivered_observed_model_kwh": {k: round(v, 4) for k, v in delivered.items()},
        "ev_group_unattributed_model_kwh": round(unattributed, 4),
    }


def active_advisory(r):
    if not r["active_advisory_id"]:
        return None
    row = db.one("SELECT body FROM events WHERE event_id=?", r["active_advisory_id"])
    return json.loads(row["body"]) if row else None


def known_events(r):
    p, adv = r["params"], active_advisory(r)
    peaks = []
    if adv:
        wins = [(parse_ts(w["start"], "w"), parse_ts(w["end"], "w")) for w in adv["peak_windows"]]
        for i in range(p["horizon_intervals"]):
            t = interval_time(r, i)
            if any(a <= t < b for a, b in wins):
                peaks.append(i)
    src = {i: False for i in p.get("known_outage_intervals", [])}
    return {"peak_intervals": peaks, "source_water_available": src}


OPERATOR_OR_MANAGER_REASONS = {"PEAK_ADVISORY_ISSUED", "ALLOWANCE_CHANGED", "OUTAGE_ANNOUNCED", "REQUIREMENT_CHANGED",
                               "PROPOSAL_STALE_AT_ACCEPTANCE"}


def plan_inputs(r):
    """Planning state (observations only), remaining requirements and constraints for the scheduler."""
    p, ps = r["params"], planning_state(r)
    reqs = {"ev_sessions": [{**s, "remaining_model_kwh": max(0.0, s["requested_model_kwh"]
                             - ps["ev_delivered_observed_model_kwh"].get(s["ev_id"], 0))} for s in p["ev_sessions"]]}
    cons = {k: p[k] for k in ("horizon_intervals", "model_dt_minutes", "tank_max_l", "tank_min_l",
                              "pump_model_kw", "ev_charger_model_kw", "pump_inflow_lph", "allowance_model_kw")}
    cons["expected_withdrawal_l"] = p["withdrawal_profile_l"]
    for k, default in (("pump_min_on_intervals", 4), ("pump_min_off_intervals", 2),  # runs saved before these existed
                       ("reserve_margin_l", 0), ("tank_critical_l", 0), ("verified_electrical_limit_model_kw", None)):
        cons[k] = p.get(k, default)
    r["_demand_policy"] = demand_policy(r, cons)  # None for the default advisory-only policy
    return ps, reqs, cons


def replan(r, reason):
    """Compute a new pending proposal from observations. Supersedes older pending proposals."""
    p = r["params"]
    if r["interval_index"] >= p["horizon_intervals"]:
        return None
    ps, reqs, cons = plan_inputs(r)
    try:
        out = rules.plan(ps, known_events(r), reqs, cons)
    except Exception as e:  # keep current commitment; report failure instead of inventing a plan
        r.setdefault("_warnings", []).append(f"PLANNER_FAILED: {e}")
        return None
    # revision gate: only a material, actionable change asks the manager for an acceptance (scheduler/revision.py)
    com, gate = active_commitment(r["run_id"]), None
    if com:
        gate = revision.assess(com["body"]["option"]["actions"], out["options"][0]["actions"], ps, cons, reqs,
                               known_events(r), com["effective_from_interval"])
        gate["trigger"] = reason
        if reason in OPERATOR_OR_MANAGER_REASONS and gate["decision"] != "SUPPRESSED_EQUIVALENT":
            gate.update(decision="PROPOSE", initiated_by_decision=True)  # an explicit decision is never deferred
        if gate["decision"] != "PROPOSE":  # recorded for audit, never pending, needs no acceptance
            wid = new_id("w")
            body = {"proposal_id": wid, "run_id": r["run_id"], "apartment_id": p["apartment_id"], "status": "WITHHELD",
                    "reason": reason, "created_at": iso(now()), "revision_gate": gate, "planning_inputs": ps,
                    "options": out["options"], "infeasibilities": out["infeasibilities"],
                    "demand_policy": r.get("_demand_policy")}
            db.ex("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?)", wid, r["run_id"], p["apartment_id"], r["revision"],
                  "WITHHELD", reason, body["created_at"], body["created_at"], db.dumps(body))
            return None
    db.ex("UPDATE proposals SET status='SUPERSEDED' WHERE run_id=? AND status IN ('PROPOSED','DELIVERED')", r["run_id"])
    r["revision"] += 1
    pid, adv = new_id("p"), active_advisory(r)
    prop = {"proposal_id": pid, "run_id": r["run_id"], "apartment_id": p["apartment_id"], "revision": r["revision"],
            "status": "PROPOSED", "reason": reason, "source": out["source"], "computation_ms": out["computation_ms"],
            "created_at": iso(now()), "expires_at": iso(now() + timedelta(seconds=PROPOSAL_TTL_S)),
            "advisory_id": adv["advisory_id"] if adv else None, "forecast_id": adv["forecast_id"] if adv else None,
            "planning_inputs": ps, "options": out["options"], "infeasibilities": out["infeasibilities"],
            "revision_gate": gate, "demand_policy": r.get("_demand_policy")}
    db.ex("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?)", pid, r["run_id"], p["apartment_id"], r["revision"],
          "PROPOSED", reason, prop["created_at"], prop["expires_at"], db.dumps(prop))
    return prop


def reconsider_deferred(r):
    """Re-run the gate for a revision withheld only because it was not yet actionable or inside the cooldown."""
    if pending(r["run_id"]):
        return
    g = last_revision_gate(r["run_id"])
    if g and g.get("proposal_status") == "WITHHELD" and g["decision"] in ("DEFERRED_COOLDOWN", "SUPPRESSED_NOT_ACTIONABLE"):
        replan(r, "RECONSIDER_DEFERRED")


GATE_EXPLANATION = {
    "SUPPRESSED_EQUIVALENT": "A recomputed plan is the same as the current one, so there is nothing to accept.",
    "SUPPRESSED_NO_IMPROVEMENT": "A different plan would not improve water, EV energy (by 0.1 kWh or more), advisory-"
                                 "window energy or pump starts, so the current plan stands.",
    "SUPPRESSED_NOT_ACTIONABLE": "The change it would need is more than 2 hours away; it is checked again every interval.",
    "DEFERRED_COOLDOWN": "A plan was accepted less than 1 hour ago and this change is not urgent; it is checked again "
                         "every interval.",
}


def discrepancy_status(run_id):
    """Three separate facts for the UI: a discrepancy was detected; whether a revision is needed; whether one is
    awaiting the manager. A discrepancy stays visible even when no acceptance is requested."""
    c = active_commitment(run_id)
    since = c["effective_from_interval"] if c else 0
    row = db.one("SELECT interval_index, reason FROM executions WHERE run_id=? AND observation_status='OBSERVED_DEVIATION' "
                 "AND interval_index>=? ORDER BY interval_index DESC LIMIT 1", run_id, max(0, since - 1))
    pend = pending(run_id)
    g = last_revision_gate(run_id)
    if pend:
        rev = {"status": "AWAITING_MANAGER_ACCEPTANCE", "proposal_id": pend[-1]["proposal_id"],
               "explanation": "A revised plan is waiting for the manager to accept it. The current plan stays active "
                              "until then."}
    elif g and g.get("proposal_status") == "WITHHELD":
        deferred = g["decision"] in ("SUPPRESSED_NOT_ACTIONABLE", "DEFERRED_COOLDOWN")
        rev = {"status": "DEFERRED" if deferred else "NOT_NEEDED", "gate_decision": g["decision"],
               "explanation": GATE_EXPLANATION.get(g["decision"], g["decision"])}
    else:
        rev = {"status": "NONE", "explanation": None}
    pred = {}
    if rev["status"] != "NONE" and g and g.get("active_prediction"):
        pred = {"current_plan_predicted": {k: g["active_prediction"][k] for k in
                                           ("intervals_below_reserve", "ev_shortfall_model_kwh")},
                "recomputed_plan_predicted": {k: g["candidate_prediction"][k] for k in
                                              ("intervals_below_reserve", "ev_shortfall_model_kwh")}}
    return {"execution_discrepancy": {"detected": bool(row), "interval_index": row["interval_index"] if row else None,
                                      "reason": row["reason"] if row else None,
                                      # True when the plan now active was accepted after this discrepancy
                                      "resolved_by_accepted_revision": bool(row) and row["interval_index"] < since},
            "revision": {**rev, **pred}}


def operator_intervention(run_id):
    """Unresolved service risk that only the operator can resolve (pending proposal first, else the active plan)."""
    for prop in pending(run_id):
        oi = prop["options"][0].get("operator_intervention")
        if oi:
            return {**oi, "source": "pending_proposal", "proposal_id": prop["proposal_id"]}
    c = active_commitment(run_id)
    oi = c["body"]["option"].get("operator_intervention") if c else None
    return {**oi, "source": "active_commitment", "commitment_id": c["commitment_id"]} if oi else None


def last_revision_gate(run_id):
    """Latest gate decision for the run (WITHHELD or PROPOSED), so the UI can say why no revision was requested."""
    row = db.one("SELECT body, status FROM proposals WHERE run_id=? AND status IN ('WITHHELD','PROPOSED','DELIVERED',"
                 "'ACCEPTED','SUPERSEDED','EXPIRED') ORDER BY created_at DESC, rowid DESC LIMIT 1", run_id)
    if not row:
        return None
    g = json.loads(row["body"]).get("revision_gate")
    return {**g, "proposal_status": row["status"]} if g else None


def expire_proposals(run_id):
    db.ex("UPDATE proposals SET status='EXPIRED' WHERE run_id=? AND status IN ('PROPOSED','DELIVERED') AND expires_at<?",
          run_id, iso(now()))


def pending(run_id):
    expire_proposals(run_id)
    rows = db.q("SELECT body, status FROM proposals WHERE run_id=? AND status IN ('PROPOSED','DELIVERED') ORDER BY revision",
                run_id)
    return [{**json.loads(x["body"]), "status": x["status"]} for x in rows]


def active_commitment(run_id):
    row = db.one("SELECT * FROM commitments WHERE run_id=? AND status='ACCEPTED'", run_id)
    if not row:
        return None
    c = dict(row)
    c["body"] = json.loads(c["body"])
    return c


# ---------- events (operator) ----------
@app.post("/api/v1/events")
def post_event(b: dict = Depends(body), sess=Depends(session)):
    require(sess, "operator")

    def make():
        r = load_run(b.get("run_id"))
        kfi = b.get("known_from_interval")
        if not isinstance(kfi, int) or kfi < r["interval_index"]:
            raise ApiError(422, "INVALID_KNOWN_FROM", "known_from_interval must be an int >= current interval (no retroactive events)")
        kind = b.get("kind")
        eid = new_id("ev")
        if kind == "peak_advisory":
            rec = advisory_record(r, b, eid)
        elif kind == "allowance":
            kw = b.get("allowance_model_kw")
            if not isinstance(kw, (int, float)) or kw < 0:
                raise ApiError(422, "INVALID_ALLOWANCE", "allowance_model_kw must be >= 0")
            rec = {"event_id": eid, "kind": kind, "run_id": r["run_id"], "allowance_model_kw": kw,
                   "scope": "participating flexible loads only", "known_from_interval": kfi}
        elif kind == "outage":
            iv = b.get("intervals")
            if not isinstance(iv, list) or not all(isinstance(i, int) and kfi <= i < r["params"]["horizon_intervals"] for i in iv):
                raise ApiError(422, "INVALID_OUTAGE", "intervals must be ints in [known_from_interval, horizon)")
            rec = {"event_id": eid, "kind": kind, "run_id": r["run_id"], "intervals": iv, "announced": True,
                   "known_from_interval": kfi}
        else:
            raise ApiError(422, "INVALID_EVENT_KIND", "kind must be peak_advisory, allowance or outage")
        rec["issued_at"] = iso(now())
        db.ex("INSERT INTO events VALUES(?,?,?,?,?,?,?)", eid, r["run_id"], kind, kfi, 0, db.dumps(rec), rec["issued_at"])
        apply_due_events(r)
        save_run(r)
        return rec
    return idempotent("event", b.get("request_id"), b, make)


def advisory_record(r, b, eid):
    f = get_forecast(b.get("forecast_id"))
    if r["params"]["apartment_id"] not in (b.get("target_apartments") or []):
        raise ApiError(422, "INVALID_TARGETS", "target_apartments must include this run's apartment")
    vf, vu = parse_ts(b.get("valid_from"), "valid_from"), parse_ts(b.get("valid_until"), "valid_until")
    if vf >= vu:
        raise ApiError(422, "INVALID_VALIDITY", "valid_from must precede valid_until")
    fs, fe = parse_ts(f["forecast_start"], "fs"), parse_ts(f["forecast_end"], "fe")
    hours = {(p["start"], p["end"]) for p in f["points"]}
    wins = b.get("peak_windows")
    if not isinstance(wins, list) or not wins:
        raise ApiError(422, "INVALID_WINDOWS", "peak_windows required")
    norm = []
    for w in wins:
        ws, we = parse_ts(w.get("start"), "start"), parse_ts(w.get("end"), "end")
        if (iso(ws), iso(we)) not in hours or not (vf <= ws and we <= vu and fs <= ws and we <= fe):
            raise ApiError(422, "WINDOW_NOT_CONTAINED", "each window must be a forecast hour inside validity and forecast range")
        norm.append({"start": iso(ws), "end": iso(we)})
    return {"advisory_id": eid, "event_id": eid, "kind": "peak_advisory", "run_id": r["run_id"],
            "forecast_id": f["forecast_id"], "approved_by_demo_operator": "demo-operator",
            "geographic_scope": f["geographic_scope"], "valid_from": iso(vf), "valid_until": iso(vu),
            "peak_windows": sorted(norm, key=lambda w: w["start"]), "target_apartments": b["target_apartments"],
            "source_kind": f["source_kind"], "known_from_interval": b["known_from_interval"],
            "_note": "Prototype peak advisory from a regional forecast. Not a BESCOM instruction or a capacity limit."}


def apply_due_events(r):
    """Events become effective only once the clock reaches known_from_interval (no early information)."""
    due = db.q("SELECT * FROM events WHERE run_id=? AND applied=0 AND known_from_interval<=? ORDER BY created_at",
               r["run_id"], r["interval_index"])
    reasons = []
    for e in due:
        rec = json.loads(e["body"])
        if e["kind"] == "peak_advisory":
            r["active_advisory_id"] = rec["advisory_id"]
            reasons.append("PEAK_ADVISORY_ISSUED")
        elif e["kind"] == "allowance":
            r["params"]["allowance_model_kw"] = rec["allowance_model_kw"]
            reasons.append("ALLOWANCE_CHANGED")
        elif e["kind"] == "outage":
            for k in ("known_outage_intervals", "source_outage_intervals"):
                r["params"][k] = sorted(set(r["params"].get(k, [])) | set(rec["intervals"]))
            reasons.append("OUTAGE_ANNOUNCED")
        db.ex("UPDATE events SET applied=1 WHERE event_id=?", e["event_id"])
    if reasons:
        replan(r, reasons[-1])


# ---------- manager commands ----------
@app.post("/api/v1/requirements")
def post_requirements(b: dict = Depends(body), sess=Depends(session)):
    def make():
        r = load_run(b.get("run_id"))
        require(sess, "manager", r["params"]["apartment_id"])
        allowed = {"run_id", "request_id", "ev_sessions"}
        extra = set(b) - allowed
        if extra:  # e.g. allowance_model_kw: manager cannot change operator allowance
            raise ApiError(403 if "allowance_model_kw" in extra else 422, "FIELD_NOT_ALLOWED",
                           f"manager cannot set {sorted(extra)}")
        sessions = {s["ev_id"]: s for s in r["params"]["ev_sessions"]}
        for u in b.get("ev_sessions") or []:
            s = sessions.get(u.get("ev_id"))
            if not s:
                raise ApiError(404, "UNKNOWN_EV", f"ev {u.get('ev_id')} not in run")
            dep = u.get("departure_interval", s["departure_interval"])
            req = u.get("requested_model_kwh", s["requested_model_kwh"])
            if not isinstance(dep, int) or not r["interval_index"] <= dep <= r["params"]["horizon_intervals"] \
                    or dep <= s["arrival_interval"] or not isinstance(req, (int, float)) or req < 0:
                raise ApiError(422, "INVALID_REQUIREMENT", "departure must be future/within horizon; energy >= 0")
            s["departure_interval"], s["requested_model_kwh"] = dep, req
        prop = replan(r, "REQUIREMENT_CHANGED")
        save_run(r)
        return {"revision": r["revision"], "proposal": prop, "warnings": r.get("_warnings", [])}
    return idempotent("requirements", b.get("request_id"), b, make)


def load_proposal(pid):
    row = db.one("SELECT * FROM proposals WHERE proposal_id=?", pid)
    if not row:
        raise ApiError(404, "UNKNOWN_PROPOSAL", f"proposal {pid} not found")
    return dict(row)


@app.post("/api/v1/proposals/{pid}/delivered")
def proposal_delivered(pid: str, sess=Depends(session)):
    with tx():
        p = load_proposal(pid)
        require(sess, "manager", p["apartment_id"])
        expire_proposals(p["run_id"])
        db.ex("UPDATE proposals SET status='DELIVERED' WHERE proposal_id=? AND status='PROPOSED'", pid)
        return {"proposal_id": pid, "status": load_proposal(pid)["status"], "note": "Rendered only; not accepted or executed."}


class StaleProposal(Exception):
    """Acceptance refused because the clock passed part of the plan and the rest no longer meets its promise."""


def refresh_stale(pid, run_id):
    with tx():
        db.ex("UPDATE proposals SET status='SUPERSEDED' WHERE proposal_id=?", pid)
        r = load_run(run_id)
        replan(r, "PROPOSAL_STALE_AT_ACCEPTANCE")
        save_run(r)
        return r["revision"]


@app.post("/api/v1/proposals/{pid}/decision")
def proposal_decision(pid: str, b: dict = Depends(body), sess=Depends(session)):
    try:
        return _proposal_decision(pid, b, sess)
    except StaleProposal as e:
        rev = refresh_stale(pid, e.args[0])
        raise ApiError(409, "PROPOSAL_STALE", "the clock passed part of this plan and the rest no longer meets what it "
                       "promised; a refreshed proposal is pending", rev)


def _proposal_decision(pid, b, sess):
    def make():
        p = load_proposal(pid)
        require(sess, "manager", p["apartment_id"])
        r = load_run(p["run_id"])
        if b.get("run_id") != p["run_id"]:
            raise ApiError(409, "WRONG_RUN", "proposal belongs to another run", r["revision"])
        expire_proposals(p["run_id"])
        p = load_proposal(pid)
        if p["status"] not in ("PROPOSED", "DELIVERED"):
            raise ApiError(409, "STALE_PROPOSAL", f"proposal is {p['status']}; refresh before deciding", r["revision"])
        if b.get("expected_revision") != p["revision"]:
            raise ApiError(409, "STALE_PROPOSAL", "Refresh before accepting this version.", r["revision"])
        prop = json.loads(p["body"])
        opt = next((o for o in prop["options"] if o["option_id"] == b.get("option_id")), None)
        if not opt:
            raise ApiError(422, "UNKNOWN_OPTION", "option_id not in proposal")
        decision = b.get("decision")
        if decision == "DECLINE":
            db.ex("UPDATE proposals SET status='DECLINED' WHERE proposal_id=?", pid)
        elif decision == "ACCEPT":
            if opt.get("requires_operator_exception"):
                raise ApiError(409, "OPERATOR_EXCEPTION_REQUIRED", "option exceeds allowance; operator must decide")
            eff = max(opt["effective_from_interval"], r["interval_index"])
            if eff > opt["effective_from_interval"]:
                # The clock passed part of this plan before acceptance. Re-check what the rest delivers from the
                # current measured state; if it is materially worse than promised, refuse and offer a refreshed plan.
                ps, reqs, cons = plan_inputs(r)
                now_pred = revision.predict(opt["actions"], ps, cons, reqs, known_events(r))
                promised_res = opt["water_shortfall"]["intervals_below_reserve"]
                promised_ev = sum(opt["ev_shortfall_model_kwh"].values())
                if now_pred["intervals_below_reserve"] > promised_res or \
                        revision._wh(now_pred["ev_shortfall_model_kwh"]) - revision._wh(promised_ev) >= revision.EV_TOL_WH:
                    raise StaleProposal(r["run_id"])  # this transaction rolls back; the refresh happens after it
            db.ex("UPDATE commitments SET status='SUPERSEDED', ended_at_interval=? WHERE run_id=? AND status='ACCEPTED'",
                  eff, r["run_id"])
            cid = new_id("c")
            db.ex("INSERT INTO commitments VALUES(?,?,?,?,?,?,?,?,?,?)", cid, r["run_id"], p["apartment_id"], pid,
                  opt["option_id"], "ACCEPTED", eff, None, iso(now()), db.dumps({"option": opt}))
            db.ex("UPDATE proposals SET status='ACCEPTED' WHERE proposal_id=?", pid)
            r["revision"] += 1
            save_run(r)
        else:
            raise ApiError(400, "INVALID_DECISION", "decision must be ACCEPT or DECLINE")
        return state_doc(r["run_id"])
    return idempotent("decision:" + pid, b.get("request_id"), b, make)


def action_at(commitment, i):
    for a in commitment["body"]["option"]["actions"]:
        if a["interval_index"] == i:
            return a
    return None


@app.post("/api/v1/execution/claims")
def post_claim(b: dict = Depends(body), sess=Depends(session)):
    def make():
        r = load_run(b.get("run_id"))
        require(sess, "manager", r["params"]["apartment_id"])
        row = db.one("SELECT * FROM commitments WHERE commitment_id=? AND run_id=?", b.get("commitment_id"), r["run_id"])
        if not row:
            raise ApiError(404, "UNKNOWN_COMMITMENT", "commitment not found for this run")
        c = {**dict(row), "body": json.loads(row["body"])}
        i = b.get("interval_index")
        end = c["ended_at_interval"] if c["ended_at_interval"] is not None else r["params"]["horizon_intervals"]
        if not isinstance(i, int) or not c["effective_from_interval"] <= i < end or i > r["interval_index"]:
            raise ApiError(409, "CLAIM_OUT_OF_RANGE", "interval must be inside the commitment and not in the future", r["revision"])
        a = action_at(c, i)
        valid_ids = {f"{c['commitment_id']}:{i}:pump", f"{c['commitment_id']}:{i}:ev"}
        if b.get("action_id") not in valid_ids:
            raise ApiError(422, "UNKNOWN_ACTION", f"action_id must be one of {sorted(valid_ids)}")
        cid = new_id("cl")
        db.ex("INSERT INTO claims VALUES(?,?,?,?,?,?)", cid, r["run_id"], c["commitment_id"], i, b["action_id"], iso(now()))
        # If this interval was already observed as a deviation, the claim is contradicted now.
        ex_row = db.one("SELECT observation_status FROM executions WHERE run_id=? AND commitment_id=? AND interval_index=?",
                        r["run_id"], c["commitment_id"], i)
        if ex_row and ex_row["observation_status"] == "OBSERVED_DEVIATION" and not pending(r["run_id"]):
            replan(r, "CLAIM_WITHOUT_OBSERVED_EXECUTION")
            save_run(r)
        return {"claim_id": cid, "commitment_id": c["commitment_id"], "interval_index": i, "action_id": b["action_id"],
                "planned_action": a, "status": "CLAIMED_EXECUTED",
                "note": "Claim recorded only. Completion requires independent observation."}
    return idempotent("claim", b.get("request_id"), b, make)


# ---------- simulation step (operator) ----------
USABLE = ("VALID", "LATE")  # LATE = real reading that arrived after its interval; never shown as live


def judge(r, c, i, obs, prev_tank):
    """Compare one interval's observation with the committed action. Returns (status, reason)."""
    if obs["quality"] not in USABLE:
        return "STALE", "No valid observation for this interval."
    a = action_at(c, i) or {"pump": "OFF", "ev": "OFF"}
    p, problems = r["params"], []
    late = " (late reading)" if obs["quality"] == "LATE" else ""
    water_ok = obs["tank_l"] - prev_tank + p["withdrawal_profile_l"][i] >= 0.5 * p["pump_inflow_lph"] * p["model_dt_minutes"] / 60 \
        or obs["tank_l"] >= p["tank_max_l"] - 1
    if obs.get("measurement_scope") == "aggregate":
        # One meter on the participating circuits: zero proves all off; non-zero cannot be attributed to a branch.
        want_any = a["pump"] == "ON" or a["ev"] != "OFF"
        if obs["aggregate_model_kw"] is None:  # the combined channel is missing: electrical state unknown
            if a["pump"] == "ON" and not water_ok:
                return "OBSERVED_DEVIATION", "PUMP_WATER_DELIVERY_NOT_OBSERVED,COMBINED_CHANNEL_MISSING" + late
            return "AMBIGUOUS", "COMBINED_CHANNEL_MISSING" + late
        if obs["aggregate_model_kw"] <= EV_ON_KW:
            return ("OBSERVED_DEVIATION", "NO_PARTICIPATING_LOAD_OBSERVED" + late) if want_any else ("OBSERVED_MATCH", late.strip())
        if a["pump"] == "ON" and not water_ok:  # the independent tank sensor still applies
            return "OBSERVED_DEVIATION", "PUMP_WATER_DELIVERY_NOT_OBSERVED" + late
        return "AMBIGUOUS", "AGGREGATE_ONLY_BRANCH_UNATTRIBUTED" + late
    unknown = []
    if obs["pump_model_kw"] is None:  # pump channel missing: pump power unknown, the tank sensor still counts
        unknown.append("PUMP_CHANNEL_MISSING")
        if a["pump"] == "ON" and not water_ok:
            problems.append("PUMP_WATER_DELIVERY_NOT_OBSERVED")
    else:
        pump_obs = obs["pump_model_kw"] > 0
        if a["pump"] == "ON":
            if not pump_obs:
                problems.append("PUMP_NOT_OBSERVED_ON")
            elif not water_ok:
                problems.append("PUMP_POWER_WITHOUT_WATER_DELIVERY")
        elif pump_obs:
            problems.append("PUMP_ON_NOT_PLANNED")
    if obs["ev_model_kw"] is None:
        unknown.append("EV_CHANNEL_MISSING")
    else:
        ev_obs = obs["ev_model_kw"] > EV_ON_KW
        if a["ev"] != "OFF" and not ev_obs:
            problems.append("EV_CHARGING_NOT_OBSERVED")
        if a["ev"] == "OFF" and ev_obs:
            problems.append("EV_CHARGING_NOT_PLANNED")
    if problems:
        return "OBSERVED_DEVIATION", ",".join(problems + unknown) + late
    if unknown:
        return "AMBIGUOUS", ",".join(unknown) + late
    return "OBSERVED_MATCH", late.strip()


def present_ev(p, i):
    """EV sessions declared present at interval i (requirements known to the planner, not simulator truth)."""
    return [s["ev_id"] for s in p["ev_sessions"] if s["arrival_interval"] <= i < s["departure_interval"]]


# One wording per source kind, shared by the meter card label and the report's provenance (they must agree).
SOURCE_LABELS = {
    "synthetic": "Simulated meter: synthetic readings, not a measurement",
    "hardware": "Physical meter: unscaled measurements (model values equal the meter's own readings)",
    "scaled_hardware": "Scaled from bench measurements; model values, not field measurements.",
}


def scaled_label(ps, vs):
    """Short label for scaled_hardware that names each scale, so volume-only scaling never reads as electrical scaling.
    web/ui.js fmtSource builds the same sentence from the same two factors."""
    e = "unscaled (×1)" if ps == 1 else f"model-scaled (×{ps:g})"
    w = "unscaled (×1)" if vs == 1 else f"model-scaled (×{vs:g})"
    return f"Bench measurements: electrical measurements {e}; water volume {w}. Scaled values are model values, not field measurements."


def meter_block(p, readings, kind):
    # power_scale / volume_scale: device-to-model factors; null on simulator runs (no device). The meter measures
    # electricity, so its label follows the electrical scale: volume-only scaling leaves the meter readings unscaled.
    telemetry = p["observation_source"] == "telemetry"
    ps = p["telemetry_power_scale"] if telemetry else None
    electrical = "hardware" if kind == "scaled_hardware" and ps == 1 else kind
    return {"meter_id": readings[0]["meter_id"] if readings else p.get("meter_id"), "source_kind": kind,
            "electrical_source_kind": electrical,
            "label": SOURCE_LABELS.get(electrical, f"Unrecognised source: {electrical}"),
            "power_scale": ps, "volume_scale": p["telemetry_volume_scale"] if telemetry else None,
            "channels": readings, "total": meter.channel_total(readings)}


def electrical_fields(p, i, readings, scope):
    """Observation power fields from meter channels. Missing channel -> null (unknown), never zero."""
    by = {r["channel_id"]: r for r in readings}
    total = meter.channel_total(readings)
    if scope == "aggregate":
        agg = by[meter.COMBINED]["average_power_model_kw"]
        return {"pump_model_kw": None, "ev_model_kw": None, "ev_id": None, "aggregate_model_kw": agg,
                "branch_attribution": "UNKNOWN" if agg is None else "AMBIGUOUS" if agg > EV_ON_KW else "AGGREGATE_ZERO"}
    pump, ev = by[meter.PUMP]["average_power_model_kw"], by[meter.EV_GROUP]["average_power_model_kw"]
    present = present_ev(p, i)
    # The EV channel measures the charger group. Its energy is one vehicle's only when exactly one EV session is
    # declared present and the group draws no more than one charger can (+10 %); otherwise it stays group energy
    # (planning_state reports it as unattributed). Group energy never establishes a vehicle's state of charge.
    one_charger = ev is not None and ev <= p["ev_charger_model_kw"] * 1.1
    ev_id = present[0] if ev is not None and ev > EV_ON_KW and len(present) == 1 and one_charger else None
    return {"pump_model_kw": pump, "ev_model_kw": ev, "ev_id": ev_id, "aggregate_model_kw": total["average_power_model_kw"],
            "branch_attribution": "SYNTHETIC_KNOWN" if total["complete"] else "INCOMPLETE"}


def make_observation(p, rec, received_interval, quality):
    base = {"interval_index": rec["interval_index"], "received_at_interval": received_interval, "quality": quality,
            "source_kind": p["source_kind"], "measurement_scope": p["observation_scope"], "tank_l": rec["tank_l"],
            "received_at": iso(now())}
    readings = [{**x, "quality": "LATE" if quality == "LATE" and x["quality"] == "VALID" else x["quality"]}
                for x in rec["meter_readings"]]
    elec = electrical_fields(p, rec["interval_index"], readings, p["observation_scope"])
    out = {**base, **elec, "meter": meter_block(p, readings, p["source_kind"])}
    if rec.get("whole_connection_reading") is not None:
        out["whole_connection"] = whole_connection_block(p, rec["whole_connection_reading"], elec, quality)
    return out


def simulate_meter(r, i, device_kw, missing=(), restart=False):
    """The simulated meter's readings for interval i, from actual device power. Its state lives with the plant."""
    p = r["params"]
    st = r["truth"].setdefault("meter", meter.new_state(p["meter_id"]))
    if restart:
        meter.restart(st)
    rng = random.Random(f"{r['run_id']}:{i}:meter")
    draws = {c["channel_id"]: rng.gauss(0, 1) for c in p["meter_channels"]}
    return meter.read_interval(st, p["meter_channels"], iso(interval_time(r, i)), iso(interval_time(r, i + 1)),
                               p["model_dt_minutes"] / 60, device_kw, p["meter_power_noise_sd"], draws, missing)


def commitment_at(run_id, i):
    row = db.one("SELECT * FROM commitments WHERE run_id=? AND effective_from_interval<=? AND status IN ('ACCEPTED','SUPERSEDED') "
                 "AND (ended_at_interval IS NULL OR ended_at_interval>?) ORDER BY accepted_at DESC LIMIT 1", run_id, i, i)
    return {**dict(row), "body": json.loads(row["body"])} if row else None


def tank_before(r, i):
    rows = [o for o in observed(r) if o["quality"] in USABLE and o["interval_index"] < i]
    return rows[-1]["tank_l"] if rows else r["params"]["tank_init_l"]


def record_observation(r, obs):
    """Store an observation and judge it against the commitment that covered its interval."""
    run_id, i = r["run_id"], obs["interval_index"]
    annotate_recovery(r, obs)
    db.ex("INSERT OR REPLACE INTO observations VALUES(?,?,?)", run_id, i, db.dumps(obs))
    c = commitment_at(run_id, i)
    if not c:
        return None
    status, reason = judge(r, c, i, obs, tank_before(r, i))
    db.ex("INSERT OR REPLACE INTO executions VALUES(?,?,?,?,?)", run_id, c["commitment_id"], i, status, reason)
    return {"commitment_id": c["commitment_id"], "interval_index": i, "observation_status": status, "reason": reason}


def telemetry_observation(r, i):
    """Close interval i from device readings received since the previous close (telemetry runs).

    Raw bench values stay in raw_bench (W, Wh over actual device seconds); model values use explicit scales.
    """
    p = r["params"]
    cursor = r["truth"].get("telemetry_cursor", 0)
    rows = db.q("SELECT id, status, body FROM telemetry WHERE run_id=? AND id>? ORDER BY id", r["run_id"], cursor)
    if rows:
        r["truth"]["telemetry_cursor"] = rows[-1]["id"]
    samples = [json.loads(x["body"]) for x in rows if x["status"] in ("ACCEPTED", "NEW_DEVICE_SESSION")]
    excluded = len(rows) - len(samples)
    if not samples:
        return {"interval_index": i, "received_at_interval": None, "quality": "STALE", "source_kind": p["source_kind"],
                "measurement_scope": p["observation_scope"], "tank_l": None, "pump_model_kw": None, "ev_model_kw": None,
                "ev_id": None, "aggregate_model_kw": None, "branch_attribution": "UNKNOWN", "received_at": None,
                "raw_bench": {"sample_count": 0, "excluded_samples": excluded}}
    kinds = {s["source_kind"] for s in samples}
    hw = "hardware" in kinds
    source_kind = ("hardware" if p["telemetry_power_scale"] == 1 and p["telemetry_volume_scale"] == 1 else "scaled_hardware") \
        if hw else "synthetic"
    branch = p["observation_scope"] == "branch" and all(
        s.get("pump_power_w") is not None and s.get("ev_load_power_w") is not None for s in samples)

    def integrate(field):
        """Time-weighted mean W and Wh, using device monotonic deltas within one device session."""
        wh, secs = 0.0, 0.0
        for a_, b_ in zip(samples, samples[1:]):
            if a_["device_session_id"] != b_["device_session_id"] or a_.get(field) is None:
                continue  # reboot: no elapsed time is known across sessions
            dt = (b_["device_monotonic_ms"] - a_["device_monotonic_ms"]) / 1000
            if dt > 0:
                wh += a_[field] * dt / 3600
                secs += dt
        vals = [s[field] for s in samples if s.get(field) is not None]
        if not vals:
            return None, None, secs
        mean = wh * 3600 / secs if secs > 0 else sum(vals) / len(vals)
        return mean, (round(wh, 4) if secs > 0 else None), secs

    agg_w, agg_wh, secs = integrate("aggregate_power_w")
    pump_w, pump_wh, _ = integrate("pump_power_w") if branch else (None, None, 0)
    ev_w, ev_wh, _ = integrate("ev_load_power_w") if branch else (None, None, 0)
    ps = p["telemetry_power_scale"] / 1000
    tank = [s["tank_volume_l"] for s in samples if s.get("tank_volume_l") is not None]
    present = [s["ev_id"] for s in p["ev_sessions"] if s["arrival_interval"] <= i < s["departure_interval"]]
    ev_kw = None if ev_w is None else round(ev_w * ps, 4)
    obs = {"interval_index": i, "received_at_interval": i, "quality": "VALID", "source_kind": source_kind,
           "measurement_scope": "branch" if branch else "aggregate",
           "tank_l": round(tank[-1] * p["telemetry_volume_scale"], 2) if tank else None,
           "aggregate_model_kw": None if agg_w is None else round(agg_w * ps, 4),
           "pump_model_kw": None if pump_w is None else round(pump_w * ps, 4),
           "ev_model_kw": ev_kw,
           # one charger: energy is attributable only when exactly one EV session is present
           "ev_id": present[0] if branch and len(present) == 1 and ev_kw and ev_kw > EV_ON_KW else None,
           "branch_attribution": "BRANCH_METERS" if branch else
           ("AMBIGUOUS" if (agg_w or 0) * ps > EV_ON_KW else "AGGREGATE_ZERO"),
           "received_at": iso(now()),
           "raw_bench": {"sample_count": len(samples), "excluded_samples": excluded, "duration_s": round(secs, 3),
                         "device_sessions": sorted({s["device_session_id"] for s in samples}),
                         "mean_aggregate_w": agg_w and round(agg_w, 2), "aggregate_wh": agg_wh,
                         "mean_pump_w": pump_w and round(pump_w, 2), "pump_wh": pump_wh,
                         "mean_ev_load_w": ev_w and round(ev_w, 2), "ev_load_wh": ev_wh,
                         "last_tank_volume_l": tank[-1] if tank else None,
                         "power_scale": p["telemetry_power_scale"], "volume_scale": p["telemetry_volume_scale"]}}
    readings = telemetry_meter(r, i, samples)
    if readings is not None:  # the device carries a multi-channel meter: its channels are the electrical observation
        scope = p["observation_scope"]
        obs.update(electrical_fields(p, i, readings, scope), measurement_scope=scope,
                   meter=meter_block(p, readings, source_kind))
        if scope == "branch" and obs["branch_attribution"] == "SYNTHETIC_KNOWN":
            obs["branch_attribution"] = "BRANCH_METERS"
    if obs["tank_l"] is None:  # no tank reading: water evidence is unknown, not assumed
        obs["quality"] = "STALE"
    return obs


def _ms(ts):
    return int(parse_ts(ts, "meter time").timestamp() * 1000)


def telemetry_meter(r, i, samples):
    """Interval channel readings from device meter samples: energy counters (kWh) with the meter's own clock.

    Energy is counter increase; nothing is time-scaled. Durations come from the meter's clock: simulated timestamps
    (meter_clock=simulated, the emulator) or the device monotonic clock (meter_clock=device, a physical meter), never
    from network arrival time.
    - Reference for the increase: this channel's last reading in interval i-1; else, if the counter session started
      at zero inside this interval (known only on simulated time), that start; else only this interval's readings
      (energy_basis energy_counter_within_interval: the energy before the first reading is not counted here).
    - interval_energy_model_kwh = counter increase x telemetry_power_scale; average_power_model_kw = that energy over
      the covered time. covered_from_utc / covered_to_utc give the span (simulated clock only).
    - cumulative_energy_model_kwh = the counter x telemetry_power_scale (the meter's own total since its session).
    telemetry_power_scale is the existing explicit bench-to-model conversion (default 1; hardware at 1 is unscaled);
    raw counter values are kept unscaled. A channel with no data is MISSING (null), never zero. None if no meter.
    """
    p = r["params"]
    with_meter = [s for s in samples if isinstance(s.get("meter"), dict)]
    if not with_meter:
        return None
    simulated = p["meter_clock"] == "simulated"
    cursor = r["truth"].setdefault("meter_cursor", {})
    start, end = iso(interval_time(r, i)), iso(interval_time(r, i + 1))
    start_ms = _ms(start)
    ps = p["telemetry_power_scale"]

    def device_points(cid):
        pts = []
        for s in with_meter:
            m = s["meter"]
            ch = next((c for c in m.get("channels", []) if c.get("channel_id") == cid), None)
            if ch is None or ch.get("energy_kwh") is None:
                continue
            if simulated:
                t, t0 = _ms(m["sample_time_utc"]), _ms(m["session_start_utc"]) if m.get("session_start_utc") else None
            else:
                t, t0 = s["device_monotonic_ms"], 0  # the device clock restarts with the session
            pts.append({"sess": m.get("meter_session", s["device_session_id"]), "t": t, "t0": t0,
                        "kwh": float(ch["energy_kwh"]), "seq": s["seq_num"], "time_utc": m.get("sample_time_utc")})
        return pts

    def one(cid, sources):
        """sources: device channel ids making up this run channel (non-overlapping)."""
        kwh, secs, restarts, last, basis, first_t = 0.0, 0.0, 0, None, "energy_counter", None
        for src in sources:
            pts = device_points(src)
            if not pts:
                return {"quality": "MISSING", "missing_device_channels": [src]}
            ref = cursor.get(src)
            if ref is not None and ref["interval"] == i - 1:
                refpt = (ref["sess"], ref["t"], ref["kwh"], ref["t0"])
                frm = ref.get("time_utc")
            elif simulated and pts[0]["t0"] is not None and pts[0]["t0"] >= start_ms:
                refpt = (pts[0]["sess"], pts[0]["t0"], 0.0, pts[0]["t0"])  # counter was zero at the session start
                frm = iso(datetime.fromtimestamp(pts[0]["t0"] / 1000, timezone.utc))
            else:
                refpt, frm = None, pts[0]["time_utc"]
                basis = "energy_counter_within_interval"
            seq = ([refpt] if refpt else []) + [(x["sess"], x["t"], x["kwh"], x["t0"] or 0) for x in pts]
            e, sec, rs = meter.register_energy(seq)
            cursor[src] = {**{k: pts[-1][k] for k in ("sess", "t", "kwh", "t0", "time_utc")}, "interval": i}
            kwh, restarts, last = kwh + e, restarts + rs, pts[-1]
            secs = max(secs, sec)
            first_t = frm if first_t is None else first_t
        if secs <= 0:  # a single reading with no reference: no duration is known, so no energy or power is claimed
            return {"quality": "MISSING", "reason": "single reading without a reference; duration unknown",
                    "meter_session": last["sess"], "sequence_number": last["seq"]}
        cum = sum(device_points(src)[-1]["kwh"] for src in sources)
        return {"quality": "VALID", "average_power_model_kw": round(kwh * ps / (secs / 3600), 4),
                "interval_energy_model_kwh": round(kwh * ps, 5), "cumulative_energy_model_kwh": round(cum * ps, 5),
                "meter_session": last["sess"], "sequence_number": last["seq"], "counter_restarts": restarts,
                "energy_basis": basis, "covered_s": round(secs, 3),
                "covered_from_utc": first_t if simulated else None, "covered_to_utc": last["time_utc"] if simulated else None,
                "raw_counter_increase_kwh": round(kwh, 6), "raw_counter_kwh": round(cum, 6)}

    meter_id = with_meter[-1]["meter"].get("meter_id", p["meter_id"])
    out = []
    for ch in p["meter_channels"]:
        cid = ch["channel_id"]
        sources = [cid]
        if cid == meter.COMBINED and not device_points(cid):  # aggregate run on a branch-metered device
            sources = [meter.PUMP, meter.EV_GROUP]
        out.append({"meter_id": meter_id, "channel_id": cid, "label": ch.get("label", cid), "devices": list(ch["devices"]),
                    "grouped": len(ch["devices"]) > 1, "interval_start_utc": start, "interval_end_utc": end,
                    "average_power_model_kw": None, "interval_energy_model_kwh": None,
                    "cumulative_energy_model_kwh": None, "meter_session": None, "sequence_number": None,
                    **one(cid, sources)})
    return out


def check_meter_sample(run, b):
    """Reject meter samples whose clock or counters are inconsistent with the run, instead of storing totals that
    could be wrong by the emulator's speed factor. Called inside the telemetry transaction."""
    m = b.get("meter")
    if m is None:
        return
    p, i = run["params"], run["interval_index"]
    if not isinstance(m, dict) or not isinstance(m.get("channels"), list):
        raise ApiError(422, "INVALID_METER", "meter must be an object with a channels list")
    if m.get("clock") not in ("device", "simulated"):
        raise ApiError(422, "INVALID_METER", "meter.clock must be device or simulated")
    if b["source_kind"] == "hardware" and m["clock"] != "device":
        raise ApiError(422, "METER_CLOCK_MISMATCH", "a hardware meter reports measured energy on its own clock; "
                       "simulated time is only for the emulator")
    if m["clock"] != p["meter_clock"]:
        raise ApiError(422, "METER_CLOCK_MISMATCH",
                       f"this run expects meter_clock={p['meter_clock']} but the sample says {m['clock']}; "
                       f"create the run with apartment_params.meter_clock={m['clock']}")
    for ch in m["channels"]:
        e = ch.get("energy_kwh")
        if e is not None and (not isinstance(e, (int, float)) or e < 0):
            raise ApiError(422, "INVALID_METER", "energy_kwh must be a number >= 0")
    if m["clock"] != "simulated":
        return
    for k in ("sample_time_utc", "session_start_utc"):
        if not m.get(k):
            raise ApiError(422, "INVALID_METER", f"meter.{k} is required on simulated time")
    t, t0 = _ms(m["sample_time_utc"]), _ms(m["session_start_utc"])
    lo, hi = _ms(iso(interval_time(run, i))), _ms(iso(interval_time(run, i + 1)))
    if not lo < t <= hi:
        raise ApiError(422, "METER_TIME_OUTSIDE_INTERVAL",
                       f"meter sample_time_utc {m['sample_time_utc']} is not in the run's current interval {i} "
                       f"({iso(interval_time(run, i))} to {iso(interval_time(run, i + 1))}); the emulator's speed or "
                       "clock start does not match this run")
    if t0 > t:
        raise ApiError(422, "INVALID_METER", "session_start_utc is after sample_time_utc")
    prev = db.one("SELECT body FROM telemetry WHERE device_id=? AND device_session_id=? AND seq_num<? "
                  "AND status IN ('ACCEPTED','NEW_DEVICE_SESSION') ORDER BY seq_num DESC LIMIT 1",
                  b["device_id"], b["device_session_id"], b["seq_num"])
    pm = json.loads(prev["body"]).get("meter") if prev else None
    if not pm or pm.get("meter_session") != m.get("meter_session"):
        return
    dt_h = (t - _ms(pm["sample_time_utc"])) / 3_600_000
    if dt_h <= 0:
        raise ApiError(422, "METER_TIME_NOT_INCREASING", "meter sample_time_utc must increase within a session")
    before = {c["channel_id"]: c.get("energy_kwh") for c in pm["channels"]}
    for ch in m["channels"]:
        a, e = before.get(ch["channel_id"]), ch.get("energy_kwh")
        if a is None or e is None or ch.get("power_w") is None:
            continue
        got, want = e - a, ch["power_w"] / 1000 * dt_h
        if max(got, want) > 1e-6 and not 0.5 <= (got / want if want > 0 else float("inf")) <= 2.0:
            raise ApiError(422, "METER_COUNTER_INCONSISTENT",
                           f"channel {ch['channel_id']}: counter rose {got:.6f} kWh over {dt_h * 60:.3f} simulated min "
                           f"while reporting {ch['power_w']} W ({want:.6f} kWh expected); counters must accumulate "
                           "simulated energy, not wall-clock energy")


def annotate_recovery(r, obs):
    """After one or more MISSING readings of a channel, the next reading's counter also holds the energy of the gap.

    That energy is reported once, as recovered_unassigned_model_kwh with the span it fell in. It is not spread over
    the missing intervals (their timing is unknown), not added to this interval's energy, and never credited to a
    vehicle. If the counter restarted inside the gap, energy before the restart is unknowable: the figure is a lower
    bound (restart_in_gap). Applies to simulator and telemetry readings alike.
    """
    known = r["truth"].setdefault("meter_known", {})
    i = obs["interval_index"]
    for ch in (obs.get("meter") or {}).get("channels", []):
        if ch["quality"] not in USABLE or ch.get("cumulative_energy_model_kwh") is None:
            continue
        cid, prev = ch["channel_id"], known.get(ch["channel_id"])
        if prev and prev["interval"] < i - 1:
            same = prev["meter_session"] == ch["meter_session"]
            base = prev["cumulative_energy_model_kwh"] if same else 0.0
            ch["recovered_after_missing"] = {
                "recovered_unassigned_model_kwh": round(max(0.0, ch["cumulative_energy_model_kwh"] - base
                                                            - (ch["interval_energy_model_kwh"] or 0)), 5),
                "span_start_utc": prev.get("to_utc") or iso(interval_time(r, prev["interval"] + 1)),
                "span_end_utc": ch.get("covered_from_utc") or ch["interval_start_utc"],
                "missing_intervals": i - 1 - prev["interval"],
                "restart_in_gap": not same, "lower_bound": not same,
                "note": "energy in the gap; timing within it unknown; not assigned to an interval or a vehicle"}
        if prev is None or i > prev["interval"]:
            known[cid] = {"interval": i, "meter_session": ch["meter_session"], "to_utc": ch.get("covered_to_utc"),
                          "cumulative_energy_model_kwh": ch["cumulative_energy_model_kwh"]}


@app.post("/api/v1/runs/{run_id}/step")
def step_run(run_id: str, b: dict = Depends(body), sess=Depends(session)):
    require(sess, "operator")

    def make():
        r = load_run(run_id)
        p = r["params"]
        i = r["interval_index"]
        if i >= p["horizon_intervals"]:
            raise ApiError(409, "RUN_FINISHED", "horizon reached", r["revision"])
        if p["observation_source"] == "telemetry":
            if set(b) - {"request_id"}:
                raise ApiError(422, "TELEMETRY_RUN", "telemetry runs take no simulated switches or faults; the device reports them")
            verdicts = [v for v in [record_observation(r, telemetry_observation(r, i))] if v]
            r["interval_index"] = i + 1
            apply_due_events(r)
            dev = [v for v in verdicts if v["observation_status"] == "OBSERVED_DEVIATION"]
            if dev and not pending(run_id):
                claimed = db.one("SELECT 1 FROM claims WHERE run_id=? AND commitment_id=? AND interval_index=?",
                                 run_id, dev[0]["commitment_id"], i)
                replan(r, "CLAIM_WITHOUT_OBSERVED_EXECUTION" if claimed else "OBSERVED_DEVIATION")
            elif not dev:
                reconsider_deferred(r)
            save_run(r)
            return {"stepped_interval": i, "execution": verdicts[0] if verdicts else None, "late_execution": None,
                    "state": state_doc(run_id)}
        if b.get("drop_observation") and b.get("delay_observation"):
            raise ApiError(422, "INVALID_STEP", "choose drop_observation or delay_observation, not both")
        lost = b.get("drop_meter_channels") or []
        ids = {c["channel_id"] for c in p["meter_channels"]}
        if not isinstance(lost, list) or not set(lost) <= ids:
            raise ApiError(422, "INVALID_STEP", f"drop_meter_channels must list channel ids from {sorted(ids)}")
        sw = b.get("actual_switches") or {}
        for k in sw:
            if k not in ("pump", "ev") or sw[k] not in ("ON", "OFF"):
                raise ApiError(422, "INVALID_SWITCH", "actual_switches: {pump|ev: ON|OFF}")
        verdicts = []
        held = r["truth"].pop("held_reading", None)  # a reading in transit from an earlier interval
        if held:
            verdicts.append(record_observation(r, make_observation(p, held, i, "LATE")))
        pump_on = sw.get("pump", "ON" if r["truth"]["pump_on"] else "OFF") == "ON"
        ev_on = sw.get("ev", "ON" if r["truth"]["ev_on"] else "OFF") == "ON"
        r["truth"], rec = plant.step(r["truth"], p, i, pump_on, ev_on)
        rec["allowance_model_kw"] = p["allowance_model_kw"]
        rec["meter_readings"] = simulate_meter(r, i, rec["device_model_kw"], set(lost), bool(b.get("restart_meter")))
        if p.get("whole_connection"):
            rec["whole_connection_reading"] = synth_whole_connection(p, i, rec, bool(b.get("drop_whole_connection")))
        db.ex("INSERT INTO truth_log VALUES(?,?,?)", run_id, i, db.dumps(rec))
        if b.get("drop_observation") or b.get("delay_observation"):
            if b.get("delay_observation"):
                r["truth"]["held_reading"] = rec  # delivered as LATE at the next step
            gap = {"interval_index": i, "received_at_interval": None, "quality": "STALE", "source_kind": p["source_kind"],
                   "measurement_scope": p["observation_scope"], "tank_l": None, "pump_model_kw": None, "ev_model_kw": None,
                   "ev_id": None, "aggregate_model_kw": None, "branch_attribution": "UNKNOWN", "received_at": None,
                   "meter": meter_block(p, [{**x, "quality": "MISSING", "average_power_model_kw": None,
                                             "interval_energy_model_kwh": None, "cumulative_energy_model_kwh": None}
                                            for x in rec["meter_readings"]], p["source_kind"])}
            verdicts.append(record_observation(r, gap))
        else:
            verdicts.append(record_observation(r, make_observation(p, rec, i, "VALID")))
        verdicts = [v for v in verdicts if v]
        r["interval_index"] = i + 1
        apply_due_events(r)
        dev = [v for v in verdicts if v["observation_status"] == "OBSERVED_DEVIATION"]
        if dev and not pending(run_id):
            v = dev[-1]
            claimed = db.one("SELECT 1 FROM claims WHERE run_id=? AND commitment_id=? AND interval_index=?",
                             run_id, v["commitment_id"], v["interval_index"])
            replan(r, "CLAIM_WITHOUT_OBSERVED_EXECUTION" if claimed else "OBSERVED_DEVIATION")
        elif not dev:
            reconsider_deferred(r)
        save_run(r)
        cur = next((v for v in verdicts if v["interval_index"] == i), None)
        late = next((v for v in verdicts if v["interval_index"] != i), None)
        return {"stepped_interval": i, "execution": cur, "late_execution": late, "state": state_doc(run_id)}
    return idempotent("step:" + run_id, b.get("request_id"), b, make)


# ---------- telemetry (device) ----------
REQUIRED_TELEMETRY = ("run_id", "apartment_id", "device_id", "device_session_id", "seq_num", "source_kind")


@app.post("/api/v1/telemetry")
def post_telemetry(b: dict = Depends(body), sess=Depends(session)):
    require(sess, "device")
    missing = [k for k in REQUIRED_TELEMETRY if k not in b]
    if missing:
        raise ApiError(422, "INVALID_TELEMETRY", f"missing {missing}")
    if b["device_id"] != sess["device_id"]:
        raise ApiError(403, "DEVICE_MISMATCH", "token belongs to another device")
    if b["source_kind"] not in ("hardware", "synthetic"):
        raise ApiError(422, "INVALID_SOURCE_KIND", "telemetry source_kind must be hardware or synthetic")
    if not isinstance(b["seq_num"], int) or b["seq_num"] < 0:
        raise ApiError(422, "INVALID_SEQ", "seq_num must be a non-negative int")
    with tx():
        run = load_run(b["run_id"])
        if b["apartment_id"] != run["params"]["apartment_id"]:
            raise ApiError(422, "WRONG_APARTMENT", "telemetry apartment_id does not match the run")
        if not isinstance(b.get("device_monotonic_ms"), int) or b["device_monotonic_ms"] < 0:
            raise ApiError(422, "INVALID_TELEMETRY", "device_monotonic_ms must be a non-negative int")
        dup = db.one("SELECT id FROM telemetry WHERE device_id=? AND device_session_id=? AND seq_num=?",
                     b["device_id"], b["device_session_id"], b["seq_num"])
        if dup:
            return {"status": "DUPLICATE", "telemetry_id": dup["id"]}
        check_meter_sample(run, b)
        last = db.one("SELECT device_session_id, MAX(seq_num) m FROM telemetry WHERE device_id=? AND device_session_id=?",
                      b["device_id"], b["device_session_id"])
        known_session = last["m"] is not None
        any_prev = db.one("SELECT 1 FROM telemetry WHERE device_id=?", b["device_id"])
        status = "OUT_OF_ORDER" if known_session and b["seq_num"] < last["m"] else \
                 "NEW_DEVICE_SESSION" if any_prev and not known_session else "ACCEPTED"
        db.ex("INSERT INTO telemetry(run_id, device_id, device_session_id, seq_num, received_at, status, body) VALUES(?,?,?,?,?,?,?)",
              b["run_id"], b["device_id"], b["device_session_id"], b["seq_num"], iso(now()), status, db.dumps(b))
        tid = db.one("SELECT last_insert_rowid() i")["i"]
    return {"status": status, "telemetry_id": tid,
            "note": "Raw bench units stored as received; not converted into model kW/kWh."}


NO_PER_EV_VERIFICATION = ("NO_PER_VEHICLE_MEASUREMENT: the EV meter channel measures the charger group; per-EV "
                          "figures are allocations inferred from declared sessions, not verification")


def ev_energy_summary(p, obs, ps):
    """EV energy kept in three separate kinds: measured (charger-group channel), allocated (inferred per EV) and
    verified per EV (none: no per-vehicle measurement exists). Recovered gap energy is measured but unassigned."""
    dt_h = p["model_dt_minutes"] / 60
    usable = [o for o in obs if o["quality"] in USABLE]
    agg = p["observation_scope"] == "aggregate"
    group = None if agg else round(sum(ev_interval_kwh(o, dt_h) for o in usable if o.get("ev_model_kw") is not None), 4)
    recovered = 0.0
    for o in usable:
        for ch in (o.get("meter") or {}).get("channels", []):
            if ch["channel_id"] == meter.EV_GROUP and ch.get("recovered_after_missing"):
                recovered += ch["recovered_after_missing"]["recovered_unassigned_model_kwh"]
    alloc = None if agg else ps["ev_delivered_observed_model_kwh"]
    return {
        "measured_group_model_kwh": group,
        "measured_group_basis": None if agg else "sum of EV charger-group channel interval energies (usable readings)",
        "allocated_by_ev_model_kwh": alloc,
        "allocation_basis": None if agg else "inferred: exactly one EV session declared present and the group drew no "
                                             "more than one charger's rating (+10 %); not a measurement of the vehicle",
        "unallocated_group_model_kwh": None if agg else round(group - sum(alloc.values()), 4),
        "recovered_unassigned_model_kwh": None if agg else round(recovered, 4),
        "intervals_ev_channel_missing": sum(1 for o in obs if not agg and o.get("ev_model_kw") is None),
        "verified_by_ev_model_kwh": None,
        "verified_reason": "AGGREGATE_ONLY_METERING" if agg else NO_PER_EV_VERIFICATION,
    }


def latest_raw(run_id):
    row = db.one("SELECT received_at, status, body FROM telemetry WHERE run_id=? AND status!='OUT_OF_ORDER' ORDER BY id DESC LIMIT 1",
                 run_id)
    return {**json.loads(row["body"]), "received_at": row["received_at"], "ingest_status": row["status"]} if row else None


# ---------- state and report ----------
def state_doc(run_id):
    r = load_run(run_id)
    p, ps = r["params"], planning_state(r)
    obs = observed(r)
    c = active_commitment(run_id)
    adv = active_advisory(r)
    i = r["interval_index"]
    last_obs = obs[-1] if obs else None
    execution = None
    if c:
        ex_row = db.one("SELECT * FROM executions WHERE run_id=? AND commitment_id=? ORDER BY interval_index DESC LIMIT 1",
                        run_id, c["commitment_id"])
        claims = db.q("SELECT interval_index, action_id, claimed_at FROM claims WHERE commitment_id=? ORDER BY claimed_at",
                      c["commitment_id"])
        ex_i = ex_row["interval_index"] if ex_row else None
        execution = {"commitment_id": c["commitment_id"], "interval_index": ex_i,
                     "claim": "CLAIMED_EXECUTED" if any(x["interval_index"] == ex_i for x in claims) else "NO_CLAIM",
                     "observation_status": ex_row["observation_status"] if ex_row else "UNOBSERVED",
                     "reason": ex_row["reason"] if ex_row else "", "claims": [dict(x) for x in claims],
                     "next_action": action_at(c, i)}
    warnings = ["SYNTHETIC apartment simulation. Not a field measurement.",
                "Regional forecast and advisory do not establish local transformer or apartment capacity."]
    if ps["aggregate_only"]:
        warnings.append("Aggregate-only metering: branch execution and per-EV energy are not verified; the tank sensor is separate.")
    if ps["stale"]:
        warnings.append("Latest observation is STALE; planning uses the last valid observation.")
    warnings += r.get("_warnings", [])
    remaining = {s["ev_id"]: round(max(0.0, s["requested_model_kwh"] - ps["ev_delivered_observed_model_kwh"].get(s["ev_id"], 0)), 4)
                 for s in p["ev_sessions"]}
    return {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "revision": r["revision"], "mode": "advisory",
        # simulator: steps take simulated switches; telemetry: a device (or the emulator) reports and closes intervals
        "observation_source": p["observation_source"],
        "meter_clock": p["meter_clock"] if p["observation_source"] == "telemetry" else None,
        "clock": {"interval_index": i, "model_dt_minutes": p["model_dt_minutes"], "horizon_intervals": p["horizon_intervals"],
                  "model_time_utc": iso(interval_time(r, i)), "clock_start_utc": r["clock_start_utc"], "source_kind": "synthetic"},
        "apartment": {"apartment_id": p["apartment_id"], "allowance_model_kw": p["allowance_model_kw"],
                      "allowance_scope": "participating flexible loads (pump + EV-charging emulator) only",
                      "allowance_label": "configured participating-load limit (model kW); not BESCOM-sanctioned load "
                                         "or transformer capacity",
                      "model_tank_l": ps["tank_l"], "model_tank_min_l": p["tank_min_l"], "model_tank_max_l": p["tank_max_l"],
                      "source_water_available": i not in p.get("known_outage_intervals", []),
                      "pump_state": "UNKNOWN" if ps["stale"] or ps["pump_on"] is None else ("ON" if ps["pump_on"] else "OFF"),
                      "pump_starts_observed": ps["pump_starts_history"],
                      "ev_sessions": [{**s, "remaining_model_kwh": None if ps["aggregate_only"] else remaining[s["ev_id"]]}
                                      for s in p["ev_sessions"]]},
        "active_commitment": None if not c else {"commitment_id": c["commitment_id"], "proposal_id": c["proposal_id"],
                                                 "option_id": c["option_id"], "status": c["status"],
                                                 "effective_from_interval": c["effective_from_interval"],
                                                 "accepted_at": c["accepted_at"], "option": c["body"]["option"]},
        "pending_proposals": pending(run_id),
        "scheduling": scheduling_block(r),
        "observation": None if not last_obs else {**last_obs, "model_power_kw": last_obs["aggregate_model_kw"],
                                                  "raw_bench": last_obs.get("raw_bench") or latest_raw(run_id),
                                                  "latest_device_reading": latest_raw(run_id)},
        "execution": execution,
        "execution_history": execution_history(run_id),
        "readiness": {"water_reserve_status": "UNKNOWN" if ps["stale"] else
                      ("ABOVE_MINIMUM" if ps["tank_l"] >= p["tank_min_l"] else "BELOW_MINIMUM"),
                      # fixture fields keep their v0.1 types; per-EV detail is additive
                      "charging_status": "UNVERIFIED" if ps["aggregate_only"] else
                      ("MET" if all(v <= 1e-9 for v in remaining.values()) else "PENDING"),
                      "charging_status_by_ev": {k: ("UNVERIFIED" if ps["aggregate_only"] else "MET" if v <= 1e-9 else "PENDING")
                                                for k, v in remaining.items()},
                      # No source in this system measures energy per vehicle (no per-vehicle meter or vehicle
                      # interface), so nothing is labelled verified per EV. See ev_energy for what is known.
                      "verified_ev_delivered_model_kwh": None,
                      "verified_ev_delivered_by_ev_model_kwh": None,
                      "verified_ev_unavailable_reason": "AGGREGATE_ONLY_METERING: combined reading cannot be attributed to an EV"
                      if ps["aggregate_only"] else NO_PER_EV_VERIFICATION,
                      "charging_status_basis": None if ps["aggregate_only"] else "allocation (inferred, not verified)",
                      "ev_energy": ev_energy_summary(p, obs, ps)},
        "warnings": warnings,
        "revision_gate": last_revision_gate(run_id),
        "operator_intervention": operator_intervention(run_id),
        "discrepancy_status": discrepancy_status(run_id),
        "forecast_id": adv["forecast_id"] if adv else None,
        "active_advisory": adv,
    }


def execution_history(run_id):
    """Every judged interval and every claim across all commitments (claims kept even when contradicted)."""
    ex = {(e["commitment_id"], e["interval_index"]): dict(e) for e in
          db.q("SELECT commitment_id, interval_index, observation_status, reason FROM executions WHERE run_id=?", run_id)}
    claims = db.q("SELECT commitment_id, interval_index, action_id, claimed_at FROM claims WHERE run_id=?", run_id)
    out = {k: {**v, "claims": []} for k, v in ex.items()}
    for cl in claims:
        key = (cl["commitment_id"], cl["interval_index"])
        out.setdefault(key, {"commitment_id": key[0], "interval_index": key[1], "observation_status": "UNOBSERVED",
                             "reason": "", "claims": []})["claims"].append({"action_id": cl["action_id"], "claimed_at": cl["claimed_at"]})
    obs = {o["interval_index"]: o for o in (json.loads(x["body"]) for x in
           db.q("SELECT body FROM observations WHERE run_id=?", run_id))}
    for v in out.values():
        o = obs.get(v["interval_index"])
        v["quality"] = o["quality"] if o else None
        v["received_at_interval"] = o.get("received_at_interval") if o else None
    return sorted(out.values(), key=lambda v: (v["interval_index"], v["commitment_id"]))


@app.get("/api/v1/runs")
def list_runs(sess=Depends(session)):
    require(sess, "operator")
    rows = db.q("SELECT run_id, params, interval_index, revision, clock_start_utc, created_at FROM runs ORDER BY created_at DESC")
    return {"runs": [{"run_id": x["run_id"], "apartment_id": json.loads(x["params"])["apartment_id"],
                      "observation_scope": json.loads(x["params"])["observation_scope"],
                      "observation_source": json.loads(x["params"])["observation_source"],
                      "meter_clock": json.loads(x["params"]).get("meter_clock", "device"), "interval_index": x["interval_index"],
                      "revision": x["revision"], "clock_start_utc": x["clock_start_utc"], "created_at": x["created_at"]}
                     for x in rows]}


@app.get("/api/v1/state")
def get_state(run_id: str, apartment_id: str, sess=Depends(session)):
    with tx():
        r = load_run(run_id)
        if r["params"]["apartment_id"] != apartment_id:
            raise ApiError(404, "UNKNOWN_APARTMENT", "apartment not in run")
        if sess["role"] != "operator":
            require(sess, "manager", apartment_id)
        return state_doc(run_id)


@app.get("/api/v1/operator/state")
def get_operator_state(run_id: str, sess=Depends(session)):
    require(sess, "operator")
    with tx():
        s = state_doc(run_id)
        events = [json.loads(e["body"]) | {"applied": bool(e["applied"])} for e in
                  db.q("SELECT body, applied FROM events WHERE run_id=? ORDER BY created_at", run_id)]
        n = s["clock"]["interval_index"]
        valid = db.one("SELECT COUNT(*) n FROM observations WHERE run_id=? AND (body LIKE '%\"quality\":\"VALID\"%' OR body LIKE '%\"quality\":\"LATE\"%')", run_id)["n"]
        return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "revision": s["revision"], "clock": s["clock"],
                "observation_source": s["observation_source"],
                "participating_scope": "pump + EV-charging emulator per apartment; background load unmetered (unknown)",
                "apartments": [{"apartment_id": s["apartment"]["apartment_id"], "allowance_model_kw": s["apartment"]["allowance_model_kw"],
                                "latest_aggregate_model_kw": (s["observation"] or {}).get("aggregate_model_kw"),
                                "pending_proposals": len(s["pending_proposals"]),
                                "active_commitment_id": (s["active_commitment"] or {}).get("commitment_id"),
                                "execution": s["execution"]}],
                "events": events, "forecast_id": s["forecast_id"], "active_advisory": s["active_advisory"],
                "observation_coverage": {"intervals_elapsed": n, "valid_observations": valid}, "warnings": s["warnings"]}


@app.get("/api/v1/runs/{run_id}/report")
def report(run_id: str, sess=Depends(session)):
    if sess["role"] not in ("operator", "manager"):
        raise ApiError(403, "FORBIDDEN_ROLE", "operator or manager required")
    from simulation.metrics import run_metrics
    with tx():
        r = load_run(run_id)
        if sess["role"] == "manager":
            require(sess, "manager", r["params"]["apartment_id"])
        truth = [json.loads(x["body"]) for x in db.q("SELECT body FROM truth_log WHERE run_id=? ORDER BY interval_index", run_id)]
        obs = observed(r)
        telemetry_run = r["params"]["observation_source"] == "telemetry"
        if telemetry_run:  # no simulator truth exists: outcomes are what the meters measured (usable intervals only)
            truth = [{**o, "allowance_model_kw": r["params"]["allowance_model_kw"], "pump_model_kw": o["pump_model_kw"] or 0,
                      "ev_model_kw": o["ev_model_kw"] or 0, "unmet_water_l": None}
                     for o in obs if o["quality"] in USABLE and o["aggregate_model_kw"] is not None]
        peaks = set(known_events(r)["peak_intervals"])
        m = run_metrics(r["params"], truth, peaks)
        cnt = lambda sql: db.one(sql, run_id)["n"]
        m["counts"] = {"proposals": cnt("SELECT COUNT(*) n FROM proposals WHERE run_id=?"),
                       "acceptances": cnt("SELECT COUNT(*) n FROM commitments WHERE run_id=?"),
                       "claims": cnt("SELECT COUNT(*) n FROM claims WHERE run_id=?"),
                       "observed_deviations": cnt("SELECT COUNT(*) n FROM executions WHERE run_id=? AND observation_status='OBSERVED_DEVIATION'"),
                       "claims_contradicted": cnt("SELECT COUNT(*) n FROM claims c JOIN executions e ON e.commitment_id=c.commitment_id "
                                                  "AND e.interval_index=c.interval_index WHERE c.run_id=? AND e.observation_status='OBSERVED_DEVIATION'")}
        m["observation_coverage"] = {"intervals_elapsed": len(truth), "valid": sum(o["quality"] in USABLE for o in obs),
                                     "late": sum(o["quality"] == "LATE" for o in obs),
                                     "measurement_scope": r["params"]["observation_scope"]}
        kind = next((o["source_kind"] for o in reversed(obs) if o["quality"] in USABLE), "synthetic")
        if telemetry_run:
            m["observation_coverage"]["intervals_elapsed"] = r["interval_index"]
            m["unmet_withdrawal_l"] = None  # a tank sensor cannot see unmet demand, before or after the first reading
        # "measured" only for unscaled real device readings; scaled bench values and emulator readings never say so
        ps, vs = r["params"]["telemetry_power_scale"], r["params"]["telemetry_volume_scale"]
        plant_txt = {"hardware": "device telemetry, measured by hardware (unscaled)",
                     "scaled_hardware": "device telemetry from bench measurements; electrical measurements "
                                        + (f"model-scaled (x{ps:g})" if ps != 1 else "unscaled (x1)")
                                        + "; water volume " + (f"model-scaled (x{vs:g})" if vs != 1 else "unscaled (x1)")
                                        + "; scaled quantities are model values, not field measurements"}.get(
            kind, "device telemetry from the emulator (synthetic readings, not measurements)")
        p_kind = kind if telemetry_run else "synthetic"
        m["provenance"] = {"source_kind": p_kind,
                           "label": scaled_label(ps, vs) if p_kind == "scaled_hardware" else SOURCE_LABELS.get(p_kind),
                           "plant": plant_txt + "; unmet water not observable" if telemetry_run
                           else "simulation.plant (evaluator truth)",
                           "advisory_id": r["active_advisory_id"], "computed_at": iso(now()),
                           "meter": None if not telemetry_run else {
                               "meter_clock": r["params"]["meter_clock"],
                               "energy": "counter increases in kWh; no time scaling",
                               "durations": "simulated timestamps" if r["params"]["meter_clock"] == "simulated"
                               else "device clock (monotonic)",
                               "telemetry_power_scale": ps, "telemetry_volume_scale": vs}}
        dt_h = r["params"]["model_dt_minutes"] / 60
        ground = None
        if not telemetry_run:  # evaluator-only simulator truth, from the plant's own record of which EV charged
            ground = {s["ev_id"]: 0.0 for s in r["params"]["ev_sessions"]}
            for rec in truth:
                if rec.get("ev_id"):
                    ground[rec["ev_id"]] = round(ground[rec["ev_id"]] + rec["ev_model_kw"] * dt_h, 4)
        m["ev_energy_accounting"] = {
            "ground_truth_delivered_by_ev_model_kwh": ground,
            "ground_truth_basis": "simulator truth (evaluator only)" if ground is not None else
            "none: a telemetry run has no simulator truth",
            **ev_energy_summary(r["params"], obs, planning_state(r)),
            "note": "ground truth, measured group energy, inferred allocation and verified per-EV energy are separate; "
                    "outcome metrics above use " + ("simulator truth" if ground is not None else "usable observations")}
        if sess["role"] == "manager":  # simulator truth is evaluator-only
            return {"counts": m["counts"], "observation_coverage": m["observation_coverage"],
                    "provenance": {**m["provenance"], "plant": None},
                    "scope": "manager: counts and coverage only; truth-based outcomes are operator/evaluator views"}
        return m


# ---------- experiments (simulated policy comparison) ----------
@app.post("/api/v1/experiments")
def post_experiment(b: dict = Depends(body), sess=Depends(session)):
    require(sess, "operator")
    from simulation import experiment

    def make():
        seeds = b.get("seeds_per_scenario", 20)
        if not isinstance(seeds, int) or not 1 <= seeds <= 50:
            raise ApiError(422, "INVALID_SEEDS", "seeds_per_scenario must be 1..50")
        peaks = None
        if b.get("run_id"):  # use that run's active advisory hours
            r = load_run(b["run_id"])
            peaks = known_events(r)["peak_intervals"]
        try:
            rows, summary, cfg = experiment.run(b.get("scenarios"), b.get("policies"), seeds, peaks)
        except ValueError as e:
            raise ApiError(422, "INVALID_EXPERIMENT", str(e))
        eid, d = experiment.export(rows, summary, cfg)
        doc = json.loads((d / "summary.json").read_text())
        doc.update({"status": "COMPLETED", "runs_csv": str(d / "runs.csv"), "runs": rows})
        db.ex("INSERT INTO experiments VALUES(?,?,?)", eid, db.dumps(doc), iso(now()))
        return {"experiment_id": eid, "status": "COMPLETED", "runs": len(rows)}
    return idempotent("experiment", b.get("request_id"), b, make)


@app.get("/api/v1/experiments/{eid}")
def get_experiment(eid: str, sess=Depends(session)):
    row = db.one("SELECT body FROM experiments WHERE experiment_id=?", eid)
    if not row:
        raise ApiError(404, "UNKNOWN_EXPERIMENT", f"experiment {eid} not found")
    return json.loads(row["body"])


# ---------- billing (additive): sourced KERC rules; illustrative unless built from a bill or unscaled physical meter ----------
BILLING_INPUT_KEYS = {"category", "period_start", "period_end", "energy_kwh", "energy_basis", "sanctioned_load_kw",
                      "recorded_md_kw", "contract_demand_kva", "recorded_md_kva", "fppca_paise_per_kwh", "include_tax",
                      "fy2024_25_kwh", "scope"}
# the current scenario; billing-scenario-v1 is kept on disk as the superseded record
BILLING_SCENARIO = ROOT / "results" / "billing_scenario" / "billing-scenario-v2" / "summary.json"


@app.get("/api/v1/billing/tariffs")
def billing_tariffs(sess=Depends(session)):
    if sess["role"] not in ("operator", "manager"):
        raise ApiError(403, "FORBIDDEN_ROLE", "operator or manager required")
    return billing_calc.TARIFFS


@app.post("/api/v1/billing/calculate")
def billing_calculate(b: dict = Depends(body), sess=Depends(session)):
    """Stateless: the inputs are neither stored nor logged (they may come from a private bill)."""
    require(sess, "operator")
    unknown = set(b) - BILLING_INPUT_KEYS
    if unknown:
        raise ApiError(422, "UNKNOWN_FIELD", f"unknown billing field(s) {sorted(unknown)}")
    return billing_calc.calculate(b)


@app.get("/api/v1/billing/scenario")
def billing_scenario(sess=Depends(session)):
    require(sess, "operator")
    if not BILLING_SCENARIO.exists():
        raise ApiError(404, "NO_BILLING_SCENARIO", "run python -m billing.scenario to produce the illustrative scenario")
    return json.loads(BILLING_SCENARIO.read_text(encoding="utf-8"))


@app.get("/api/v1/runs/{run_id}/billing")
def run_billing(run_id: str, category: str = "LT-1", sess=Depends(session)):
    """Energy-related charges of the participating circuits (pump + EV charger group) for the run so far.

    Never a connection bill: fixed/demand charges belong to the whole connection. Energy comes from the meter channels
    (summed per channel; the channel total is not added again). Unscaled physical meters use their raw counters;
    scaled bench values and simulated/emulated readings are illustrative. Intervals without a usable reading make the
    energy incomplete: no cost is calculated from a partial sum (missing is unknown, not zero)."""
    require(sess, "operator")
    with tx():
        r = load_run(run_id)
        obs = observed(r)
    p, n = r["params"], r["interval_index"]
    by = {o["interval_index"]: o for o in obs}
    kinds, missing, model_kwh, raw_kwh, recovered = set(), [], 0.0, 0.0, 0.0
    raw_complete = True
    for i in range(n):
        o = by.get(i)
        # a reading without a meter block (older per-branch telemetry) counts as missing here: no channel energy to sum
        chans = ((o or {}).get("meter") or {}).get("channels") if o and o.get("quality") in USABLE else None
        if not chans or any(c.get("interval_energy_model_kwh") is None for c in chans):
            missing.append(i)
            continue
        # the meter measures electricity: volume-only scaling leaves its readings unscaled (electrical_source_kind)
        kinds.add(o["meter"].get("electrical_source_kind") or o["source_kind"])
        for c in chans:
            model_kwh += c["interval_energy_model_kwh"]
            if c.get("raw_counter_increase_kwh") is None:
                raw_complete = False
            else:
                raw_kwh += c["raw_counter_increase_kwh"]
            if c.get("recovered_after_missing"):
                recovered += c["recovered_after_missing"]["recovered_unassigned_model_kwh"] or 0.0
    if not kinds:  # no usable reading at all: say so rather than guessing a provenance
        basis, energy, label = None, 0.0, "No usable meter readings yet"
    elif kinds == {"hardware"} and raw_complete:
        basis, energy, label = "measured_raw", raw_kwh, ("Measured participating circuits (unscaled physical meter): "
                                                         "not a whole-connection bill")
    elif "scaled_hardware" in kinds:
        basis, energy, label = "scaled_model", model_kwh, "Illustrative: scaled bench values, not field measurements"
    else:
        basis, energy, label = "simulated", model_kwh, "Illustrative simulation: synthetic readings, not measurements"
    start = (parse_ts(r["clock_start_utc"], "clock_start_utc") + timedelta(hours=5, minutes=30)).date()
    elapsed_h = n * p["model_dt_minutes"] / 60
    end = start + timedelta(days=max(1, -(-int(elapsed_h) // 24)))
    out = {"run_id": run_id, "label": label, "energy_basis": basis, "category": category,
           "category_note": "Assumed for illustration; the actual category comes from the connection's bill, never "
                            "from the equipment.", "intervals_elapsed": n, "elapsed_hours": elapsed_h,
           "intervals_missing": len(missing), "energy_kwh_known": round(energy, 4),
           "recovered_unassigned_kwh": round(recovered, 4),
           "recovered_note": "Energy recovered after lost readings is real but not placed in time; it is shown, not "
                             "added, and the calculation stays partial while any interval is missing.",
           "source_kinds": sorted(kinds)}
    if n == 0 or not kinds:
        out.update(status="not_estimable", calculation=None, energy_kwh_known=None,
                   reason="No interval has been closed yet." if n == 0 else "No interval has a usable meter reading.")
        return out
    if missing:
        out.update(status="partial", calculation=None,
                   reason=f"{len(missing)} of {n} intervals have no usable meter reading; their energy is unknown, so "
                          "no cost is calculated from the incomplete sum.")
        return out
    calc = billing_calc.calculate({"category": category, "period_start": start.isoformat(), "period_end": end.isoformat(),
                                   "energy_kwh": round(energy, 4), "energy_basis": basis,
                                   "scope": "participating_loads", "fppca_paise_per_kwh": None})
    out.update(status=calc["status"], calculation=calc)
    return out


# ---------- same-origin web assets (web/ is owned by the UI workstream) ----------
def _page(name):
    f = WEB / f"{name}.html"
    if f.exists():
        return FileResponse(f)
    return JSONResponse({"error": {"code": "UI_NOT_PRESENT", "message": f"web/{name}.html not yet provided"}}, status_code=404)


@app.get("/operator")
def operator_page():
    return _page("operator")


@app.get("/manager")
def manager_page():
    return _page("manager")


if WEB.exists():
    app.mount("/web", StaticFiles(directory=WEB), name="web")
