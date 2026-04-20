#!/bin/bash
if [ -d "venv" ]; then
    source venv/bin/activate
else
    python3 -m venv venv
    source venv/bin/activate
    pip install --default-timeout=1000 -r requirements.txt
fi
pip install --default-timeout=1000 -r requirements.txt --quiet
python run.py
