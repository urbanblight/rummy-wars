python3 venv .venv/
source .venv/bin/activate
python3 pip upgrade 
pip install -r requirements.txt
pip install -r requirements-dev.txt
cd docs
make html
cd ..
python3 -m  playwright install chromium
