import unittest

from scanner import calculate_token_received, calculate_trade_metrics


class TradeMetricsTests(unittest.TestCase):
    def test_buy_tax_reduces_tokens_before_the_return_quote(self):
        self.assertEqual(calculate_token_received(1_000, 0.125), 875)

    def test_trade_metrics_apply_sell_tax_slippage_and_both_gas_legs(self):
        quote_buy = {"gas": 100_000, "gasPrice": 1_000_000_000}
        quote_sell = {
            "amountOut": 6_000_000_000_000_000,
            "gas": 100_000,
            "gasPrice": 1_000_000_000,
        }

        metrics = calculate_trade_metrics(quote_buy, quote_sell, sell_tax=0.10)

        self.assertAlmostEqual(metrics["actual_bnb_out"], 0.0054)
        self.assertAlmostEqual(metrics["total_gas_bnb"], 0.0002)
        self.assertAlmostEqual(metrics["final_net_bnb"], 0.005173)
        self.assertAlmostEqual(metrics["net_profit_bnb"], 0.000173)
        self.assertAlmostEqual(metrics["net_roi_pct"], 3.46, places=2)
        self.assertAlmostEqual(metrics["theoretical_gross_pct"], 20.0)


if __name__ == "__main__":
    unittest.main()
