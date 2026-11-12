"""Creates clearly labelled DEMONSTRATION citizen reports (is_demo=True).
These are synthetic and must never be presented as real complaints."""
import sys, random
from datetime import datetime, timedelta
sys.path.insert(0, ".")
from app.db import SessionLocal, Base, engine
from app import models as m
from app.main import recluster
from app.security import hash_secret

Base.metadata.create_all(engine)
db = SessionLocal()
random.seed(7)
spots = [(12.9352, 77.6245, "Demo stretch A", "pothole", ["Large pothole near the bus stop", "Road damaged in front of the bus stop", "Deep pothole on the same stretch"]),
         (12.9784, 77.6408, "Demo stretch B", "damaged_road", ["Broken road surface after digging", "Dug up road not restored"]),
         (12.9141, 77.5833, "Demo stretch C", "waterlogging", ["Water collects and road is breaking"])]
for lat, lon, road, cat, texts in spots:
    for i, t in enumerate(texts * 2):
        dlat, dlon = random.uniform(-0.0002, 0.0002), random.uniform(-0.0002, 0.0002)
        r = m.Report(public_ref=f"DEMO{random.randint(1000, 9999)}", token_hash=hash_secret("demo"),
                     category=cat, description=t, lat=lat + dlat, lon=lon + dlon, road_name=road,
                     is_demo=True, created_at=datetime.utcnow() - timedelta(days=random.randint(0, 45)))
        db.add(r)
db.commit()
recluster(db)
print("Demo reports created. Tracking code for all demo reports: demo")
