import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from agent.config import load_config
from agent.sheets_client import SheetsClient

cfg = load_config()
sheets = SheetsClient.from_config(cfg)
rows = sheets.read_universities()
print("total rows:", len(rows))
print("header keys:", list(rows[0].keys()) if rows else "EMPTY")

hits = [(i, r) for i, r in enumerate(rows) if any("3841382" in str(v) for v in r.values())]
print("rows mentioning 3841382:", len(hits))
for i, r in hits[:5]:
    print(i, r)

non_empty = [SheetsClient.extract_orgid(r) for r in rows if SheetsClient.extract_orgid(r)]
print("rows with extractable OrgID:", len(non_empty), "of", len(rows))
print("sample first 5:", non_empty[:5])
print("sample last 5:", non_empty[-5:])
