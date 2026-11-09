@echo off
cd /d "%~dp0"
rem Listens on this PC only (127.0.0.1). Open http://localhost:8501
python -m streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
pause
