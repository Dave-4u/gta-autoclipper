(() => {
  const $ = (id) => document.getElementById(id);
  let mode = "single";
  let pollTimer = null;
  let running = false;
  let clockTimer = null;
  let startedAt = null;

  const niche = $("niche");
  const topicField = $("topicField");
  const logEl = $("log");
  const stageStrip = $("stageStrip");
  const gallery = $("gallery");
  const preview = $("preview");
  const tally = document.querySelector(".tally");
  const tallyText = $("tallyText");
  const jobClock = $("jobClock");
  const btnStart = $("btnStart");
  const btnStop = $("btnStop");

  function fmtClock(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    const m = String(Math.floor(s / 60)).padStart(2, "0");
    const r = String(s % 60).padStart(2, "0");
    return `${m}:${r}`;
  }

  function setTally(state, text) {
    tally.classList.remove("live", "done", "err");
    if (state) tally.classList.add(state);
    tallyText.textContent = text;
  }

  function setMode(next) {
    mode = next;
    document.querySelectorAll("#modeSeg button").forEach((b) => {
      b.classList.toggle("on", b.dataset.mode === mode);
    });
    topicField.hidden = mode !== "ranking";
    topicField.style.display = mode === "ranking" ? "" : "none";
  }

  document.querySelectorAll("#modeSeg button").forEach((b) => {
    b.addEventListener("click", () => setMode(b.dataset.mode));
  });

  async function loadMeta() {
    const res = await fetch("/api/meta");
    const data = await res.json();
    niche.innerHTML = "";
    (data.niches || []).forEach((n) => {
      const opt = document.createElement("option");
      opt.value = n.id;
      opt.textContent = n.label || n.id;
      niche.appendChild(opt);
    });
    if (data.defaults?.niche) niche.value = data.defaults.niche;
    $("topMeta").textContent = `OUTPUTS ${data.outputs_count || 0}`;
  }

  function renderStages(stages) {
    stageStrip.innerHTML = "";
    (stages || []).forEach((s) => {
      const el = document.createElement("span");
      el.className = `chip ${s.status || ""}`;
      const elap = s.elapsed_s != null ? ` ${s.elapsed_s}s` : "";
      el.textContent = `${s.name}${elap}`.toUpperCase();
      stageStrip.appendChild(el);
    });
  }

  function renderGallery(items) {
    gallery.innerHTML = "";
    if (!items?.length) {
      gallery.innerHTML = `<div class="meta" style="padding:0.5rem;color:var(--muted);font-family:var(--mono);font-size:0.7rem;">No finished clips yet.</div>`;
      return;
    }
    items.forEach((it) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "card";
      btn.innerHTML = `
        <img src="${it.thumb}" alt="" loading="lazy" onerror="this.style.opacity=0.2" />
        <div class="meta"><strong>${it.name}</strong>${it.niche} · ${(it.size/1048576).toFixed(1)} MB</div>`;
      btn.addEventListener("click", () => {
        document.querySelectorAll(".card").forEach((c) => c.classList.remove("active"));
        btn.classList.add("active");
        preview.src = it.url;
        preview.play().catch(() => {});
      });
      gallery.appendChild(btn);
    });
  }

  async function refreshOutputs() {
    const res = await fetch("/api/outputs");
    const data = await res.json();
    renderGallery(data.items || []);
    $("topMeta").textContent = `OUTPUTS ${(data.items || []).length}`;
  }

  async function poll() {
    const res = await fetch("/api/status");
    const st = await res.json();
    if (st.log) logEl.textContent = st.log;
    logEl.scrollTop = logEl.scrollHeight;
    renderStages(st.stages || []);
    const status = st.status || "idle";
    running = status === "running";
    btnStart.disabled = running;
    btnStop.disabled = !running;
    if (running) {
      setTally("live", (st.current_stage || "RUNNING").toUpperCase());
      if (st.elapsed_s != null) jobClock.textContent = fmtClock(st.elapsed_s);
    } else if (status === "done") {
      setTally("done", "COMPLETE");
      jobClock.textContent = fmtClock(st.elapsed_s);
      clearInterval(pollTimer); pollTimer = null;
      refreshOutputs();
    } else if (status === "error") {
      setTally("err", "FAULT");
      jobClock.textContent = fmtClock(st.elapsed_s);
      clearInterval(pollTimer); pollTimer = null;
      refreshOutputs();
    } else {
      setTally("", "STANDBY");
    }
  }

  async function startJob() {
    const body = {
      niche: niche.value,
      mode,
      topic: $("topic").value || "",
      rank_n: 5,
      max_videos: Number($("maxVideos").value || 1),
      max_clips: Number($("maxClips").value || 2),
      voiceover: $("voiceover").checked,
      captions: $("captions").checked,
      fast: $("fast").checked,
    };
    const res = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      logEl.textContent = `Cannot start: ${err.detail || res.status}`;
      return;
    }
    setTally("live", "RUNNING");
    startedAt = Date.now();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(poll, 800);
    poll();
  }

  async function stopJob() {
    await fetch("/api/stop", { method: "POST" });
    poll();
  }

  btnStart.addEventListener("click", startJob);
  btnStop.addEventListener("click", stopJob);
  $("btnRefresh").addEventListener("click", refreshOutputs);

  loadMeta().then(refreshOutputs).catch((e) => {
    logEl.textContent = String(e);
  });
  setMode("single");
})();
