"""Render the as-built architecture and Markdown submission notes as PDFs.

Optional authoring dependency: reportlab. Outputs do not change app/runtime data.
"""
from pathlib import Path
import html
import re
from urllib.parse import quote

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.graphics.shapes import Drawing, Rect, String, PolyLine, Polygon
from reportlab.graphics import renderPDF, renderSVG
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Preformatted, KeepTogether

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/pdf'
ASSETS=ROOT/'docs/submission/assets'
REPO='https://github.com/Tushar-kalsi-virtualfusion/cozmo-library-claim-agent'
FONT='Helvetica'
BOLD='Helvetica-Bold'
arial=Path('/System/Library/Fonts/Supplemental/Arial.ttf')
if arial.exists():
    pdfmetrics.registerFont(TTFont('LCArial',str(arial)))
    pdfmetrics.registerFont(TTFont('LCArialBold',str(arial.with_name('Arial Bold.ttf'))))
    FONT,BOLD='LCArial','LCArialBold'
    pdfmetrics.registerFontFamily(FONT,normal=FONT,bold=BOLD,italic=FONT,boldItalic=BOLD)

INK=colors.HexColor('#183b37');MUTED=colors.HexColor('#536765')
GREEN=colors.HexColor('#16715e');BLUE=colors.HexColor('#21699b')
PURPLE=colors.HexColor('#6f4aa4');ORANGE=colors.HexColor('#a36521')

def clean(text):
    return (text.replace('\u2011','-').replace('\u2013','-').replace('\u2014',' - ')
            .replace('→',' -> ').replace('≤','<= ').replace('≥','>= ').replace('₹','INR ')
            .replace('²','2').replace('×','x').replace('≈','~').replace('−','-'))

def architecture_diagram():
    d=Drawing(1120,550)
    for x,label,color in [(10,'IPHONE 17 PRO',GREEN),(410,'PYTHON / FASTAPI SERVER',BLUE),(810,'HOSTED MODELS + SOURCES',PURPLE)]:
        d.add(Rect(x,10,300,530,rx=14,ry=14,fillColor=colors.HexColor('#f5f8f8'),strokeColor=None))
        d.add(String(x+15,520,label,fontName=BOLD,fontSize=17,fillColor=color))
    def block(x,y,title,lines,color):
        d.add(Rect(x+10,y,280,70,rx=8,ry=8,fillColor=colors.white,strokeColor=color,strokeWidth=1.3))
        d.add(String(x+23,y+49,title,fontName=BOLD,fontSize=16,fillColor=INK))
        for n,line in enumerate(lines):d.add(String(x+23,y+29-n*17,line,fontName=FONT,fontSize=14,fillColor=MUTED))
    block(10,430,'Microphone + speaker',['PCM stream / echo cancellation','Start, spoken setup, interruptions'],PURPLE)
    block(10,330,'Camera + LiDAR / RoomPlan',['RGB, depth, confidence, calibration','AR pose + same-session geometry'],BLUE)
    block(10,230,'Local detection + OCR',['YOLOE-26s Core ML / Vision text','Provisional labels + dotted outlines'],GREEN)
    block(10,130,'Select + retain + upload',['Quality / novelty / duplicate gates','Durable outbox; selected JPEGs'],GREEN)
    block(10,30,'Live and saved inventory',['Verified state, source links, values','Room dimensions + review queue'],GREEN)
    block(410,430,'Realtime session gateway',['PCM relay, action validation','Readiness, corrections, grounded cues'],PURPLE)
    block(410,330,'Measurement + geometry',['Confident depth / calibrated rays','RoomPlan polygons + furniture boxes'],BLUE)
    block(410,230,'Recognition + identity',['Bounded vision workers / OCR checks','RoomPlan, depth, visual association'],GREEN)
    block(410,130,'Async valuation workers',['Fetch / parse / validate retailer offers','Exact vs comparable; unknowns kept'],ORANGE)
    block(410,30,'Evidence + deterministic packet',['Immutable files, revisions, cost ledger','JSON / HTML / ZIP + live events'],BLUE)
    block(810,430,'OpenAI GPT-Realtime-2.1',['Native speech-to-speech; Marin','No separate STT -> LLM -> TTS chain'],PURPLE)
    block(810,230,'OpenAI GPT-6 Astra',['Selected original-image recognition','Structured observations, not prices'],GREEN)
    block(810,130,'Search API + public sources',['GPT-5.4 mini / web_search','Retailer pages + dated FX sources'],ORANGE)
    d.add(String(829,380,'Separate concurrent channels',fontName=BOLD,fontSize=16,fillColor=INK))
    for n,line in enumerate(['Voice continues during image work.','The cloud receives selected stills,','not the full continuous video stream.']):
        d.add(String(829,358-n*20,line,fontName=FONT,fontSize=14,fillColor=MUTED))
    def arrow(points,color,label=None,at=None,both=False):
        d.add(PolyLine(points,strokeColor=color,strokeWidth=1.8,fillColor=None))
        def head(a,b):
            import math
            theta=math.atan2(b[1]-a[1],b[0]-a[0]);size=7
            p=[b[0],b[1],b[0]-size*math.cos(theta-.5),b[1]-size*math.sin(theta-.5),
               b[0]-size*math.cos(theta+.5),b[1]-size*math.sin(theta+.5)]
            d.add(Polygon(p,fillColor=color,strokeColor=None))
        pairs=list(zip(points[::2],points[1::2]));head(pairs[-2],pairs[-1])
        if both:head(pairs[1],pairs[0])
        if label:
            for n,line in enumerate(label.split('|')):
                d.add(String(at[0],at[1]-n*14,line,fontName=FONT,fontSize=12,fillColor=color,textAnchor='middle'))
    arrow([300,465,420,465],PURPLE,'PCM / tools',(360,486),True)
    arrow([700,465,820,465],PURPLE,'WebSocket',(760,486),True)
    arrow([160,330,160,300],GREEN)
    arrow([160,230,160,200],GREEN)
    arrow([300,365,420,365],BLUE,'Depth +|geometry',(360,386))
    arrow([300,165,345,165,345,265,420,265],GREEN,'JPEG +|metadata',(361,296))
    arrow([700,265,820,265],GREEN,'Vision API',(760,286),True)
    arrow([560,230,560,200],ORANGE,'enqueue',(602,211))
    arrow([700,165,820,165],ORANGE,'Search / fetch',(760,186),True)
    arrow([560,130,560,100],BLUE)
    arrow([420,65,300,65],GREEN,'Inventory /|overlays',(360,89))
    arrow([700,365,722,365,722,88,700,88],BLUE)
    arrow([700,245,744,245,744,73,700,73],GREEN)
    arrow([700,50,777,50,777,445,700,445],PURPLE)
    d.add(String(795,65,'State -> voice',fontName=FONT,fontSize=12,fillColor=PURPLE))
    return d

def coordination_diagram():
    """Printable summary of the detailed Mermaid sequence in the source report."""
    d=Drawing(600,610)
    lanes=[(65,'iPhone'),(225,'Paired server'),(385,'Realtime'),(545,'Workers / web')]
    for x,label in lanes:
        d.add(Rect(x-52,572,104,26,rx=5,ry=5,fillColor=colors.HexColor('#e3efea'),strokeColor=None))
        d.add(String(x,581,label,textAnchor='middle',fontName=BOLD,fontSize=11,fillColor=INK))
        d.add(PolyLine([x,570,x,22],strokeColor=colors.HexColor('#c4d4d0'),strokeWidth=1,strokeDashArray=[3,3]))
    def phase(y,label):
        d.add(Rect(7,y-5,586,20,fillColor=colors.HexColor('#f0f5f8'),strokeColor=None))
        d.add(String(15,y,label,fontName=BOLD,fontSize=11,fillColor=BLUE))
    def message(y,a,b,label,color=GREEN):
        x1,x2=lanes[a][0],lanes[b][0]
        d.add(PolyLine([x1,y,x2,y],strokeColor=color,strokeWidth=1.4))
        direction=1 if x2>x1 else -1
        d.add(Polygon([x2,y,x2-6*direction,y+3,x2-6*direction,y-3],fillColor=color,strokeColor=None))
        d.add(String((x1+x2)/2,y+6,label,textAnchor='middle',fontName=FONT,fontSize=10,fillColor=INK))
    phase(550,'1  Spoken setup and explicit readiness')
    message(528,0,1,'Start + spoken answers',PURPLE)
    message(504,1,2,'Persistent voice session',PURPLE)
    message(480,2,0,'Readback / speech through paired server',PURPLE)
    message(456,0,1,'Played audio + confirmation',PURPLE)
    message(432,1,0,'Separate readiness gate')
    phase(402,'2  Concurrent work during the sweep - these are independent channels')
    message(378,0,2,'PCM in / streamed audio out through server',PURPLE)
    message(352,0,1,'Selected RGB + depth')
    message(326,1,3,'Bounded recognition; geometry; queued pricing')
    message(300,3,1,'Validated observations and retrieved offers')
    message(274,1,0,'Inventory revisions / outlines')
    message(248,0,1,'RoomPlan updates',BLUE)
    message(222,1,2,'Fresh saved facts / cues',PURPLE)
    phase(192,'3  Finish - flush evidence before the final packet')
    message(166,0,1,'End; final room + outbox',BLUE)
    message(140,1,3,'Drain jobs within deadline',BLUE)
    message(114,3,1,'Results / explicit unknowns',BLUE)
    message(88,1,0,'JSON / HTML / evidence ZIP',BLUE)
    message(62,1,2,'Grounded final summary',PURPLE)
    d.add(String(15,8,'Preview video stays on device. Only selected stills and calibrated sensor evidence are uploaded.',fontName=FONT,fontSize=10,fillColor=MUTED))
    return d

def paragraph(canvas_,text,x,y,width,size=9,leading=12):
    style=ParagraphStyle('note',fontName=FONT,fontSize=size,leading=leading,textColor=INK)
    p=Paragraph(text,style);_,height=p.wrap(width,900);p.drawOn(canvas_,x,y-height);return height

def make_architecture_pdf(diagram):
    path=OUT/'architecture-note.pdf';c=canvas.Canvas(str(path),pagesize=landscape(A4))
    c.setTitle('Library Claim - Hybrid Architecture');c.setAuthor('Tushar Kalsi')
    width,height=landscape(A4)
    c.setFillColor(INK);c.setFont(BOLD,21);c.drawString(32,height-36,'Library Claim | Hybrid architecture')
    c.setFont(FONT,9);c.setFillColor(MUTED);c.drawString(32,height-53,'Tushar Kalsi  /  Cozmo assessment  /  As implemented on 20 October 2026')
    scale=(width-64)/diagram.width
    c.saveState();c.translate(32,143);c.scale(scale,scale);renderPDF.draw(diagram,c,0,0);c.restoreState()
    paragraph(c,'<b>Metric scale.</b> LiDAR supplies depth in metres. Camera intrinsics and AR poses align confident depth with the observed object. Plane intersections measure book spines; RoomPlan polygons and oriented boxes supply room/furniture geometry. Camera-only mode leaves unsupported metric values unknown.',32,130,370,8.4,11)
    paragraph(c,'<b>Price and truth boundaries.</b> Search discovers URLs; server code accepts retrieved, dated offers and computes totals. Labels do not provide dimensions or prices. The local detector is a vision model, not a text SLM. Apple language-model, Claude and ElevenLabs paths are inactive in this build.',436,130,370,8.4,11)
    paragraph(c,'<b>Evidence status.</b> 362 Python tests and an unsigned iPhone build pass. The submitted capture is partial, independent ground truth is absent, and current cloud pricing is blocked by exhausted API credit. Full detail and failure analysis accompany this note.',32,64,width-64,8.4,11)
    c.setFont(FONT,7);c.setFillColor(MUTED);c.drawString(32,19,'Source: repository implementation; observed results are documented separately from design capabilities.')
    c.showPage();c.save()

styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='LCBody',fontName=FONT,fontSize=9.4,leading=13.2,textColor=INK,spaceAfter=6))
styles.add(ParagraphStyle(name='LCH1',fontName=BOLD,fontSize=22,leading=27,textColor=INK,spaceAfter=16))
styles.add(ParagraphStyle(name='LCH2',fontName=BOLD,fontSize=13.2,leading=17,textColor=GREEN,spaceBefore=12,spaceAfter=7,keepWithNext=True))
styles.add(ParagraphStyle(name='LCH3',fontName=BOLD,fontSize=10.6,leading=14,textColor=INK,spaceBefore=9,spaceAfter=5,keepWithNext=True))
styles.add(ParagraphStyle(name='LCCode',fontName='Courier',fontSize=7.3,leading=10,textColor=INK,backColor=colors.HexColor('#f1f5f5'),spaceAfter=8))
styles.add(ParagraphStyle(name='LCTable',fontName=FONT,fontSize=8,leading=10.5,textColor=INK))

def inline(text,source):
    text=html.escape(clean(text.strip()))
    def link(match):
        target=html.unescape(match.group(2))
        if not re.match(r'^[a-z]+:',target):
            target=REPO+'/blob/main/'+quote(str((source.parent/target).resolve().relative_to(ROOT)),safe='/#')
        return f'<link href="{html.escape(target,quote=True)}" color="#21699b">{match.group(1)}</link>'
    text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,text)
    text=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',text)
    return re.sub(r'`([^`]+)`',r'<font name="Courier">\1</font>',text)

def markdown_flow(path,diagram=None,compact=False):
    lines=path.read_text().splitlines();flow=[];i=0
    while i<len(lines):
        line=lines[i].strip();i+=1
        if not line:continue
        if line.startswith('```'):
            language=line[3:];code=[]
            while i<len(lines) and not lines[i].strip().startswith('```'):code.append(lines[i]);i+=1
            i+=1
            if language in {'mermaid','dot'}:
                drawing=coordination_diagram()
                factor=(A4[0]-76)/drawing.width
                drawing.scale(factor,factor);drawing.width*=factor;drawing.height*=factor
                flow.extend([drawing,Spacer(1,10)])
            else:
                import textwrap
                wrapped='\n'.join(part for s in code for part in (textwrap.wrap(clean(s),100,replace_whitespace=False,drop_whitespace=False) or ['']))
                flow.append(Preformatted(wrapped,styles['LCCode']))
        elif line.startswith('|'):
            rows=[line]
            while i<len(lines) and lines[i].strip().startswith('|'):rows.append(lines[i].strip());i+=1
            rows=[r for r in rows if not re.fullmatch(r'[|:\-\s]+',r)]
            cells=[[Paragraph(inline(s,path),styles['LCTable']) for s in r.strip('|').split('|')] for r in rows]
            count=max(map(len,cells));cells=[row+[Paragraph('',styles['LCTable'])]*(count-len(row)) for row in cells]
            table=Table(cells,colWidths=[(A4[0]-76)/count]*count,repeatRows=1,hAlign='LEFT')
            table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e3efea')),('VALIGN',(0,0),(-1,-1),'TOP'),('GRID',(0,0),(-1,-1),.3,colors.HexColor('#c8d5d2')),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
            flow.extend([table,Spacer(1,8)])
        elif line.startswith('#'):
            level=len(line)-len(line.lstrip('#'))
            flow.append(Paragraph(inline(line.lstrip('# '),path),styles['LCH1' if level==1 else 'LCH2' if level==2 else 'LCH3']))
            if level==1 and diagram:
                drawing=architecture_diagram();factor=(A4[0]-76)/drawing.width
                drawing.scale(factor,factor);drawing.width*=factor;drawing.height*=factor
                flow.extend([drawing,Spacer(1,10)])
        elif re.match(r'^([-*]|\d+\.)\s',line):
            text=re.sub(r'^([-*]|\d+\.)\s+','',line)
            flow.append(Paragraph('&#8226; '+inline(text,path),styles['LCBody']))
        else:
            group=[line]
            while i<len(lines) and lines[i].strip() and not re.match(r'^(#|\||```|[-*] |\d+\. )',lines[i].strip()):group.append(lines[i].strip());i+=1
            flow.append(Paragraph(inline(' '.join(group),path),styles['LCBody']))
    return flow

def footer(c,doc):
    c.saveState();c.setFont(FONT,7.5);c.setFillColor(MUTED)
    c.drawString(38,24,'Library Claim | Tushar Kalsi | 20 October 2026')
    c.drawRightString(A4[0]-38,24,str(doc.page));c.restoreState()

def make_report():
    path=OUT/'technical-report.pdf'
    doc=SimpleDocTemplate(str(path),pagesize=A4,rightMargin=38,leftMargin=38,topMargin=35,bottomMargin=40,title='Library Claim - Full Technical Report',author='Tushar Kalsi')
    doc.build(markdown_flow(ROOT/'docs/submission/technical-report.md',diagram=True),onFirstPage=footer,onLaterPages=footer)

def make_failure():
    # A one-page note must remain readable; fail visibly if its content overflows.
    path=OUT/'failure-log.pdf'
    doc=SimpleDocTemplate(str(path),pagesize=A4,rightMargin=38,leftMargin=38,topMargin=32,bottomMargin=36,title='Library Claim - Failure Log',author='Tushar Kalsi')
    doc.build(markdown_flow(ROOT/'docs/submission/failure-log.md'),onFirstPage=footer,onLaterPages=footer)

def main():
    OUT.mkdir(parents=True,exist_ok=True);ASSETS.mkdir(parents=True,exist_ok=True)
    diagram=architecture_diagram();renderSVG.drawToFile(diagram,str(ASSETS/'hybrid-architecture.svg'))
    renderSVG.drawToFile(coordination_diagram(),str(ASSETS/'voice-video-coordination.svg'))
    make_architecture_pdf(diagram);make_report();make_failure()
    for path in OUT.glob('*.pdf'):print(path)

if __name__=='__main__':main()
