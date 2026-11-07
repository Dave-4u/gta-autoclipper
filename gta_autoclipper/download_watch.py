"""Progress-aware, cancellable subprocess supervision; never log raw remote output."""
import collections
import math
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from . import procutil


@dataclass
class ProgressClock:
    started: float
    last_active: float
    maximum: float = 0

    def observe(self, value, now):
        if value > self.maximum:
            self.maximum = value
            self.last_active = now

    def expired(self, now, idle_seconds, total_seconds):
        if now-self.started >= total_seconds:
            return 'source_budget'
        if now-self.last_active >= idle_seconds:
            return 'inactivity_stall'
        return None


class SourceHealth:
    def __init__(self, limit=3):
        self.lock=threading.Lock()
        self.failed=set()
        self.streak=0
        self.limit=limit

    def __deepcopy__(self, memo):
        return self  # one shared run state across per-window config copies

    def allowed(self, source):
        with self.lock:
            return source not in self.failed and self.streak < self.limit

    def record(self, source, success):
        with self.lock:
            if success:
                self.streak=0
            else:
                self.failed.add(source)
                self.streak+=1


def diagnostic_reason(text, fallback='extractor_error'):
    low=(text or '').lower()
    if any(x in low for x in ('sign in to confirm','not a bot','login required','cookies')):
        return 'authentication_required'
    if any(x in low for x in ('403','forbidden','video is not available','private video','copyright')):
        return 'source_unavailable'
    if any(x in low for x in ('429','503','502','timed out','connection reset','temporary failure')):
        return 'transient_network'
    if 'ffmpeg' in low:
        return 'ffmpeg_failed'
    return fallback


def run_download(cmd, *, out_dir, stem, total_seconds, idle_seconds=45, tick=.25):
    """Drain stdout+stderr, distinguish actual progress from warning chatter, finite wall."""
    total_seconds=max(.1,min(240.0,float(total_seconds)))
    idle_seconds=max(.1,min(90.0,float(idle_seconds)))
    opts=dict(stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',bufsize=1)
    if os.name=='nt':
        opts['creationflags']=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)
    else:
        opts['start_new_session']=True
    p=subprocess.Popen(cmd,**opts)
    procutil.register(p)
    events=queue.Queue(maxsize=256)
    tail=collections.deque(maxlen=48)
    def drain(stream):
        try:
            for line in stream:
                try:
                    events.put_nowait(line[-2048:])
                except queue.Full:
                    pass
        finally:
            stream.close()
    readers=[threading.Thread(target=drain,args=(s,),daemon=True) for s in (p.stdout,p.stderr)]
    for t in readers:t.start()
    now=time.monotonic(); clock=ProgressClock(now,now)
    max_bytes=0; max_media=0; last_notice=now
    reason=None
    try:
        while True:
            now=time.monotonic()
            while True:
                try:line=events.get_nowait()
                except queue.Empty:break
                tail.append(line)
                byte_match=re.search(r'gta_progress:(\d+)',line)
                if byte_match:max_bytes=max(max_bytes,int(byte_match[1]))
                media=re.search(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)',line)
                if media:max_media=max(max_media,int(media[1])*3600+int(media[2])*60+float(media[3]))
            try:
                size=sum(x.stat().st_size for x in out_dir.glob(stem+'*') if x.is_file())
                max_bytes=max(max_bytes,size)
            except OSError:pass
            # Increasing media position or file bytes counts; repeated warnings do not.
            clock.observe(max_bytes+max_media*1e9,now)
            if procutil.cancelled():reason='cancelled'
            else:reason=clock.expired(now,idle_seconds,total_seconds)
            if p.poll() is not None:break
            if reason:
                procutil._kill_one(p)
                break
            if now-last_notice >= 10:
                print(f'[download-progress] bytes={max_bytes} media_seconds={max_media:.1f} elapsed={now-clock.started:.0f}s',flush=True)
                last_notice=now
            time.sleep(tick)
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:procutil._kill_one(p)
        for t in readers:t.join(timeout=1)
        while not events.empty():tail.append(events.get_nowait())
        text=''.join(tail)
        if p.returncode==0 and not reason:return True,'complete',text
        return False,reason or diagnostic_reason(text),text
    finally:
        if p.poll() is None:procutil._kill_one(p)
        procutil.unregister(p)
