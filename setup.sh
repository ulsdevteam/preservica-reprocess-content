# script mostly for reference, can be used as bash script as well but ymmv
/usr/bin/python3.11 -m venv .venv

source .venv/bin/activate
git clone https://github.com/ulsdevteam/preservica-opex-parser.git
cd preservica-opex-parser
git checkout simple-initial-features
pip install .
cd ..
deactivate


