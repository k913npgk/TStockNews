import unittest

from tstocknews.list_shadow import decide, financial_features, next_session_rises


class ListShadowTests(unittest.TestCase):
    def test_retention_and_breadth_hysteresis(self):
        ranked = [dict(symbol=f"{i:04}", market="twse", trend=1 if i<50 else -1) for i in range(100)]
        previous = dict(active=True, members=[ranked[39],ranked[40]])
        active, breadth, members = decide(ranked, previous)
        self.assertTrue(active)
        self.assertEqual(breadth,.5)
        self.assertEqual(members,[ranked[39],*ranked[:19]])
        self.assertEqual(decide(ranked,{})[2],[])
        for row in ranked[39:50]:
            row["trend"] = -1
        self.assertEqual(decide(ranked,previous)[2],[])
        for row in ranked[39:55]:
            row["trend"] = 1
        self.assertEqual(decide(ranked,{})[2],ranked[:20])

    def test_unmatched_eps_never_becomes_a_growth_observation(self):
        day = "2026-11-04"
        rows = [dict(symbol=s, market="twse") for s in ("1234","1235")]
        facts = dict(fundamentals=[dict(row, available_date=day, fiscal_period="2026-Q2",
                                      eps=2,revenue_yoy_3m=.15,revenue_yoy_prev_3m=.10) for row in rows])
        financial_features(rows, facts, day, lambda *_: ({"1234":(2,1),"1235":(3,1)}, {}))
        self.assertEqual(rows[0]["earnings"],1)
        self.assertIsNone(rows[1]["earnings"])

    def test_next_session_rise_counts_missing_as_unknown(self):
        rows = [dict(symbol=s, market="twse") for s in ("1234","1235")]
        quotes = {("twse","1234"):dict(open=10,close=11,low=10,high=11)}
        self.assertEqual(next_session_rises(rows,quotes),dict(up=1,known=1,total=2))
