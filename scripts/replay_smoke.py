"""Explicit SYNTHETIC replay: exercises storage→tracking→measurement→packet offline.

Detection responses are test inputs, not AI observations. No prices are supplied.
This must never be presented as the assessment's real room, video or accuracy run.
"""
import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageDraw

from claim_agent.config import Settings
from claim_agent.models import FrameMetadata
from claim_agent.pipeline import SweepRunner
from claim_agent.storage import EvidenceStore
from claim_agent.vision import Scan, Detection


class SyntheticVision:
    provider=SimpleNamespace(usage=[{"synthetic":True,"usage":None}])
    async def inspect(self,jpeg,metadata):
        detections=[Detection(kind="shelf",polygon=[[.3,.35],[.6,.35],[.6,.6],[.3,.6]],confidence=1)]
        for n,x in enumerate([.35,.4,.45],1):
            detections.append(Detection(kind="book",polygon=[[x,.45],[x+.015,.45],[x+.015,.55],[x,.55]],
                                       title=f"Example Book {n}",visible_text=f"Example Book {n}",confidence=.9,shelf_index=0))
        return Scan(detections=detections,guidance="Synthetic replay: no model or real-room accuracy was tested.")


async def main():
    root=Path(sys.argv[1] if len(sys.argv)>1 else "data")
    store=EvidenceStore(root)
    state=store.create("SYNTHETIC replay — not a physical device","India","INR",synthetic=True)
    # This fixture supplies declared detections directly; it does not model the
    # phone's OCR/detector evidence or test the production local-first router.
    runner=SweepRunner(state,store,Settings(data_dir=root,local_first=False),SyntheticVision(),None)
    image=Image.new("RGB",(1000,1000),"#ecebdc")
    draw=ImageDraw.Draw(image)
    draw.text((50,40),"SYNTHETIC GEOMETRY FIXTURE — NOT A REAL ROOM",fill="black")
    draw.rectangle((300,350,600,600),outline="black",width=3)
    for n,x in enumerate([350,400,450],1):
        draw.rectangle((x,450,x+15,550),fill=["#427b69","#d6b155","#7874a0"][n-1])
        draw.text((x-20,570+n*15),f"Example Book {n}",fill="black")
    out=io.BytesIO();image.save(out,format="JPEG",quality=95)
    runner.start()
    try:
        for n in [1,2]:
            metadata=FrameMetadata(id=f"f{n}",timestamp_s=n,image_width=1000,image_height=1000,
                intrinsics=[1000,0,0,0,1000,0,500,500,1],camera_transform=np.eye(4).flatten(order="F").tolist(),
                tracking="normal",depth_width=100,depth_height=100,ar_session_id="synthetic-one-session")
            await runner.upload(metadata,out.getvalue(),np.full((100,100),2,dtype="<f4").tobytes(),bytes([2])*10000)
            await runner.queue.join()
        corners=[[-2,-1,-3],[2,-1,-3],[2,-1,0],[-2,-1,0]]
        walls=[]
        for n in range(4):
            a,b=corners[n],corners[(n+1)%4]
            walls.append({"id":f"w{n}","corners":[a,b,[b[0],1.5,b[2]],[a[0],1.5,a[2]]]})
        await runner.set_geometry({"ar_session_id":"synthetic-one-session","floors":[{"corners":corners}],"walls":walls})
        packet=await runner.finish(2)
        assert packet.totals.book_count==3, packet.totals
        assert all(b.spine_height_cm==20 and b.spine_thickness_cm==3 for b in packet.books)
        assert packet.room.floor_area_m2==12
        assert packet.room.wall_area_m2==35
        assert packet.totals.books_replacement_cost==0
        print(f"SYNTHETIC replay passed: repeated views → 3 books, 20×3 cm, 12 m² floor, 35 m² walls; prices unknown.")
        print(f"Packet: {store.directory(state.sweep.id)/'packet.zip'}")
        print(f"Sweep ID: {state.sweep.id}")
    finally:
        await runner.close()


if __name__=="__main__":
    asyncio.run(main())
