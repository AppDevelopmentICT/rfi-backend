if [ -d "venv" ]; then
    source venv/bin/activate
else
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
fi
pip install -r requirements.txt --quiet
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
