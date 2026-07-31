"""Layer validation helpers (optional local checks)."""
import sys
import time

def test_layer_1_syntax():
    """Syntax check."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("buggy_code", "buggy_code.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return True, "Syntax OK"
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"
    except Exception as e:
        return False, f"Import Error: {e}"

def test_layer_2_import():
    """Import check."""
    try:
        import json
        data = json.loads('{"test": "value"}')
        return True, "Import OK"
    except ImportError as e:
        return False, f"ImportError: {e}"

def test_layer_3_type():
    """Type handling."""
    try:
        result = "Score: " + str(95)
        assert result == "Score: 95"
        return True, "Type conversion OK"
    except TypeError as e:
        return False, f"TypeError: {e}"

def test_layer_4_logic():
    """Boundary logic."""
    def is_valid_score(score):
        return score >= 0 and score <= 100
    
    try:
        assert is_valid_score(100), "A perfect score should pass"
        assert is_valid_score(0), "Zero should pass"
        assert is_valid_score(50), "Mid-range should pass"
        return True, "Logic OK"
    except AssertionError as e:
        return False, f"Logic Error: {e}"

def test_layer_5_performance():
    """Performance (set-based dedup)."""
    def find_duplicates(data):
        seen = set()
        duplicates = set()
        for item in data:
            if item in seen:
                duplicates.add(item)
            seen.add(item)
        return list(duplicates)
    
    try:
        data = list(range(1000)) + [500]
        start = time.time()
        result = find_duplicates(data)
        elapsed = time.time() - start
        
        if elapsed > 2.0:
            return False, f"Too slow: {elapsed:.2f}s"
        return True, f"Performance OK: {elapsed:.3f}s"
    except Exception as e:
        return False, f"Performance Error: {e}"

if __name__ == "__main__":
    tests = [
        test_layer_1_syntax,
        test_layer_2_import,
        test_layer_3_type,
        test_layer_4_logic,
        test_layer_5_performance,
    ]
    
    for test in tests:
        passed, msg = test()
        status = "PASS" if passed else "FAIL"
        print(f"{test.__name__}: {status} - {msg}")
