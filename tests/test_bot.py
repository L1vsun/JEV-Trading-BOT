"""Run with ``python -m pytest`` or ``python -m unittest``."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

from jev_bot import features as F
from jev_bot.book import Market, load_csv
from jev_bot.decide import Policy, decide
from jev_bot.engine import RunConfig, run
from jev_bot.quant import RLS, FairValueModel
from jev_bot.questions import BUY_SIDE, QUESTION_IDS, SELL_SIDE, decision_questions
from jev_bot.sim import SimConfig, simulate
from jev_bot.state import FLOW, StateBuilder, bucket, value_of
from jev_bot.surrogate import JevSurrogate
from jev_bot.typesafe import Answer, Choice, JevClient, JevError, Noul, ResponseCache, Response, Score, confidence_from


def small_market(seconds: int = 3600, seed: int = 3) -> Market:
    return simulate(SimConfig(seconds=seconds, seed=seed))[0]


class Features(unittest.TestCase):
    def test_rolling_sum_matches_naive(self):
        x = np.random.default_rng(0).normal(size=200)
        got = F.rolling_sum(x, 7)
        want = np.array([x[max(0, i - 6): i + 1].sum() for i in range(200)])
        np.testing.assert_allclose(got, want, atol=1e-9)

    def test_ofi_sign(self):
        # the bid steps up with the ask unchanged: buying pressure -> positive OFI
        m = Market(np.arange(2.0), np.array([10.0, 10.01]), np.array([10.02, 10.02]),
                   np.array([[5.0], [7.0]]), np.array([[5.0], [5.0]]), np.zeros(2), np.zeros(2), np.zeros(2, int))
        self.assertGreater(F.ofi(m)[1], 0)

    def test_weighted_mid_inside_spread(self):
        m = small_market(600)
        wm = F.weighted_mid(m)
        self.assertTrue(np.all(wm >= m.bid - 1e-12) and np.all(wm <= m.ask + 1e-12))

    def test_vpin_bounds(self):
        v = F.vpin(small_market(1800))
        self.assertTrue(np.all((v >= 0) & (v <= 1)))

    def test_features_are_causal(self):
        m = small_market(2400)
        X1, _, _ = F.feature_matrix(m)
        cut = 1500
        m2 = m.slice(0, len(m))
        rng = np.random.default_rng(9)
        m2.bid = m.bid.copy(); m2.ask = m.ask.copy(); m2.buy_vol = m.buy_vol.copy()
        m2.bid[cut:] += rng.normal(0, 0.5, len(m) - cut)
        m2.ask[cut:] = m2.bid[cut:] + 0.01
        m2.buy_vol[cut:] *= 5
        X2, _, _ = F.feature_matrix(m2)
        np.testing.assert_allclose(X1[:cut], X2[:cut], atol=1e-9)


class Quant(unittest.TestCase):
    def test_rls_recovers_weights(self):
        rng = np.random.default_rng(1)
        w = np.array([0.5, -1.0, 2.0])
        r = RLS(3, lam=1.0, ridge=1e-3)
        for _ in range(2000):
            x = rng.normal(size=3)
            r.update(x, float(w @ x + rng.normal(0, 0.01)))
        np.testing.assert_allclose(r.w, w, atol=0.02)

    def test_fair_value_uses_only_realised_labels(self):
        m = small_market(2400)
        X, _, raw = F.feature_matrix(m)
        p1 = FairValueModel(X.shape[1], horizon=120).run(X, raw["mid"], raw["rv"], warmup=300)
        mid2 = raw["mid"].copy(); mid2[2000:] += 5.0      # change the future only
        p2 = FairValueModel(X.shape[1], horizon=120).run(X, mid2, raw["rv"], warmup=300)
        np.testing.assert_allclose(p1[:2000], p2[:2000], atol=1e-12)


class State(unittest.TestCase):
    def test_bucket_roundtrip(self):
        self.assertEqual(bucket(0.0, FLOW, (0.5, 1.2, 2.0)), "balanced")
        self.assertEqual(bucket(3.0, FLOW, (0.5, 1.2, 2.0)), "heavy buying")
        self.assertEqual(bucket(-0.7, FLOW, (0.5, 1.2, 2.0)), "light selling")
        self.assertAlmostEqual(value_of("heavy selling", FLOW), -1.0)

    def test_state_is_json_text_only(self):
        m = small_market(2400)
        X, _, raw = F.feature_matrix(m)
        st = StateBuilder(raw, np.zeros(len(m))).state(2000)
        json.dumps(st)
        leaves = []
        def walk(o):
            if isinstance(o, dict): [walk(v) for v in o.values()]
            elif isinstance(o, list): [walk(v) for v in o]
            else: leaves.append(o)
        walk(st)
        self.assertTrue(all(isinstance(x, str) for x in leaves))


class Surrogate(unittest.TestCase):
    def test_answers_match_question_types(self):
        m = small_market(2400)
        X, _, raw = F.feature_matrix(m)
        st = StateBuilder(raw, np.full(len(m), 1.5)).state(2000)
        qs = decision_questions()
        r = JevSurrogate().system_one(st, qs)
        self.assertEqual(set(r.answers), set(qs))
        for k, q in qs.items():
            a = r[k]
            self.assertEqual(a.type, q.to_json()["type"])
            if a.type == "noul":
                self.assertTrue(0 <= a.noul <= 1)
            else:
                self.assertAlmostEqual(sum(a.probabilities.values()), 1.0, places=2)
                self.assertTrue(0 <= a.confidence <= 1)

    def test_confidence_formula(self):
        self.assertAlmostEqual(confidence_from({"a": 1, "b": 0, "c": 0}), 1.0)
        self.assertAlmostEqual(confidence_from({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}), 0.0)


def fake(answers: dict) -> Response:
    return Response("test", {k: Answer.from_json(v) for k, v in answers.items()})


def _noul(value: float) -> dict:
    return {"type": "noul", "noul": value}


def _table(**overrides: float) -> dict:
    raw = {key: _noul(0.05) for key in QUESTION_IDS}
    for key in BUY_SIDE:
        raw[key] = _noul(0.9)
    raw.update({key: _noul(value) for key, value in overrides.items()})
    return raw


class Decide(unittest.TestCase):
    def test_all_gates_pass_buys_full_size(self):
        d = decide(0, fake(_table()), 0)
        self.assertEqual((d.action, d.size), ("buy", 1.0))
        self.assertTrue(all(d.gates.values()))
        self.assertEqual(d.composite, 1.0)

    def test_unsettled_band_holds(self):
        d = decide(0, fake(_table(tape_buys_up=0.5)), 0, Policy())
        self.assertEqual(d.action, "hold")
        self.assertIn("unsettled", d.reason)
        self.assertFalse(d.gates["settled"])

    def test_missing_answer_is_not_a_no(self):
        raw = _table()
        del raw["flow_up"]
        d = decide(0, fake(raw), 0)
        self.assertEqual(d.action, "hold")
        self.assertIn("missing", d.reason)
        self.assertIn("not read as no", d.reason)
        self.assertNotIn("flow_up no", d.reason)

    def test_liquidity_veto_holds(self):
        d = decide(0, fake(_table(spread_wide=0.9)), 0)
        self.assertEqual(d.action, "hold")
        self.assertIn("veto", d.reason)

    def test_sell_side_and_already_long(self):
        raw = {key: _noul(0.05) for key in QUESTION_IDS}
        for key in SELL_SIDE:
            raw[key] = _noul(0.9)
        d = decide(0, fake(raw), 0)
        self.assertEqual((d.action, d.size), ("sell", 1.0))
        held = decide(0, fake(_table()), 1)
        self.assertEqual(held.action, "hold")
        self.assertIn("already long", held.reason)

    def test_exit(self):
        d = decide(0, fake(_table(**{"exit_against_long": 0.9})), 1)
        self.assertEqual(d.action, "exit")


# ---------------------------------------------------------------- the HTTP client, against a local fake API
class _Handler(BaseHTTPRequestHandler):
    calls: list = []
    fail_first = True

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.calls.append((self.path, self.headers.get("Authorization"), body))
        if _Handler.fail_first:
            _Handler.fail_first = False
            self.send_response(429); self.send_header("retry-after", "0"); self.end_headers(); return
        answers = {}
        for k, q in body["questions"].items():
            if q["type"] == "noul":
                answers[k] = {"type": "noul", "noul": 0.7}
            elif q["type"] == "choice":
                opts = list(q["criteria"])
                answers[k] = {"type": "choice", "choice": opts[0], "probabilities": {o: (1.0 if i == 0 else 0.0) for i, o in enumerate(opts)}, "confidence": 1.0}
            else:
                answers[k] = {"type": "score", "score": 1.0, "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                              "probabilities": {str(i): (1.0 if i == 1 else 0.0) for i in range(len(q["criteria"]))}, "confidence": 1.0}
        out = json.dumps({"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 400, "output_tokens": 30}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(out)

    def do_GET(self):
        out = json.dumps({"models": [{"name": "jev-latest", "description": "x", "release_date": "2026-09-15"}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(out)


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_request_shape_retry_parse_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d) / "c.jsonl")
            c = JevClient(api_key="k", base_url=self.url, cache=cache, max_retries=2)
            qs = {"n": Noul("Is it up?", {"true": "yes", "false": "no"}), "c": Choice("Pick", {"a": None, "b": "B"}),
                  "s": Score("Rate", ["low", "mid", "high"])}
            r = c.system_one({"x": "y"}, qs)
            path, auth, body = _Handler.calls[-1]
            self.assertEqual(path, "/v1/systemone")
            self.assertEqual(auth, "Bearer k")
            self.assertEqual(body["model"], "jev-latest")
            self.assertEqual(body["questions"]["n"], {"type": "noul", "instructions": "Is it up?", "criteria": {"true": "yes", "false": "no"}})
            self.assertEqual(body["questions"]["s"]["criteria"], ["low", "mid", "high"])
            self.assertEqual(len(_Handler.calls), 2)             # one 429, one success
            self.assertEqual((r["n"].noul, r["c"].choice, r["s"].score, r.model), (0.7, "a", 1.0, "jev-1.13.0"))
            self.assertAlmostEqual(c.usage.cost_usd, 400 / 1e6 * 0.042)
            # replay from cache with no key and no network
            off = JevClient(base_url="http://127.0.0.1:1", cache=ResponseCache(Path(d) / "c.jsonl"), cache_only=True)
            self.assertTrue(off.system_one({"x": "y"}, qs).cached)
            with self.assertRaises(JevError):
                off.system_one({"x": "other"}, qs)
            self.assertEqual(c.models()[0]["name"], "jev-latest")
            self.assertEqual(c.last_payload["model"], "jev-latest")
            self.assertNotIn("Authorization", json.dumps(c.last_payload))

    def test_schema_validation(self):
        with self.assertRaises(ValueError):
            Score("x", ["only one"]).to_json()
        with self.assertRaises(ValueError):
            Choice("x", {"a": None}).to_json()


class EndToEnd(unittest.TestCase):
    def test_session_runs_and_fills_at_next_second(self):
        m = small_market(3 * 3600, seed=5)
        res = run(m, JevSurrogate(), RunConfig(step=15, n_random=50))
        self.assertGreater(len(res.decisions), 100)
        for tr in res.trades:
            self.assertEqual(tr.px_in, m.ask[tr.t_in] if tr.side > 0 else m.bid[tr.t_in])
            d = [x for x in res.decisions if x.t == tr.t_in - 1]
            self.assertTrue(d, "every fill must follow a decision one second earlier")

    def test_csv_roundtrip(self):
        m = small_market(300)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.csv"
            m.save_csv(p)
            m2 = load_csv(p)
            np.testing.assert_allclose(m.bid, m2.bid, atol=1e-4)
            self.assertEqual(m2.bid_sz.shape, m.bid_sz.shape)
            self.assertAlmostEqual(m2.tick, 0.01, places=6)


if __name__ == "__main__":
    unittest.main()
