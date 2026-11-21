"""Run Terraform without publishing config, prior state, or raw diagnostics."""

import json
import re
import subprocess
import sys

SAFE_SUMMARIES = {
    "Invalid function argument",
    "Error in function call",
    "Invalid for_each argument",
    "Invalid count argument",
    "Unsupported argument",
    "Missing required argument",
    "Error acquiring the state lock",
    "Resource precondition failed",
    "External Program Execution Failed",
    "No valid credential sources found",
}


def report(stdout: str, stderr: str) -> None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "change_summary":
            changes = event.get("changes", {})
            counts = [changes.get(key) for key in ("add", "change", "remove")]
            if all(type(value) is int for value in counts):
                print(f"Terraform changes: add={counts[0]} change={counts[1]} remove={counts[2]}")
        if event.get("type") != "diagnostic":
            continue
        diagnostic = event.get("diagnostic", {})
        severity = "error" if diagnostic.get("severity") == "error" else "warning"
        summary = diagnostic.get("summary", "")
        title = summary if summary in SAFE_SUMMARIES else "provider/configuration diagnostic"
        print(f"Terraform {severity}: {title}")
    # Only emit AWS action identifiers, never messages, ARNs, values or snippets.
    for action in sorted(set(re.findall(
        r"not authorized to perform:\s*([a-z0-9-]+:[A-Za-z][A-Za-z0-9]*)",
        stdout + "\n" + stderr,
    ))):
        print(f"AWS permission denied: {action}")


def main(args: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if args is None else args)
    command_index = next((i for i, arg in enumerate(args) if arg in {"plan", "apply"}), None)
    if command_index is None:
        print("Expected Terraform plan or apply", file=sys.stderr)
        return 1
    args.insert(command_index + 1, "-json")
    try:
        result = subprocess.run(["terraform", *args], capture_output=True, text=True, check=False)
    except OSError:
        print("Unable to start Terraform", file=sys.stderr)
        return 1
    report(result.stdout, result.stderr)
    if result.returncode:
        print(f"Terraform exited with code {result.returncode}; raw diagnostics withheld to protect config.")
    else:
        print("Terraform completed successfully.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
