#!/usr/bin/env python3
"""Run Verilator smoke tests for AssertPilot datasets.

The datasets include both correct and intentionally buggy RTL. In simulation
mode, correct variants are expected to pass and buggy variants are expected to
fail at least one assertion.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASETS_DIR = PROJECT_ROOT / "datasets"
DEFAULT_VERILATOR = (
    PROJECT_ROOT.parent / "verilator" / "install" / "bin" / "verilator"
)
DEFAULT_BUILD_ROOT = PROJECT_ROOT / "runs" / "verilator_datasets"


@dataclass(frozen=True)
class Variant:
    name: str
    top_module: str
    expected: str
    rtl_file: str | None = None


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Verilator lint/simulation smoke tests for AssertPilot datasets."
    )
    parser.add_argument(
        "--datasets-dir",
        type=Path,
        default=DEFAULT_DATASETS_DIR,
        help="Path to AssertPilot datasets directory.",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Dataset case to run. May be repeated. Defaults to all cases.",
    )
    parser.add_argument(
        "--variant",
        choices=["correct", "buggy", "both"],
        default="both",
        help="RTL variant to run.",
    )
    parser.add_argument(
        "--mode",
        choices=["lint", "simulate"],
        default="simulate",
        help="Run Verilator front-end lint only or full simulation.",
    )
    parser.add_argument(
        "--verilator",
        type=Path,
        default=DEFAULT_VERILATOR,
        help="Path to the Verilator executable.",
    )
    parser.add_argument(
        "--build-root",
        type=Path,
        default=DEFAULT_BUILD_ROOT,
        help="Directory for generated Verilator build artifacts.",
    )
    parser.add_argument(
        "--keep-build",
        action="store_true",
        help="Keep existing build directories instead of deleting them before each run.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print Verilator stdout/stderr for each run.",
    )
    return parser.parse_args()


def load_case(case_dir: Path) -> dict:
    signals_path = case_dir / "signals.json"
    if not signals_path.exists():
        raise FileNotFoundError(f"Missing signals.json: {signals_path}")
    return json.loads(signals_path.read_text(encoding="utf-8"))


def iter_cases(datasets_dir: Path, selected_cases: list[str] | None) -> Iterable[Path]:
    if selected_cases:
        for case_name in selected_cases:
            case_dir = datasets_dir / case_name
            if not case_dir.exists():
                raise FileNotFoundError(f"Dataset case does not exist: {case_dir}")
            yield case_dir
        return

    for case_dir in sorted(datasets_dir.iterdir()):
        if case_dir.is_dir() and (case_dir / "signals.json").exists():
            yield case_dir


def select_variants(case_config: dict, variant: str) -> list[Variant]:
    variants = []
    if variant in ["correct", "both"]:
        variants.append(
            Variant(
                name="correct",
                top_module=case_config["top_module"],
                expected="pass",
            )
        )
    if variant in ["buggy", "both"]:
        variants.append(
            Variant(
                name="buggy",
                top_module=case_config["buggy_top_module"],
                expected="fail",
            )
        )
    return variants


def source_files(case_dir: Path, variant: Variant | None = None) -> list[str]:
    rtl_dir = case_dir / "rtl"
    buggy_rtl = Path(variant.rtl_file) if variant and variant.rtl_file else rtl_dir / "design_buggy.v"
    return [
        str(rtl_dir / "design.v"),
        str(buggy_rtl),
        str(rtl_dir / "property_goldmine.sva"),
        str(rtl_dir / "bindings.sva"),
    ]


def run_lint(verilator: Path, case_dir: Path, variant: Variant) -> subprocess.CompletedProcess:
    command = [
        str(verilator),
        "--lint-only",
        "--assert",
        "--top-module",
        variant.top_module,
        *source_files(case_dir, variant),
    ]
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def run_simulation(
    verilator: Path,
    case_dir: Path,
    variant: Variant,
    build_root: Path,
    keep_build: bool,
) -> RunResult:
    build_dir = build_root / case_dir.name / variant.name
    if build_dir.exists() and not keep_build:
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(verilator),
        "--cc",
        "--exe",
        "--build",
        "--assert",
        "--top-module",
        variant.top_module,
        *source_files(case_dir, variant),
        str(case_dir / "sim" / "tb.cpp"),
        "-Mdir",
        str(build_dir),
    ]
    compile_result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if compile_result.returncode != 0:
        return RunResult(
            returncode=compile_result.returncode,
            stdout=compile_result.stdout,
        )

    binary = build_dir / f"V{variant.top_module}"
    if not binary.exists():
        return RunResult(
            returncode=2,
            stdout=(
                compile_result.stdout
                + f"\nERROR: compiled simulation binary not found: {binary}\n"
            ),
        )

    sim_result = subprocess.run(
        [str(binary)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=build_dir,
    )
    return RunResult(
        returncode=sim_result.returncode,
        stdout=compile_result.stdout + "\n" + sim_result.stdout,
    )


def outcome_matches(mode: str, variant: Variant, returncode: int) -> bool:
    if mode == "lint":
        return returncode == 0
    if variant.expected == "pass":
        return returncode == 0
    return returncode != 0


def main() -> int:
    args = parse_args()

    if not args.verilator.exists():
        print(f"ERROR: Verilator executable not found: {args.verilator}", file=sys.stderr)
        return 2

    failures = []
    for case_dir in iter_cases(args.datasets_dir, args.cases):
        case_config = load_case(case_dir)
        for variant in select_variants(case_config, args.variant):
            if args.mode == "lint":
                result = run_lint(args.verilator, case_dir, variant)
                expected_text = "lint pass"
            else:
                result = run_simulation(
                    args.verilator,
                    case_dir,
                    variant,
                    args.build_root,
                    args.keep_build,
                )
                expected_text = f"simulation {variant.expected}"

            ok = outcome_matches(args.mode, variant, result.returncode)
            status = "PASS" if ok else "FAIL"
            print(
                f"[{status}] {case_dir.name}:{variant.name} "
                f"mode={args.mode} expected={expected_text} returncode={result.returncode}"
            )

            if args.verbose or not ok:
                print(result.stdout)

            if not ok:
                failures.append((case_dir.name, variant.name))

    if failures:
        print("\nUnexpected failures:")
        for case_name, variant_name in failures:
            print(f"- {case_name}:{variant_name}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
