"""Local-only parser for the SRC-06 unofficial closure/reopen listings."""
from __future__ import annotations
import hashlib, json, os, re, tempfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Any

MAX_HTML_BYTES = 20 * 1024 * 1024
SOURCE_URLS = {
    "type6": "https://r3.quicca.com/~postal/kaihai_type.php?type=6",
    "type7": "https://r3.quicca.com/~postal/kaihai_type.php?type=7",
    "current_closed": "https://r3.quicca.com/~postal/view_closed.php",
}
HEADERS = {"type6": ("実施日", "局名", "住所", "備考"), "type7": ("実施日", "局名", "住所", "備考"),
           "current_closed": ("局名", "所在地", "貯", "閉鎖年月日", "閉鎖期間", "備考")}

class Src06Error(ValueError): pass

def source_document_id(content_sha256: str) -> str:
    return "src06-" + hashlib.sha256(content_sha256.encode()).hexdigest()

def _read(path: str | Path) -> tuple[str, str]:
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0 or p.stat().st_size > MAX_HTML_BYTES: raise Src06Error("SRC-06 HTML rejected")
    data = p.read_bytes()
    if b"\x00" in data: raise Src06Error("SRC-06 binary input rejected")
    try: text = data.decode("utf-8")
    except UnicodeDecodeError:
        try: text = data.decode("cp932")
        except UnicodeDecodeError as exc: raise Src06Error("SRC-06 encoding rejected") from exc
    return text, hashlib.sha256(data).hexdigest()

class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.tables=[]; self.table=None; self.row=None; self.cell=None; self.href=None; self.title=[]; self.title_count=0; self.in_title=False; self.skip_depth=0
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag.lower() in {"script", "style"}: self.skip_depth += 1; return
        if tag.lower()=="title": self.in_title=True; self.title=[]; self.title_count += 1; return
        if self.skip_depth: return
        if tag.lower()=="table": self.table=[]
        elif tag.lower()=="tr" and self.table is not None: self.row=[]
        elif tag.lower() in {"th","td"} and self.row is not None: self.cell=[]; self.href=None
        elif tag.lower()=="a" and self.cell is not None: self.href=attrs.get("href")
    def handle_data(self,data):
        if self.skip_depth: return
        if self.in_title: self.title.append(data)
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self,tag):
        tag=tag.lower()
        if tag in {"script", "style"} and self.skip_depth: self.skip_depth -= 1; return
        if tag=="title": self.in_title=False; return
        if self.skip_depth: return
        if tag in {"th","td"} and self.cell is not None:
            self.row.append({"text": re.sub(r"\s+"," ","".join(self.cell)).strip(), "href": self.href}); self.cell=None; self.href=None
        elif tag=="tr" and self.row is not None and self.table is not None: self.table.append(self.row); self.row=None
        elif tag=="table" and self.table is not None: self.tables.append(self.table); self.table=None

def _date(raw: str) -> str | None:
    m=re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日",raw)
    if not m: return None
    try: return datetime(int(m.group(1)),int(m.group(2)),int(m.group(3))).date().isoformat()
    except ValueError: return None

def _safe_href(href: str | None, source_url: str) -> str | None:
    if not href: return None
    u=urljoin(source_url,href); p=urlparse(u); base=urlparse(source_url)
    if p.scheme not in {"http","https"} or p.netloc != base.netloc or not p.path.startswith("/~postal/"): return None
    return u

def parse_src06_html(data: bytes | str, source_kind: str, *, retrieved_at: str, source_url: str | None = None) -> dict[str, Any]:
    if source_kind not in SOURCE_URLS: raise Src06Error("unknown SRC-06 source kind")
    url=source_url or SOURCE_URLS[source_kind]
    if url != SOURCE_URLS[source_kind]: raise Src06Error("SRC-06 source URL mismatch")
    if isinstance(data,str):
        raw=data.encode("utf-8")
        if len(raw)>MAX_HTML_BYTES or b"\x00" in raw: raise Src06Error("SRC-06 HTML rejected")
        text=data; digest=hashlib.sha256(raw).hexdigest()
    else:
        if not data or len(data)>MAX_HTML_BYTES or b"\x00" in data: raise Src06Error("SRC-06 HTML rejected")
        try: text=data.decode("utf-8")
        except UnicodeDecodeError: text=data.decode("cp932")
        digest=hashlib.sha256(data).hexdigest()
    parser=_TableParser(); parser.feed(text)
    title_text=re.sub(r"\s+"," "," ".join(parser.title)).strip()
    expected_title={"type6":"改廃情報 一時閉鎖 | inukugi web", "type7":"改廃情報 再開 | inukugi web", "current_closed":"一時閉鎖中の郵便局 | inukugi web"}[source_kind]
    if parser.title_count != 1 or title_text != expected_title: raise Src06Error("SRC-06 category/title mismatch")
    matching=[t for t in parser.tables if t and tuple(c["text"] for c in t[0])==HEADERS[source_kind]]
    if len(matching)!=1: raise Src06Error("SRC-06 data table is not unique")
    table=matching[0]; qa=[]; events=[]; observations=[]; docid=source_document_id(digest)
    for idx,row in enumerate(table[1:],1):
        vals=[c["text"] for c in row]
        if source_kind in {"type6","type7"}:
            if len(vals) != 4 or any(not vals[pos] for pos in (0, 1, 2)):
                qa.append({"kind":"error","code":"malformed_data_row","row":idx}); continue
            raw_date=vals[0] if vals else ""; iso=_date(raw_date)
            if not iso: qa.append({"kind":"error","code":"invalid_date","row":idx}); continue
            name=vals[1] if len(vals)>1 else ""; address=vals[2] if len(vals)>2 else ""; note=vals[3] if len(vals)>3 else ""
            href=_safe_href(row[1].get("href") if len(row)>1 else None,url)
            if len(row)>1 and row[1].get("href") and href is None: qa.append({"kind":"error","code":"unsafe_detail_url","row":idx}); continue
            basis=f"{digest}|{source_kind}|{idx}|{raw_date}|{name}|{address}|{note}"; event_id=hashlib.sha256(basis.encode()).hexdigest()
            before={"name":{"raw":name},"address":{"raw":address},"operating_status":{"raw":"operating","normalized":"operating"}}
            after={"name":{"raw":name},"address":{"raw":address},"operating_status":{"raw":"temporarily_closed" if source_kind=='type6' else "operating","normalized":"temporarily_closed" if source_kind=='type6' else "operating"}}
            ev={"event_id":event_id,"source_document_id":docid,"source_family":"SRC-06","source_url":url,"source_page":None,"source_line":idx,"source_line_sha256":hashlib.sha256("|".join(vals).encode()).hexdigest(),"notice_date":None,"observed_effective_date":iso,"planned_effective_date":None,"confirmed_effective_date":None,"event_status":"announced","provenance":"unofficial","facility_entity_id":None,"review_status":"unmatched","corroborated_by":[],"revision_of_event_id":None,"cancelled_by_event_id":None,"event_type":"temporarily_closed" if source_kind=='type6' else 'reopened',"before_state":before,"after_state":after,"name_raw":name,"address_raw":address,"reason_raw":note,"detail_url":href,"detail_key":("src06-"+hashlib.sha256((href or '').encode()).hexdigest()) if href else None}
            events.append(ev)
        else:
            if len(vals) != 6 or any(not vals[pos] for pos in (0, 1, 3)):
                qa.append({"kind":"error","code":"malformed_data_row","row":idx}); continue
            iso=_date(vals[3]);
            if not iso: qa.append({"kind":"error","code":"invalid_date","row":idx}); continue
            observations.append({"observation_id":hashlib.sha256(f"{digest}|{idx}|{'|'.join(vals)}".encode()).hexdigest(),"source_document_id":docid,"source_family":"SRC-06","provenance":"unofficial","source_url":url,"observed_at":retrieved_at,"source_row_index":idx,"source_row_sha256":hashlib.sha256("|".join(vals).encode()).hexdigest(),"name_raw":vals[0],"address_raw":vals[1],"savings_raw":vals[2],"closure_start_raw":vals[3],"closure_start_date":iso,"closure_duration_raw":vals[4],"note_raw":vals[5],"is_currently_closed":True})
    return {"events":events,"current_closed_observations":observations,"qa":qa,"metadata":{"source_document_id":docid,"source_family":"SRC-06","source_content_sha256":digest,"source_url":url,"retrieved_at":retrieved_at,"public_release_allowed":False,"raw_redistribution_allowed":False,"source_is_official":False,"terms_review_status":"no_explicit_terms_found","use_scope":"temporary-closure/reopen and long-suspension identification only","legal_evidence_limit":"not direct basis for individual legal finding","event_count":len(events),"observation_count":len(observations),"qa_error_count":sum(q.get('kind')=='error' for q in qa)}}

def parse_src06_file(path: str | Path, source_kind: str, *, retrieved_at: str) -> dict[str, Any]:
    p=Path(path)
    if not p.is_file() or p.stat().st_size == 0 or p.stat().st_size > MAX_HTML_BYTES: raise Src06Error("SRC-06 HTML rejected")
    return parse_src06_html(p.read_bytes(),source_kind,retrieved_at=retrieved_at)

def write_src06_outputs(result: dict[str,Any], output_dir: str|Path, *, replace=False, acknowledge_unofficial_source=False, acknowledge_internal_use=False) -> dict[str,str]:
    if not acknowledge_unofficial_source or not acknowledge_internal_use: raise Src06Error("SRC-06 acknowledgements required")
    out=Path(output_dir); files={"metadata":out/'metadata.json',"events":out/'events.jsonl',"current_closed_observations":out/'current_closed_observations.jsonl',"qa":out/'qa.jsonl'}
    if not replace and any(p.exists() for p in files.values()): raise Src06Error("output exists; pass --replace")
    out.mkdir(parents=True,exist_ok=True); staged=[]
    try:
        vals={"metadata":result['metadata'],"events":result['events'],"current_closed_observations":result['current_closed_observations'],"qa":result['qa']}
        for k,p in files.items():
            fd,n=tempfile.mkstemp(prefix='.'+p.name+'.',dir=out); os.close(fd); q=Path(n); staged.append((k,q))
            with q.open('w',encoding='utf8',newline='\n') as f:
                if k=='metadata': json.dump(vals[k],f,ensure_ascii=False,indent=2); f.write('\n')
                else:
                    for row in vals[k]: f.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
        backups=[]; committed=[]
        try:
            for p in files.values():
                if p.exists():
                    fd,n=tempfile.mkstemp(prefix='.'+p.name+'.backup.',dir=out); os.close(fd); b=Path(n); b.unlink(); os.replace(p,b); backups.append((p,b))
            for k,q in staged: os.replace(q,files[k]); committed.append(files[k])
        except Exception as exc:
            for p in committed:
                if p.exists(): p.unlink()
            for p,b in reversed(backups):
                if b.exists(): os.replace(b,p)
            raise Src06Error('SRC-06 atomic commit failed') from exc
        for _,b in backups:
            if b.exists(): b.unlink()
    finally:
        for _,q in staged:
            if q.exists(): q.unlink()
    return {k:str(v) for k,v in files.items()}
