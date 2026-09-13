"""受限符号数学 Tool 测试。"""

import unittest

from symbolic_math import SymbolicMathError, symbolic_math
from context_builder import ContextBuilder
from tools import create_read_only_registry


class SymbolicMathTests(unittest.TestCase):
    def test_solves_equation_with_implicit_multiplication(self):
        result = symbolic_math("solve", "x^2 - 5x + 6 = 0")
        self.assertEqual(result["exact"], [{"x": 2}, {"x": 3}])
        self.assertIn("\\left", result["latex"])

    def test_calculus_and_algebra_operations(self):
        derivative = symbolic_math("diff", "sin(x) * exp(x)")
        self.assertIn("exp(x)*sin(x)", derivative["exact"])
        integral = symbolic_math("integrate", "x^2", lower=0, upper=3)
        self.assertEqual(integral["exact"], 9)
        factor = symbolic_math("factor", "x^2 - 1")
        self.assertEqual(factor["exact"], "(x - 1)*(x + 1)")

    def test_multiple_declared_variables(self):
        result = symbolic_math(
            "diff", "x^2 + x*y + y^2", variable="x", variables=["y"]
        )
        self.assertEqual(result["exact"], "2*x + y")

    def test_matrix_operations(self):
        determinant = symbolic_math("matrix_det", matrix=[[1, 2], [3, 4]])
        self.assertEqual(determinant["exact"], -2)
        inverse = symbolic_math("matrix_inv", matrix=[[1, 2], [3, 4]])
        self.assertEqual(inverse["exact"][1], ["3/2", "-1/2"])
        solution = symbolic_math(
            "matrix_solve", matrix=[[2, 1], [1, -1]], vector=[5, 1]
        )
        self.assertEqual(solution["exact"], [[2], [1]])

    def test_rejects_code_and_undeclared_names(self):
        for expression in (
            "__import__('os')",
            "x.__class__",
            "open(x)",
            "x + undeclared",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(SymbolicMathError):
                    symbolic_math("simplify", expression)

    def test_registry_exposes_read_only_symbolic_tool(self):
        registry = create_read_only_registry()
        tool = registry.get("symbolic_math")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.safety_level, "read_only")
        result = registry.execute(
            "symbolic_math",
            {"operation": "limit", "expression": "sin(x)/x", "point": "0"},
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output["exact"], 1)

    def test_context_requires_latex_for_user_facing_math(self):
        context = ContextBuilder().build_stable_context()
        self.assertIn("# Mathematical Typesetting", context)
        self.assertIn(r"\(...\)", context)
        self.assertIn(r"\[...\]", context)
        self.assertIn("symbolic_math", context)
        self.assertIn("严禁为了对齐版面", context)
        self.assertIn(r"\binom{10}{3}=120", context)


if __name__ == "__main__":
    unittest.main()
