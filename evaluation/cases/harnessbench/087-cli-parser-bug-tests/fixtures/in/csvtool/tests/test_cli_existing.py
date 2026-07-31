import csv
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "csvtool.cli", *args], cwd=ROOT, capture_output=True, text=True)


def test_filter_select_and_descending_sort():
    proc = run_cli("samples/orders.csv", "--where", "status=paid", "--select", "id,total", "--sort", "-created_at")
    assert proc.returncode == 0, proc.stderr
    rows = list(csv.DictReader(proc.stdout.splitlines()))
    assert rows == [{"id": "o1", "total": "1200"}, {"id": "o3", "total": "750"}]


def test_quoted_commas_are_preserved_when_selected():
    proc = run_cli("samples/orders.csv", "--where", "id=o1", "--select", "id,customer")
    assert proc.returncode == 0, proc.stderr
    rows = list(csv.DictReader(proc.stdout.splitlines()))
    assert rows == [{"id": "o1", "customer": "Ava, Inc"}]


def test_empty_result_keeps_header():
    proc = run_cli("samples/orders.csv", "--where", "status=refunded", "--select", "id,total")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "id,total"
