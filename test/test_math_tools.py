"""安全科学计算 Tool 测试。"""

import unittest

from math_tools import CalculatorError, calculate
from tools import create_read_only_registry


class MathToolTests(unittest.TestCase):
    def test_arithmetic_constants_and_power_alias(self):
        result = calculate("2^10 + sqrt(81) + round(pi, 2)")
        self.assertAlmostEqual(result["result"], 1036.14)
        self.assertEqual(result["normalizedExpression"], "2**10 + sqrt(81) + round(pi, 2)")

    def test_degree_mode_and_variables(self):
        result = calculate(
            "a * sin(theta) + cos(60)",
            variables={"a": 4, "theta": 30},
            angle_unit="degree",
        )
        self.assertAlmostEqual(result["result"], 2.5)

    def test_statistics_and_combinatorics(self):
        self.assertEqual(calculate("mean([1, 2, 3, 4])")["result"], 2.5)
        self.assertEqual(calculate("comb(10, 3) + factorial(5)")["result"], 240)

    def test_common_combination_aliases(self):
        expected = 184756
        self.assertEqual(calculate("C(20, 10)")["result"], expected)
        self.assertEqual(calculate("combination(20, 10)")["result"], expected)
        self.assertEqual(calculate("20 choose 10")["result"], expected)

    def test_complex_roots_support_i_and_negative_square_root(self):
        positive = calculate("(-1 + sqrt(-3)) / 2", precision=10)
        negative = calculate("(-1 - sqrt(3)*i) / 2", precision=10)
        coefficient = calculate("-1/2 + sqrt(3)/2 * 1i", precision=10)

        self.assertAlmostEqual(positive["result"]["real"], -0.5)
        self.assertAlmostEqual(positive["result"]["imag"], 3 ** 0.5 / 2)
        self.assertEqual(positive["formatted"], "-0.5 + 0.8660254038i")
        self.assertAlmostEqual(negative["result"]["imag"], -(3 ** 0.5 / 2))
        self.assertEqual(
            coefficient["normalizedExpression"],
            "-1/2 + sqrt(3)/2 * 1*i",
        )
        self.assertAlmostEqual(coefficient["result"]["imag"], 3 ** 0.5 / 2)

    def test_rejects_code_execution_and_resource_abuse(self):
        for expression in (
            "__import__('os').system('echo unsafe')",
            "(1).__class__",
            "[x for x in [1, 2]]",
            "2^10001",
            "factorial(2001)",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(CalculatorError):
                    calculate(expression)

    def test_registry_exposes_read_only_calculator(self):
        registry = create_read_only_registry()
        tool = registry.get("calculate")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.safety_level, "read_only")
        result = registry.execute(
            "calculate",
            {"expression": "hypot(3, 4)", "precision": 8},
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output["result"], 5.0)

    def test_invalid_arguments_fail_cleanly(self):
        with self.assertRaisesRegex(CalculatorError, "angle_unit"):
            calculate("1 + 1", angle_unit="gradian")
        with self.assertRaisesRegex(CalculatorError, "变量名"):
            calculate("1", variables={"not valid": 2})
        with self.assertRaisesRegex(CalculatorError, "有限实数"):
            calculate("1e309")


if __name__ == "__main__":
    unittest.main()
