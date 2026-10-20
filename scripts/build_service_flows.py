"""Render source-audited service sequences as editable Mermaid, SVG and PDF.

Authoring only: needs reportlab and pypdf; never imports the application or calls
its APIs. Canonical messages live in docs/submission/service-flows.json.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import quote

from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon, Circle
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/submission/service-flows.json"
DEST = ROOT / "docs/submission/flows"
OUT = ROOT / "output/pdf/service-flow-diagrams.pdf"
REPO = "https://github.com/Tushar-kalsi-virtualfusion/cozmo-library-claim-agent"
W, H = landscape(A3)
INK = "#153d38"
MUTED = "#536d68"
PALE = "#f3f7f6"
COLORS = {"request": "#147760", "response": "#236c9b", "event": "#7651a4", "internal": "#657b80", "error": "#ac5b1d"}
NAMES = {"request": "REQUEST", "response": "RESPONSE", "event": "ASYNC EVENT", "internal": "IN-PROCESS", "error": "ALTERNATIVE / ERROR"}


def wrap(text, width, size=11, font="Helvetica"):
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for word in paragraph.split():
            if line and stringWidth(line + " " + word, font, size) > width:
                lines.append(line)
                line = ""
            # Paths/identifiers may have no spaces; split without clipping.
            while stringWidth(word, font, size) > width:
                n = 1
                while n < len(word) and stringWidth(word[:n+1], font, size) <= width:
                    n += 1
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:n])
                word = word[n:]
            line = (line + " " + word).strip()
        if line:
            lines.append(line)
    return lines or [""]


def label(d, text, x, y, width, size=11, bold=False, color=INK, leading=None):
    leading = leading or size * 1.25
    font = "Helvetica-Bold" if bold else "Helvetica"
    lines = wrap(text, width, size, font)
    for i, value in enumerate(lines):
        d.add(String(x, y - i * leading, value, fontName=font, fontSize=size, fillColor=colors.HexColor(color)))
    return len(lines) * leading


def base(title, subtitle, footer):
    d = Drawing(W, H)
    d.add(Rect(0, 0, W, H, fillColor=colors.white, strokeColor=None))
    label(d, "LIBRARY CLAIM  /  SERVICE FLOWS", 38, H-28, W-76, 10, True, MUTED)
    label(d, title, 38, H-59, W-76, 24, True)
    label(d, subtitle, 38, H-80, W-76, 10.5, False, MUTED)
    label(d, footer, 38, 20, W-76, 8.5, False, MUTED)
    return d


def row_height(step):
    return max(34, 13 * len(wrap(step["label"], 365, 11, "Helvetica-Bold")) +
               12.2 * len(wrap(step["detail"], 365, 10.2)) + 11)


def pages_for(flow):
    pages, current, height = [], [], 0
    for index, step in enumerate(flow["steps"], 1):
        h = row_height(step)
        if current and height + h > H - 255:
            pages.append(current)
            current, height = [], 0
        current.append((index, step, h))
        height += h
    if current:
        pages.append(current)
    # Balance continuations so a final response does not occupy a page alone.
    for i in range(len(pages)-1, 0, -1):
        previous, following = pages[i-1], pages[i]
        while len(previous)>1:
            before=sum(row[2] for row in previous)
            after=sum(row[2] for row in following)
            moving=previous[-1][2]
            if after+moving>H-255 or abs((before-moving)-(after+moving))>=abs(before-after):
                break
            following.insert(0,previous.pop())
    return pages


def sequence_page(flow, rows, part, total):
    suffix = f" - part {part}/{total}" if total > 1 else ""
    d = base(flow["id"] + "  " + flow["title"] + suffix, flow["subtitle"],
             "As implemented 20 October 2026 | Solid: request/call | Dashed: response | Purple: asynchronous event | Grey: in-process")
    participants = flow["participants"]
    left, right, ledger = 50, 704, 764
    spacing = (right-left) / len(participants)
    xs = {p["id"]: left + spacing*(i+.5) for i, p in enumerate(participants)}
    first_y = H-158
    end_y = first_y - sum(h for _, _, h in rows)
    for i, (_, _, height) in enumerate(rows):
        y = first_y - sum(h for _, _, h in rows[:i])
        if i % 2 == 0:
            d.add(Rect(38, y-height, W-76, height, fillColor=colors.HexColor("#f7f9f9"), strokeColor=None))
    for p in participants:
        x = xs[p["id"]]
        tint = {"phone": "#e6f2eb", "internal": "#e9f1f9", "cloud": "#f0eafa", "store": "#edf0f0", "web": "#faf0de"}[p["kind"]]
        width = min(118, spacing-9)
        d.add(Rect(x-width/2, H-140, width, 43, rx=5, ry=5, fillColor=colors.HexColor(tint), strokeColor=None))
        lines = wrap(p["name"], width-12, 10, "Helvetica-Bold")
        for n, line in enumerate(lines):
            d.add(String(x, H-112-n*12, line, textAnchor="middle", fontName="Helvetica-Bold", fontSize=10, fillColor=colors.HexColor(INK)))
        d.add(Line(x, H-145, x, end_y-5, strokeColor=colors.HexColor("#bbc9c6"), strokeDashArray=[3, 4]))
    label(d, "MESSAGE / PAYLOAD / RESULT", ledger, H-113, W-ledger-38, 11, True, MUTED)
    label(d, "Read downward; numbers match the arrows.", ledger, H-132, W-ledger-38, 10, False, MUTED)
    d.add(Line(734, H-97, 734, end_y-5, strokeColor=colors.HexColor("#c8d6d2")))
    y = first_y
    for number, step, height in rows:
        mid = y-height/2
        x1, x2 = xs[step["from"]], xs[step["to"]]
        kind = step["kind"]
        color = colors.HexColor(COLORS[kind])
        dash = [5, 3] if kind == "response" else [2, 3] if kind in {"event", "error"} else None
        if x1 == x2:
            x2 = x1 + min(35, spacing/3)
            d.add(Line(x1, mid+6, x2, mid+6, strokeColor=color, strokeWidth=1.5, strokeDashArray=dash))
            d.add(Line(x2, mid+6, x2, mid-6, strokeColor=color, strokeWidth=1.5, strokeDashArray=dash))
            d.add(Line(x2, mid-6, x1, mid-6, strokeColor=color, strokeWidth=1.5, strokeDashArray=dash))
            d.add(Polygon([x1,mid-6,x1+6,mid-3,x1+6,mid-9], fillColor=color, strokeColor=None))
            anchor = x1-22
        else:
            d.add(Line(x1, mid, x2, mid, strokeColor=color, strokeWidth=1.5, strokeDashArray=dash))
            direction = 1 if x2>x1 else -1
            d.add(Polygon([x2,mid,x2-7*direction,mid+3.5,x2-7*direction,mid-3.5], fillColor=color, strokeColor=None))
            anchor = (x1+x2)/2
        # The numbered badge is drawn after the lifeline/arrow for readability.
        d.add(Circle(anchor, mid, 10, fillColor=colors.white, strokeColor=color, strokeWidth=1.2))
        d.add(String(anchor, mid-3.3, f"{number:02}", textAnchor="middle", fontName="Helvetica-Bold", fontSize=9, fillColor=color))
        d.add(String(ledger-24, y-14, f"{number:02}", fontName="Helvetica-Bold", fontSize=10, fillColor=color))
        used = label(d, step["label"], ledger, y-14, 365, 11, True, COLORS[kind], 13)
        label(d, step["detail"], ledger, y-14-used, 365, 10.2, False, INK, 12.2)
        y -= height
    note = "BOUNDARY  " + flow["note"]
    label(d, note, 38, 84, W-76, 10, False, MUTED, 12)
    label(d, "SOURCE  " + " | ".join(flow["sources"]), 38, 48, W-76, 8.5, False, MUTED, 11)
    return d


def mermaid(flow):
    result = ["sequenceDiagram", "  autonumber"]
    for p in flow["participants"]:
        result.append(f'  participant {p["id"]} as {p["name"]}')
    for step in flow["steps"]:
        arrow = "-->>" if step["kind"] in {"response", "event", "error"} else "->>"
        message = f'{NAMES[step["kind"]]}: {step["label"]}<br/>{step["detail"]}'
        message = message.replace(";", "#59;").replace("&", "#38;")
        result.append(f'  {step["from"]}{arrow}{step["to"]}: {message}')
    return "\n".join(result) + "\n"


def overview(data):
    d = base("Request / response architecture", "One native client, one FastAPI process, external model and source APIs. Internal modules are not separate deployed microservices.", "Trace atlas | Implementation audit, not a live-provider or physical-accuracy test | 20 October 2026")
    boxes = [
        (40,510,235,188,"iPhone / web review","phone",["HTTP JSON: setup / frames / geometry", "WS: events + native audio on one socket", "On-phone Core ML + Vision OCR", "Durable selected-evidence outbox"]),
        (320,510,235,188,"FastAPI + live gateway","internal",["Bearer token or paired cookie", "HTTP admission / validation", "Event sockets + voice ownership", "OpenAI Realtime bridge"]),
        (600,510,250,188,"SweepRunner + adapters","internal",["In-process frame / price workers", "Identity, geometry and revisions", "Deterministic packets + usage", "Bounded provider calls"]),
        (900,510,250,188,"EvidenceStore / filesystem","store",["Immutable RGB + depth + metadata", "Observations + price source refs", "Saved inventory + usage ledger", "Claim JSON / HTML / ZIP"]),
        (320,320,235,108,"OpenAI Realtime","cloud",["WSS: PCM + tools / audio + events", "Provider socket owned by gateway"]),
        (600,320,250,108,"OpenAI Responses","cloud",["HTTPS: image / web_search requests", "Observation JSON / source URLs"]),
        (900,320,250,108,"Retailers / catalogue / FX","web",["HTTPS GET: product pages / rates", "Offers / bibliographic facts / FX"]),
    ]
    for x,y,w,h,title,kind,lines in boxes:
        tint={"phone":"#e6f2eb","internal":"#e9f1f9","cloud":"#f0eafa","web":"#faf0de","store":"#f1f4f3"}[kind]
        d.add(Rect(x,y,w,h,rx=9,ry=9,fillColor=colors.HexColor(tint),strokeColor=None))
        label(d,title,x+13,y+h-24,w-26,13,True)
        for i,line in enumerate(lines):label(d,line,x+13,y+h-52-i*26,w-26,10.5)
    def edge(x1,y1,x2,y2,title,tx=None,ty=None):
        color=colors.HexColor(COLORS["request"])
        d.add(Line(x1,y1,x2,y2,strokeColor=color,strokeWidth=2))
        if x2!=x1:
            direction=1 if x2>x1 else -1
            d.add(Polygon([x2,y2,x2-6*direction,y2+3,x2-6*direction,y2-3],fillColor=color,strokeColor=None))
        else:d.add(Polygon([x2,y2,x2-3,y2+6,x2+3,y2+6],fillColor=color,strokeColor=None))
        if title:
            label(d,title,tx if tx is not None else (x1+x2)/2-55,ty if ty is not None else (y1+y2)/2+13,150,9,True,COLORS["request"])
    edge(275,609,320,609,"HTTP / WS",255,716)
    edge(555,609,600,609,"in-process",543,716)
    edge(850,609,900,609,"read / persist",836,716)
    edge(438,510,438,428,"stream + return events",329,465)
    edge(725,510,725,428,"vision / search + result",610,465)
    # Runner calls public sources; this does not originate in the file store.
    d.add(Line(830,510,830,482,strokeColor=colors.HexColor(COLORS["request"]),strokeWidth=2))
    d.add(Line(830,482,1025,482,strokeColor=colors.HexColor(COLORS["request"]),strokeWidth=2))
    edge(1025,482,1025,428,"source GET + result",890,492)
    label(d,"Routes show the caller. Reverse responses/events use the same connection; the numbered sequences below show each direction separately.",40,296,W-80,10,False,MUTED)
    label(d,"Detailed sequence index",40,270,W-80,17,True)
    for i,flow in enumerate(data["flows"]):
        col=i//6;row=i%6
        label(d,flow["id"]+"  "+flow["title"],40+col*565,242-row*27,540,11.5,True)
    label(d,"Green solid: network request  |  Blue dashed: network/result response  |  Purple dotted: asynchronous event  |  Grey solid: internal call",40,53,W-80,10,False,MUTED)
    return d


def endpoint_pages(data):
    pages=[]
    rows=data["endpoints"]
    for offset in range(0,len(rows),14):
        d=base("Endpoint and contract index", "Active routes and optional workflows; legacy voice HTTP routes remain implemented but are not used by the active native Realtime flow.", "Common errors: 401 pairing, 403 Origin, 413 body size, 422 validation; operation-specific alternatives appear in each flow.")
        columns=[(40,65,"Method"),(115,360,"Route"),(492,260,"Result / transport"),(770,380,"Purpose / flow")]
        for x,width,title in columns:label(d,title,x,H-120,width,12,True)
        y=H-146
        for i,row in enumerate(rows[offset:offset+14]):
            h=max(len(wrap(str(row[k]),width,10.5))*13 for (x,width,_),k in zip(columns,["method","path","result","purpose"]))+14
            if i%2==0:d.add(Rect(38,y-h+11,W-76,h,fillColor=colors.HexColor(PALE),strokeColor=None))
            for (x,width,_),key in zip(columns,["method","path","result","purpose"]):label(d,str(row[key]),x,y,width,10.5,key=="method")
            y-=h
        assert y>65,"Endpoint page overflows; reduce rows per page"
        pages.append(d)
    return pages


def validate(data):
    assert len({f["id"] for f in data["flows"]})==len(data["flows"])
    for flow in data["flows"]:
        ids={p["id"] for p in flow["participants"]}
        assert len(ids)==len(flow["participants"])
        for step in flow["steps"]:
            assert step["kind"] in COLORS and step["from"] in ids and step["to"] in ids
        for ref in flow["sources"]:
            path,line=ref.rsplit(":",1)
            assert (ROOT/path).is_file(),ref
            assert 1<=int(line)<=len((ROOT/path).read_text().splitlines()),ref


def main():
    data=json.loads(DATA.read_text());validate(data)
    DEST.mkdir(parents=True,exist_ok=True);OUT.parent.mkdir(parents=True,exist_ok=True)
    for flow in data['flows']:
        for old in DEST.glob(flow['id']+'*.svg'):old.unlink()
    pages=[overview(data)]
    renderSVG.drawToFile(pages[0],str(DEST/"00-service-map.svg"))
    markdown=["# Request and response flows", "", "As implemented on 20 October 2026. The diagrams trace source code; they are not evidence of successful live-provider calls or physical accuracy.", "", "The FastAPI API, Realtime bridge, SweepRunner, vision/price workers, tracking/geometry and EvidenceStore execute in one server process. Localhost/LAN HTTP and WebSocket connect the phone; HTTPS/WSS connect external providers. There is no microservice bus, durable distributed worker or SSE endpoint in this implementation.", "", "Solid arrows represent requests/internal calls, blue dashed arrows responses, and purple dotted arrows asynchronous events. Numbered arrows match the payload/result ledger. Optional and failure paths are explicitly labelled. A combined step can summarize several in-process calls; it does not invent a network hop.", "", "[Download the flow atlas](../../output/pdf/service-flow-diagrams.pdf) · [Full report with these diagrams](../../output/pdf/technical-report.pdf)", "", "![Service map](flows/00-service-map.svg)", ""]
    for flow in data["flows"]:
        chunks=pages_for(flow)
        (DEST/(flow["id"]+".mmd")).write_text(mermaid(flow))
        markdown.extend(["## "+flow["id"]+" - "+flow["title"],"",flow["subtitle"],"",f'[Editable Mermaid](flows/{flow["id"]}.mmd)',""])
        for i,rows in enumerate(chunks,1):
            drawing=sequence_page(flow,rows,i,len(chunks));pages.append(drawing)
            name=flow["id"]+(f"-{i}" if len(chunks)>1 else "")+".svg"
            renderSVG.drawToFile(drawing,str(DEST/name))
            markdown.extend([f'![{flow["title"]} - part {i}](flows/{name})',""])
        markdown.extend(["**Boundary:** "+flow["note"],"", "Sources: "+", ".join(f'[{ref}]({REPO}/blob/main/{ref.rsplit(":",1)[0]}#L{ref.rsplit(":",1)[1]})' for ref in flow["sources"]),""])
    pages.extend(endpoint_pages(data))
    markdown.extend(["## HTTP and WebSocket index","","| Method | Route | Result / transport | Purpose / flow |","|---|---|---|---|"])
    markdown.extend("| "+" | ".join(str(row[k]) for k in ["method","path","result","purpose"])+" |" for row in data["endpoints"])
    (ROOT/"docs/submission/service-flows.md").write_text("\n".join(markdown)+"\n")
    c=canvas.Canvas(str(OUT),pagesize=(W,H),pageCompression=1)
    c.setTitle("Library Claim - Request and Response Flow Atlas");c.setAuthor("Tushar Kalsi")
    for i,page in enumerate(pages,1):
        renderPDF.draw(page,c,0,0)
        c.setFont("Helvetica",8.5);c.setFillColor(colors.HexColor(MUTED));c.drawRightString(W-38,20,f"Flow atlas {i}/{len(pages)}")
        c.showPage()
    c.save()
    print(f"Rendered {len(data['flows'])} flows across {len(pages)} atlas pages: {OUT}")


if __name__=="__main__":main()
