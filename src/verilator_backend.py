# Copyright (c) 2025, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from config import FLAGS
from saver import saver

print = saver.log_info


def run_verilator_flow(
    sva_file_paths: List[str],
    design_dir: str,
    testbench_path: Optional[str],
    top_module: Optional[str],
) -> Dict:
    """
    Run simulation-driven assertion checking with Verilator.

    This backend intentionally requires a user-provided C++ testbench. It does
    not try to synthesize stimuli or a harness because assertion checking during
    simulation is only meaningful for a concrete test scenario.
    """
    build_dir = _prepare_build_dir()
    report = {
        "backend": "verilator",
        "status": "not_run",
        "compile_status": "not_run",
        "simulation_status": "not_run",
        "build_dir": str(build_dir),
        "rtl_files": [],
        "sva_files": list(sva_file_paths),
        "bindings_file": None,
        "testbench_path": testbench_path,
        "top_module": top_module,
        "compile_command": [],
        "simulation_command": [],
        "assertion_failures": [],
        "compile_log_path": None,
        "simulation_log_path": None,
        "report_json_path": None,
        "report_text_path": None,
    }

    if not testbench_path:
        return _write_report(
            report,
            build_dir,
            status="skipped",
            message="verilator_testbench_path is not set; provide a C++ testbench to run the Verilator backend.",
        )

    testbench = Path(testbench_path)
    if not testbench.exists():
        return _write_report(
            report,
            build_dir,
            status="error",
            message=f"Verilator testbench does not exist: {testbench}",
        )

    rtl_files = _collect_rtl_files(design_dir)
    bindings_file = _find_bindings_file(design_dir)
    report["rtl_files"] = rtl_files
    report["bindings_file"] = bindings_file

    if not rtl_files:
        return _write_report(
            report,
            build_dir,
            status="error",
            message=f"No RTL files found in design_dir: {design_dir}",
        )

    compile_result = compile_with_verilator(
        rtl_files=rtl_files,
        sva_file_paths=sva_file_paths,
        bindings_file=bindings_file,
        testbench_path=str(testbench),
        top_module=top_module,
        build_dir=build_dir,
    )
    report.update(compile_result)

    if compile_result["compile_status"] != "pass":
        return _write_report(report, build_dir, status="compile_failed")

    sim_result = run_simulation(
        binary_path=compile_result["binary_path"],
        build_dir=build_dir,
    )
    report.update(sim_result)
    report.update(parse_verilator_results(sim_result.get("simulation_log", "")))

    final_status = "pass" if sim_result["simulation_status"] == "pass" else "fail"
    return _write_report(report, build_dir, status=final_status)


def compile_with_verilator(
    rtl_files: List[str],
    sva_file_paths: List[str],
    bindings_file: Optional[str],
    testbench_path: str,
    top_module: Optional[str],
    build_dir: Path,
) -> Dict:
    command = [
        getattr(FLAGS, "verilator_bin", "verilator"),
        "--cc",
        "--exe",
        "--build",
    ]
    command.extend(getattr(FLAGS, "verilator_extra_args", ["--assert", "--trace"]))

    if top_module:
        command.extend(["--top-module", top_module])

    command.extend(rtl_files)
    command.extend(sva_file_paths)
    if bindings_file:
        command.append(bindings_file)
    command.append(testbench_path)
    command.extend(["-Mdir", str(build_dir)])

    compile_log_path = build_dir / "verilator_compile.log"
    print(f"Running Verilator compile command: {' '.join(command)}")

    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=getattr(FLAGS, "verilator_timeout_sec", 300),
            cwd=FLAGS.design_dir,
        )
        compile_log = result.stdout
        compile_status = "pass" if result.returncode == 0 else "fail"
    except subprocess.TimeoutExpired as e:
        compile_log = e.stdout or ""
        compile_log += f"\nTimed out after {getattr(FLAGS, 'verilator_timeout_sec', 300)} seconds.\n"
        compile_status = "timeout"
    except Exception as e:
        compile_log = f"Error running Verilator compile: {e}\n"
        compile_status = "error"

    compile_log_path.write_text(compile_log, encoding="utf-8")

    binary_path = _find_sim_binary(build_dir, top_module)

    return {
        "compile_status": compile_status,
        "compile_command": command,
        "compile_log_path": str(compile_log_path),
        "compile_log": compile_log,
        "binary_path": str(binary_path),
    }


def run_simulation(binary_path: str, build_dir: Path) -> Dict:
    simulation_log_path = build_dir / "verilator_simulation.log"
    binary = Path(binary_path)

    if not binary.exists():
        log = f"Compiled simulation binary was not found: {binary}\n"
        simulation_log_path.write_text(log, encoding="utf-8")
        return {
            "simulation_status": "error",
            "simulation_command": [str(binary)],
            "simulation_log_path": str(simulation_log_path),
            "simulation_log": log,
        }

    command = [str(binary)]
    print(f"Running Verilator simulation command: {' '.join(command)}")

    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=getattr(FLAGS, "verilator_timeout_sec", 300),
            cwd=build_dir,
        )
        simulation_log = result.stdout
        simulation_status = "pass" if result.returncode == 0 else "fail"
    except subprocess.TimeoutExpired as e:
        simulation_log = e.stdout or ""
        simulation_log += f"\nTimed out after {getattr(FLAGS, 'verilator_timeout_sec', 300)} seconds.\n"
        simulation_status = "timeout"
    except Exception as e:
        simulation_log = f"Error running Verilator simulation: {e}\n"
        simulation_status = "error"

    simulation_log_path.write_text(simulation_log, encoding="utf-8")

    return {
        "simulation_status": simulation_status,
        "simulation_command": command,
        "simulation_log_path": str(simulation_log_path),
        "simulation_log": simulation_log,
    }


def parse_verilator_results(log_text: str) -> Dict:
    failure_patterns = [
        r"%Error:\s+.*?Assertion failed.*",
        r".*Assertion failed.*",
        r".*\$stop.*",
        r".*\$fatal.*",
    ]
    assertion_failures = []
    for line in log_text.splitlines():
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in failure_patterns):
            assertion_failures.append({"message": line})

    return {"assertion_failures": assertion_failures}


def _collect_rtl_files(design_dir: str) -> List[str]:
    design_path = Path(design_dir)
    extensions = {".v", ".sv"}
    skipped_names = {"bindings.sva", "property.sva", "property_goldmine.sva"}
    rtl_files = []
    for path in sorted(design_path.rglob("*")):
        if path.suffix.lower() not in extensions:
            continue
        if path.name in skipped_names or path.name.endswith(".sva"):
            continue
        rtl_files.append(str(path))
    return rtl_files


def _find_bindings_file(design_dir: str) -> Optional[str]:
    bindings_file = Path(design_dir) / "bindings.sva"
    return str(bindings_file) if bindings_file.exists() else None


def _prepare_build_dir() -> Path:
    configured_dir = Path(getattr(FLAGS, "verilator_build_dir", "verilator_build"))
    if configured_dir.is_absolute():
        build_dir = configured_dir
    else:
        build_dir = Path(saver.logdir) / configured_dir

    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    return build_dir


def _expected_binary_path(build_dir: Path, top_module: Optional[str]) -> Path:
    binary_name = f"V{top_module}" if top_module else "Vtop"
    return build_dir / binary_name


def _find_sim_binary(build_dir: Path, top_module: Optional[str]) -> Path:
    expected = _expected_binary_path(build_dir, top_module)
    if expected.exists():
        return expected

    candidates = [
        path
        for path in build_dir.glob("V*")
        if path.is_file() and os.access(path, os.X_OK) and not path.suffix
    ]
    if candidates:
        return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]

    return expected


def _write_report(
    report: Dict,
    build_dir: Path,
    status: str,
    message: Optional[str] = None,
) -> Dict:
    report["status"] = status
    if message:
        report["message"] = message
        print(message)

    serializable_report = {
        key: value
        for key, value in report.items()
        if key not in {"compile_log", "simulation_log"}
    }
    report_json_path = build_dir / "verilator_report.json"
    report_text_path = build_dir / "verilator_report.txt"
    serializable_report["report_json_path"] = str(report_json_path)
    serializable_report["report_text_path"] = str(report_text_path)

    report_json_path.write_text(
        json.dumps(serializable_report, indent=2),
        encoding="utf-8",
    )
    report_text_path.write_text(_format_text_report(serializable_report), encoding="utf-8")

    report.update(serializable_report)
    return report


def _format_text_report(report: Dict) -> str:
    lines = [
        "Verilator Backend Report",
        "========================",
        f"Status: {report.get('status')}",
        f"Compile status: {report.get('compile_status')}",
        f"Simulation status: {report.get('simulation_status')}",
        f"Top module: {report.get('top_module')}",
        f"Testbench: {report.get('testbench_path')}",
        f"Compile log: {report.get('compile_log_path')}",
        f"Simulation log: {report.get('simulation_log_path')}",
        "",
        "Assertion failures:",
    ]
    failures = report.get("assertion_failures", [])
    if failures:
        lines.extend(f"- {failure.get('message')}" for failure in failures)
    else:
        lines.append("- None detected in the executed simulation.")
    if report.get("message"):
        lines.extend(["", f"Message: {report['message']}"])
    return "\n".join(lines) + "\n"
