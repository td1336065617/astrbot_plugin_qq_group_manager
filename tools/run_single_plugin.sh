#!/usr/bin/env bash
# 隔离矩阵执行脚本：run_single_plugin.sh <插件目录名>
# 把插件单独放进一个最小 AstrBot 目录树里跑（验证 R-IND-01/02/03）。
set -euo pipefail
NAME="${1:?用法: run_single_plugin.sh <插件目录名>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "/tmp/iso_${NAME}.XXXXXX")"
PYTHON="${PYTHON:-python3}"
LOG="$WORK/isolation_result.txt"

mkdir -p "$WORK/data/plugins"
cp -r "$ROOT" "$WORK/data/plugins/$NAME"
rm -rf "$WORK/data/plugins/$NAME/.git" "$WORK/data/plugins/$NAME/__pycache__"

{
  echo "# 隔离验证：$NAME"
  echo "时间：$(date -Iseconds)"

  echo "## 1) 单测"
  (cd "$WORK/data/plugins/$NAME" && "$PYTHON" -m pytest -q)

  echo "## 2) 零耦合门禁"
  (cd "$WORK/data/plugins/$NAME" && "$PYTHON" tools/check_no_cross_plugin_imports.py .)

  echo "## 3) 按 AstrBot 的入口模块路径真导入"
  WORK="$WORK" NAME="$NAME" "$PYTHON" - <<PY
import ast, importlib, os, pathlib, sys
work, name = os.environ["WORK"], os.environ["NAME"]
sys.path.insert(0, work)
pkg = importlib.import_module(f"data.plugins.{name}")
entry = getattr(pkg, "main", None) or importlib.import_module(f"data.plugins.{name}.main")
classes = [v for v in vars(entry).values() if isinstance(v, type) and v.__module__ == entry.__name__]
assert classes, "未找到插件类"
main_py = pathlib.Path(work) / "data" / "plugins" / name / "main.py"
tree = ast.parse(main_py.read_text(encoding="utf-8"))
tools_found = []
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for deco in node.decorator_list:
            text = ast.unparse(deco)
            if "llm_tool" in text:
                tools_found.append(node.name)
print(f"插件类：{[c.__name__ for c in classes]}")
print(f"AI 工具：{sorted(tools_found)}")
PY
} 2>&1 | tee "$LOG"

echo
echo "结果已存：$LOG"
echo "（把结果填进 tools/isolation_matrix.md）"