"""Run computer-use MCP tests on Lappy. Uploaded via paramiko."""
import subprocess, sys, os

os.chdir(r"C:\Users\aaron\computer-use-mcp")
result = subprocess.run(
    [r"C:\Program Files\Python311\python.exe", "test_tools.py"],
    capture_output=True, text=True, timeout=360,
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[:500])
sys.exit(result.returncode)
