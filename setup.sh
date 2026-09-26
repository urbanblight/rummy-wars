python3 -m venv .venv/
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
cd docs
make html
cd ..
python3 -m  playwright install chromium
