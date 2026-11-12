"""Print real headers, guessed mapping and road-row share before importing.
Usage: python scripts/inspect_csv.py path/to/file.csv"""
import sys
import pandas as pd
sys.path.insert(0, ".")
from app.ingest import read_table, guess_mapping, ROAD_RE

path = sys.argv[1]
df = read_table(open(path, "rb").read(), path)
print("Rows:", len(df))
print("Columns:", list(df.columns))
mp = guess_mapping(df.columns)
print("Guessed mapping:", mp)
if "description" in mp:
    road = df[mp["description"]].fillna("").str.contains(ROAD_RE)
    print(f"Road-related rows: {road.sum()} of {len(df)}")
print(df.head(3).T)
