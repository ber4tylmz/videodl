import os, re, uuid, time, shutil, tempfile, threading, socket, ipaddress
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI()
TMP = tempfile.mkdtemp(prefix="vdl_")
JOBS = {}
COOKIES = os.getenv("COOKIES_FILE")
PROXY = os.getenv("PROXY_URL")
MAX_MB = int(os.getenv("MAX_MB", "0"))  # 0 = sınırsız

class Req(BaseModel):
    url: str
    format: str = "mp4"
    quality: str = "best"

def check(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Geçerli bir link gir.")
    try:
        for _, _, _, _, a in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(a[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(400, "Bu adres desteklenmiyor.")
    except socket.gaierror:
        raise HTTPException(400, "Adres bulunamadı.")

def base_opts():
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20}
    if COOKIES and os.path.exists(COOKIES): o["cookiefile"] = COOKIES
    if PROXY: o["proxy"] = PROXY
    return o

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/info")
def info(r: Req):
    check(r.url)
    try:
        with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as y:
            d = y.extract_info(r.url, download=False)
        return {"title": d.get("title"), "thumb": d.get("thumbnail"),
                "uploader": d.get("uploader"), "duration": d.get("duration")}
    except Exception as e:
        raise HTTPException(400, "Video bulunamadı veya site erişimi engelledi. " + str(e)[-150:])

def run(jid, r: Req):
    out = os.path.join(TMP, jid)
    os.makedirs(out, exist_ok=True)
    opts = {**base_opts(), "outtmpl": os.path.join(out, "%(title).60B.%(ext)s"),
            "restrictfilenames": True}
    if MAX_MB: opts["max_filesize"] = MAX_MB * 1024 * 1024
    if r.format == "mp3":
        opts.update(format="bestaudio/best", postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}])
    else:
        if r.quality == "best":
            opts.update(format="bv*+ba/b", merge_output_format="mkv")
        else:
            q = r.quality if r.quality in ("2160", "1440", "1080", "720", "480") else "1080"
            h = f"[height<={q}]"
            opts.update(format=f"bv*[ext=mp4]{h}+ba[ext=m4a]/bv*{h}+ba/b{h}/best",
                        merge_output_format="mp4")
    try:
        JOBS[jid]["status"] = "working"
        with yt_dlp.YoutubeDL(opts) as y:
            d = y.extract_info(r.url, download=True)
        files = [f for f in os.listdir(out) if not f.endswith((".part", ".ytdl"))]
        if not files: raise Exception("Dosya indirilemedi (boyut sınırı aşılmış olabilir).")
        f = max(files, key=lambda x: os.path.getsize(os.path.join(out, x)))
        JOBS[jid].update(status="done", file=os.path.join(out, f), title=d.get("title"))
    except Exception as e:
        JOBS[jid].update(status="error", error=str(e)[-200:])

@app.post("/api/download")
def download(r: Req):
    check(r.url)
    jid = uuid.uuid4().hex
    JOBS[jid] = {"status": "queued", "t": time.time()}
    threading.Thread(target=run, args=(jid, r), daemon=True).start()
    return {"id": jid}

@app.get("/api/status/{jid}")
def status(jid: str):
    j = JOBS.get(jid)
    if not j: raise HTTPException(404, "İş bulunamadı.")
    return {"status": j["status"], "error": j.get("error")}

@app.get("/api/file/{jid}")
def file(jid: str):
    j = JOBS.get(jid)
    if not j or j["status"] != "done":
        raise HTTPException(404, "Dosya hazır değil veya süresi doldu.")
    return FileResponse(j["file"], filename=os.path.basename(j["file"]),
                        media_type="application/octet-stream")

def cleaner():
    while True:
        time.sleep(120)
        for k, v in list(JOBS.items()):
            if time.time() - v["t"] > 1800:
                shutil.rmtree(os.path.join(TMP, k), ignore_errors=True)
                JOBS.pop(k, None)

threading.Thread(target=cleaner, daemon=True).start()

app.mount("/", StaticFiles(directory="static", html=True), name="static")
