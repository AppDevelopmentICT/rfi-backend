if [ -d "venv" ]; then
    source venv/Scripts/activate
else
    python -m venv venv
    source venv/Scripts/activate
    pip install --default-timeout=1000 -r requirements.txt
fi
pip install --default-timeout=1000 -r requirements.txt --quiet
python run.py
