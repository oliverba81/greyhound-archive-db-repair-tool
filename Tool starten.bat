@echo off
cd /d "%~dp0"
py -c "import subprocess,sys,os; w=os.path.join(os.path.dirname(sys.executable),'pythonw.exe'); subprocess.Popen([w if os.path.exists(w) else sys.executable,'-m','gh_repair'], cwd=os.getcwd())"
