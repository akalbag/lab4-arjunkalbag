import sys
import re

from pycparser import c_parser, c_ast


class LocalVarAnalyzer:
    """Find the maximum number of simultaneously in-scope local variables."""

    def __init__(self, target_func_name):
        self.target_func_name = target_func_name
        self.found_target = False
        self.has_static = False
        self.max_local_vars = 0
        self.functions = {}
        self.active_functions = set()
        # Maps a function name to the additional peak created by that function
        # and any helpers it calls, independent of the caller's live variables.
        self.function_peaks = {}
        self.recursive_functions = set()

    @staticmethod
    def _is_variable_decl(node):
        return (
            isinstance(node, c_ast.Decl)
            and node.name is not None
            and not isinstance(node.type, c_ast.FuncDecl)
        )

    def _count_decl(self, node):
        if not self._is_variable_decl(node):
            return 0
        if "static" in (node.storage or []):
            self.has_static = True
        return 1

    def _statement_peak(self, node, live):
        """Return the peak local count while executing node with live locals."""
        if node is None:
            return live

        if isinstance(node, c_ast.Compound):
            return self._compound_peak(node, live)

        if isinstance(node, c_ast.FuncCall):
            peak = live
            # A call target can be another expression (for example, a function
            # pointer), and arguments can themselves contain function calls.
            for _, child in node.children():
                peak = max(peak, self._statement_peak(child, live))

            if isinstance(node.name, c_ast.ID):
                called_function = self.functions.get(node.name.name)
                if called_function is not None:
                    peak = max(peak, self._function_peak(called_function, live))
            return peak

        if isinstance(node, c_ast.If):
            condition_peak = self._statement_peak(node.cond, live)
            true_peak = self._statement_peak(node.iftrue, live)
            false_peak = self._statement_peak(node.iffalse, live)
            return max(live, condition_peak, true_peak, false_peak)

        if isinstance(node, (c_ast.For, c_ast.While, c_ast.DoWhile)):
            loop_live = live
            peak = live
            if isinstance(node, c_ast.For):
                if isinstance(node.init, c_ast.DeclList):
                    for declaration in node.init.decls:
                        loop_live += self._count_decl(declaration)
                        peak = max(peak, loop_live)
                        if declaration.init is not None:
                            peak = max(
                                peak,
                                self._statement_peak(declaration.init, loop_live),
                            )
                elif isinstance(node.init, c_ast.Decl):
                    loop_live += self._count_decl(node.init)
                    peak = max(peak, loop_live)
                    if node.init.init is not None:
                        peak = max(
                            peak,
                            self._statement_peak(node.init.init, loop_live),
                        )
                else:
                    peak = max(peak, self._statement_peak(node.init, loop_live))
            for part in (getattr(node, "cond", None),
                         getattr(node, "next", None)):
                peak = max(peak, self._statement_peak(part, loop_live))
            return max(peak, self._statement_peak(node.stmt, loop_live))

        if isinstance(node, (c_ast.Switch, c_ast.Label, c_ast.Case, c_ast.Default)):
            peak = live
            for child in node.children():
                peak = max(peak, self._statement_peak(child[1], live))
            return peak

        # A declaration used as a statement (unusual, but valid in an AST).
        if isinstance(node, c_ast.Decl):
            return live + self._count_decl(node)

        # Search expressions too, because a call may appear in an assignment,
        # return statement, condition, array subscript, or initializer.
        peak = live
        for _, child in node.children():
            peak = max(peak, self._statement_peak(child, live))
        return peak

    def _compound_peak(self, node, live):
        current = live
        peak = live
        for item in node.block_items or []:
            if isinstance(item, c_ast.Decl):
                current += self._count_decl(item)
                peak = max(peak, current)
                if item.init is not None:
                    peak = max(peak, self._statement_peak(item.init, current))
            else:
                peak = max(peak, self._statement_peak(item, current))
        return peak

    def _function_peak(self, node, live):
        """Return the call-chain peak for a FuncDef entered with live locals."""
        function_name = node.decl.name

        if function_name in self.function_peaks:
            return live + self.function_peaks[function_name]

        # Recursive call chains have no finite maximum without a recursion
        # depth limit. Stop at a repeated function instead of recursing forever.
        if function_name in self.active_functions:
            # Do not cache functions participating in this active recursive
            # chain, because the truncated result is not a true finite peak.
            self.recursive_functions.update(self.active_functions)
            return live

        self.active_functions.add(function_name)
        try:
            # Analyze only the body. Function parameters are intentionally not
            # counted. Calculate an intrinsic peak from zero so it can be reused
            # at call sites that have different numbers of live caller locals.
            intrinsic_peak = self._compound_peak(node.body, 0)
        finally:
            self.active_functions.remove(function_name)

        if function_name not in self.recursive_functions:
            self.function_peaks[function_name] = intrinsic_peak
        return live + intrinsic_peak

    def analyze(self, ast):
        self.functions = {
            ext.decl.name: ext
            for ext in ast.ext
            if isinstance(ext, c_ast.FuncDef)
        }
        target = self.functions.get(self.target_func_name)
        if target is not None:
            self.found_target = True
            self.max_local_vars = self._function_peak(target, 0)


def clean_c_source(c_filepath):
    """Remove includes/comments and provide minimal types for pycparser."""
    with open(c_filepath, "r") as source_file:
        code = source_file.read()

    code = re.sub(r"//.*?(?=\n|$)", "", code)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    code = re.sub(r"^\s*#include\s+[\"<][^\">]+[\">].*$", "", code,
                  flags=re.MULTILINE)
    return "typedef unsigned long size_t;\n" + code


def analyze_function_vars(c_filepath="trans.c", target_func="transpose_submit"):
    cleaned_code = clean_c_source(c_filepath)
    parser = c_parser.CParser()
    try:
        ast = parser.parse(cleaned_code, filename=c_filepath)
    except Exception as error:
        print(f"Parsing Error: {error}")
        sys.exit(1)

    analyzer = LocalVarAnalyzer(target_func)
    analyzer.analyze(ast)

    if not analyzer.found_target:
        print(f"Error: Function '{target_func}' not found in {c_filepath}.")
        sys.exit(1)

    if analyzer.has_static:
        print("Do not use static variables in this program")
        sys.exit(1)

    print(f"--- Variable Report for Target Function: '{target_func}' ---")
    print(f"Maximum Local Variables In Scope: {analyzer.max_local_vars}")


if __name__ == "__main__":
    analyze_function_vars()
    sys.exit(0)
