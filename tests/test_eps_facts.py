import hashlib
import unittest

from tstocknews.eps_facts import extract_comparison


def context(ref, start, end, *, dimensional=False):
    dimension = '<xbrldi:explicitMember dimension="tifrs:Class">tifrs:Common</xbrldi:explicitMember>' if dimensional else ""
    return f'<xbrli:context id="{ref}"><xbrli:entity><xbrli:identifier scheme="x">2330</xbrli:identifier><xbrli:segment>{dimension}</xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>'


def unit(unit_id="TWD_PER_SHARE", numerator="iso4217:TWD", denominator="xbrli:shares"):
    return f'<xbrli:unit id="{unit_id}"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>{numerator}</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>{denominator}</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>'


def fact(value, context_ref, *, unit_ref="TWD_PER_SHARE", name="tifrs:BasicEarningsLossPerShare", extra=""):
    return f'<ix:nonFraction name="{name}" contextRef="{context_ref}" unitRef="{unit_ref}" {extra}>{value}</ix:nonFraction>'


def document(facts, *, units=None, contexts=None):
    contexts = contexts or [context("cur", "2026-01-01", "2026-06-30"), context("prior", "2025-01-01", "2025-06-30")]
    units = units or [unit()]
    return ("<html><head><ix:header>" + "".join(contexts + units) + "</ix:header><body>" +
            "".join(facts) + "</body></html>").encode()


class ExtractComparisonTests(unittest.TestCase):
    member = "filing-cr-2330-2026Q2.html"

    def extract(self, body, member_name=None):
        return extract_comparison(body, symbol="2330", year=2026, quarter=2,
                                  member_name=member_name or self.member)

    def test_reads_ytd_and_prior_ytd_from_same_non_dimensional_report(self):
        body = document([fact("12.5", "cur"), fact("10.25", "prior")])
        result = self.extract(body)
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual((result["eps_ytd"], result["eps_prior_ytd"]), (12.5, 10.25))
        self.assertEqual(result["statement_scope"], "consolidated")
        self.assertEqual(result["period_start"], "2026-01-01")
        self.assertEqual(result["period_end"], "2026-06-30")
        self.assertEqual(result["prior_period_start"], "2025-01-01")
        self.assertEqual(result["prior_period_end"], "2025-06-30")
        self.assertEqual(result["facts"]["eps_ytd"]["contextRef"], "cur")
        self.assertEqual(result["facts"]["eps_prior_ytd"]["contextRef"], "prior")
        self.assertEqual(result["facts"]["eps_ytd"]["unitRef"], "TWD_PER_SHARE")
        self.assertEqual(result["facts"]["eps_ytd"]["source_member_sha256"], hashlib.sha256(body).hexdigest())

    def test_applies_sign_and_scale(self):
        body = document([fact("1.25", "cur", extra='sign="-" scale="1"'), fact("2.5", "prior", extra='scale="-1"')])
        result = self.extract(body)
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual((result["eps_ytd"], result["eps_prior_ytd"]), (-12.5, 0.25))

    def test_requires_exact_basic_eps_local_name(self):
        body = document([
            fact("99", "cur", name="tifrs:BasicEarningsLossPerShareContinuingOperations"),
            fact("98", "cur", name="tifrs:DilutedEarningsLossPerShare"),
            fact("10", "prior"),
        ])
        result = self.extract(body)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["eps_ytd"])
        self.assertEqual(result["reason"], "FACT_NOT_FOUND")

    def test_requires_real_twd_per_share_unit_definition(self):
        body = document([fact("12", "cur", unit_ref="TWD"), fact("10", "prior", unit_ref="TWD")],
                        units=[unit("TWD", numerator="foo:TWD", denominator="foo:share")])
        result = self.extract(body)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["eps_ytd"])
        self.assertEqual(result["reason"], "INVALID_FACT")

    def test_nil_and_nonfinite_values_are_unknown(self):
        nil_body = document([fact("12", "cur", extra='xsi:nil="true"'), fact("10", "prior")])
        nil_result = self.extract(nil_body)
        self.assertEqual(nil_result["status"], "UNKNOWN")
        self.assertIsNone(nil_result["eps_ytd"])
        inf_body = document([fact("INF", "cur"), fact("10", "prior")])
        inf_result = self.extract(inf_body)
        self.assertEqual(inf_result["status"], "UNKNOWN")
        self.assertIsNone(inf_result["eps_ytd"])

    def test_dimensional_context_is_not_used(self):
        contexts = [context("cur", "2026-01-01", "2026-06-30", dimensional=True),
                    context("prior", "2025-01-01", "2025-06-30")]
        result = self.extract(document([fact("12", "cur"), fact("10", "prior")], contexts=contexts))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["eps_ytd"])
        self.assertEqual(result["eps_prior_ytd"], 10.0)

    def test_conflicting_duplicate_contexts_remain_unknown(self):
        contexts = [context("cur", "2026-01-01", "2026-06-30"),
                    context("cur2", "2026-01-01", "2026-06-30"),
                    context("prior", "2025-01-01", "2025-06-30")]
        result = self.extract(document([fact("12", "cur"), fact("13", "cur2"), fact("10", "prior")], contexts=contexts))
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["eps_ytd"])
        self.assertEqual(result["reason"], "CONFLICTING_FACTS")

    def test_conflicting_duplicate_context_id_period_or_dimensions_is_unknown(self):
        dimension_common = context("cur", "2026-01-01", "2026-06-30", dimensional=True)
        dimension_preferred = dimension_common.replace("tifrs:Common", "tifrs:Preferred")
        context_sets = (
            [context("cur", "2026-01-01", "2026-06-30"),
             context("cur", "2025-01-01", "2025-06-30"), context("prior", "2025-01-01", "2025-06-30")],
            [dimension_common, dimension_preferred, context("prior", "2025-01-01", "2025-06-30")],
        )
        for contexts in context_sets:
            result = self.extract(document([fact("12", "cur"), fact("10", "prior")], contexts=contexts))
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertIsNone(result["eps_ytd"])
            self.assertEqual(result["reason"], "CONFLICTING_CONTEXT_DEFINITIONS")

    def test_conflicting_duplicate_unit_id_is_unknown(self):
        body = document([fact("12", "cur"), fact("10", "prior")],
                        units=[unit(), unit("TWD_PER_SHARE", numerator="foo:TWD", denominator="xbrli:shares")])
        result = self.extract(body)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["eps_ytd"])
        self.assertEqual(result["reason"], "INVALID_FACT")

    def test_parentheses_are_negative_and_double_negative_is_rejected(self):
        good = self.extract(document([fact("(1.25)", "cur"), fact("2.5", "prior")]))
        self.assertEqual(good["status"], "KNOWN")
        self.assertEqual(good["eps_ytd"], -1.25)
        for raw, sign in (("-1.25", '-'), ("(1.25)", '-')):
            bad = self.extract(document([fact(raw, "cur", extra=f'sign="{sign}"'), fact("2.5", "prior")]))
            self.assertEqual(bad["status"], "UNKNOWN")
            self.assertIsNone(bad["eps_ytd"])

    def test_scale_must_be_a_short_integer(self):
        for scale in ("1.5", "1000", "999999999999999999999999999999999"):
            result = self.extract(document([fact("1", "cur", extra=f'scale="{scale}"'), fact("2", "prior")]))
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertIsNone(result["eps_ytd"])

    def test_member_must_match_symbol_exact_period_and_scope(self):
        for member in ("filing-cr-12330-2026Q2.html", "filing-cr-2330-2026Q3.html",
                       "filing-cr-2330-2026Q2-extra.html", "filing-cr-2330-2026Q2.htm",
                       "filing-ir-2330-2025Q2-cr-2330-2026Q2.html"):
            result = self.extract(document([fact("12", "cur"), fact("10", "prior")]), member)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["reason"], "MEMBER_NAME_MISMATCH")
        individual = self.extract(document([fact("12", "cur"), fact("10", "prior")]), "filing-ir-2330-2026Q2.html")
        self.assertEqual(individual["statement_scope"], "individual")


if __name__ == "__main__":
    unittest.main()
