import json
import os
import shutil
import subprocess

CDK_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cdk")


def _resolve_cdk_executable() -> str:
    """
    On Windows, `cdk` is actually a `cdk.cmd` shim, and subprocess.run() with
    a bare "cdk" string does not do the .cmd/.exe PATH resolution that a real
    shell does. shutil.which() resolves the full path correctly on all OSes.
    """
    cdk_path = shutil.which("cdk")
    if not cdk_path:
        raise RuntimeError(
            "Could not find the 'cdk' executable on PATH. "
            "Install it with 'npm install -g aws-cdk' and restart your terminal."
        )
    return cdk_path


def run_cdk_deploy(context: dict, stack_name: str) -> dict:
    """
    Runs `cdk deploy` for the given stack, passing `context` in as -c key=value
    pairs (read by cdk/app.py), and returns the stack's CfnOutputs as a dict.
    """
    context_args = []
    for k, v in context.items():
        context_args += ["-c", f"{k}={v}"]

    outputs_file = os.path.join(CDK_DIR, f".outputs-{stack_name}.json")
    if os.path.exists(outputs_file):
        os.remove(outputs_file)

    cdk_exe = _resolve_cdk_executable()

    cmd = [
              cdk_exe, "deploy", stack_name,
              "--require-approval", "never",
              "--outputs-file", outputs_file,
          ] + context_args

    result = subprocess.run(
        cmd,
        cwd=CDK_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(f"cdk deploy failed:\n{stderr or stdout}")

    outputs = {}
    if os.path.exists(outputs_file):
        with open(outputs_file) as f:
            data = json.load(f)
        outputs = data.get(stack_name, {})
        os.remove(outputs_file)

    return outputs