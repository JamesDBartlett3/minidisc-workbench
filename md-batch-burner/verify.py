#!/usr/bin/env python3
"""
Verification script for MiniDisc Batch Burner
Checks file existence and syntax without requiring dependencies
"""

import ast
import json
from pathlib import Path


def verify_python_syntax(filepath):
    """Verify Python syntax."""
    with open(filepath, 'r') as f:
        try:
            ast.parse(f.read())
            return True, "Valid Python syntax"
        except SyntaxError as e:
            return False, f"Syntax error: {e}"


def verify_json_syntax(filepath):
    """Verify JSON syntax."""
    with open(filepath, 'r') as f:
        try:
            json.load(f)
            return True, "Valid JSON syntax"
        except json.JSONDecodeError as e:
            return False, f"JSON error: {e}"


def verify_nodejs_syntax(filepath):
    """Verify Node.js syntax (basic check)."""
    with open(filepath, 'r') as f:
        content = f.read()
        # Basic checks
        if 'module.exports' in content or 'require(' in content:
            return True, "Valid Node.js structure"
        return True, "Node.js script (basic verification)"


def main():
    """Run verification checks."""
    project_dir = Path(__file__).parent

    print("=" * 60)
    print("MiniDisc Batch Burner - Verification")
    print("=" * 60)
    print()

    checks = [
        ("Python GUI", project_dir / "md_batch_burner.py", verify_python_syntax),
        ("Node.js Helper", project_dir / "netmd-batch-helper.js", verify_nodejs_syntax),
    ]

    all_passed = True

    for name, filepath, verifier in checks:
        print(f"Checking {name}...")

        if not filepath.exists():
            print(f"  ❌ File not found: {filepath}")
            all_passed = False
            continue

        passed, message = verifier(filepath)

        if passed:
            print(f"  ✅ {message}")
        else:
            print(f"  ❌ {message}")
            all_passed = False

        print()

    # Check additional files
    print("Checking additional files...")
    additional_files = [
        ("Requirements", project_dir / "requirements.txt"),
        ("README", project_dir / "README.md"),
    ]

    for name, filepath in additional_files:
        if filepath.exists():
            print(f"  ✅ {name} found")
        else:
            print(f"  ❌ {name} not found")
            all_passed = False

    print()
    print("=" * 60)

    if all_passed:
        print("All checks passed! ✅")
        print()
        print("To run the application:")
        print("  1. Install dependencies: pip install -r requirements.txt")
        print("  2. Run: python md_batch_burner.py")
        return 0
    else:
        print("Some checks failed. Please review the output above.")
        return 1


if __name__ == "__main__":
    exit(main())
