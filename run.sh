if [ -d "venv" ]; then
    source venv/Scripts/activate
else
    python3 -m venv venv
    source venv/Scripts/activate
    pip install -r requirements.txt
fi
pip install -r requirements.txt --quiet
uvicorn app.main:app --host 0.0.0.0 --port 1254 --reload
