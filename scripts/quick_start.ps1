# Create venv, install requirements, run the app (Windows PowerShell).
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path unq_img)) { python -m venv unq_img }
& .\unq_img\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py @args
