"use strict";

/* 까치녹음기 화면. 프레임워크 없이 동작한다 (인터넷 없이도 열려야 하므로 외부 파일 없음). */

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");

const state = {
  recordings: [],
  subjects: [],
  pendingFile: null,
  uploading: false,
  pollTimer: null,
};

/* ---------- 공통 ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = "문제가 생겼어요. 잠시 후 다시 시도해 주세요.";
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3000);
}

function confirmDialog(text, okLabel = "삭제", { html = false, primary = false, cancelLabel = "취소" } = {}) {
  const dlg = $("#confirm-dialog");
  $("#confirm-text")[html ? "innerHTML" : "textContent"] = text;
  $("#confirm-ok").textContent = okLabel;
  $("#confirm-ok").className = `btn ${primary ? "btn-primary" : "btn-danger"}`;
  $("#confirm-cancel").textContent = cancelLabel;
  dlg.showModal();
  // 'close' 이벤트는 창이 가려져 있으면 늦게 오거나 안 와서 버튼 클릭을 직접 받는다
  return new Promise((resolve) => {
    const finish = (ok) => {
      $("#confirm-ok").onclick = $("#confirm-cancel").onclick = dlg.oncancel = null;
      dlg.close();
      resolve(ok);
    };
    $("#confirm-ok").onclick = () => finish(true);
    $("#confirm-cancel").onclick = () => finish(false);
    dlg.oncancel = (e) => { e.preventDefault(); finish(false); };  // Esc
  });
}

function fmtDuration(sec) {
  if (sec == null) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}시간 ${m}분` : m ? `${m}분 ${s}초` : `${s}초`;
}

function fmtClock(sec) {
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const days = ["일", "월", "화", "수", "목", "금", "토"];
  const time = d.toLocaleTimeString("ko-KR", { hour: "numeric", minute: "2-digit" });
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return `${sameYear ? "" : d.getFullYear() + "년 "}${d.getMonth() + 1}월 ${d.getDate()}일 (${days[d.getDay()]}) ${time}`;
}

function fmtBytes(n) {
  if (n > 1e9) return (n / 1e9).toFixed(1) + "GB";
  if (n > 1e6) return (n / 1e6).toFixed(0) + "MB";
  return Math.max(1, Math.round(n / 1e3)) + "KB";
}

const LANG_LABEL = { null: "한·영 자동", ko: "한국어", en: "영어" };
const isWorking = (r) => r.status === "queued" || r.status === "processing";

/* ---------- 라우팅 ---------- */

function hashParams() {
  const i = location.hash.indexOf("?");
  return new URLSearchParams(i >= 0 ? location.hash.slice(i + 1) : "");
}

function route() {
  const m = location.hash.match(/^#\/r\/([\w-]+)/);
  const searchInput = $("#search-input");
  if (location.hash.startsWith("#/search")) {
    const q = hashParams().get("q") || "";
    if (document.activeElement !== searchInput) searchInput.value = q;
    renderSearch(q);
  } else {
    if (document.activeElement !== searchInput) searchInput.value = "";
    if (m) renderDetail(m[1], hashParams());
    else renderHome();
  }
}
window.addEventListener("hashchange", route);

/* ---------- 홈: 업로드 + 목록 ---------- */

function renderHome() {
  view.innerHTML = `
    <section id="upload-area"></section>
    <section>
      <div class="section-title"><h2>내 녹음</h2><span class="count" id="rec-count"></span></div>
      <div class="rec-list" id="rec-list"></div>
    </section>`;
  renderUploadArea();
  renderList();
  refresh();
}

function optionsFieldsHTML(defaultTitle) {
  const subjectOptions = state.subjects.map((s) => `<option value="${esc(s.name)}">`).join("");
  return `
      <label class="field">
        <span class="field-label">제목</span>
        <input class="input" name="title" value="${esc(defaultTitle)}" maxlength="120">
      </label>

      <div class="field">
        <span class="field-label">강의 언어</span>
        <div class="segmented" role="radiogroup" aria-label="강의 언어">
          <label><input type="radio" name="language" value="auto" checked><span>한·영 섞임 (자동)</span></label>
          <label><input type="radio" name="language" value="ko"><span>한국어만</span></label>
          <label><input type="radio" name="language" value="en"><span>영어만</span></label>
        </div>
        <span class="field-hint">잘 모르겠으면 자동으로 두세요. 한 언어만 나오면 그 언어를 고르는 게 조금 더 정확해요.</span>
      </div>

      <div class="field-row">
        <label class="field">
          <span class="field-label">말하는 사람 수</span>
          <select class="select" name="num_speakers">
            <option value="">모름 (자동)</option>
            ${[1, 2, 3, 4, 5, 6, 7, 8].map((n) => `<option value="${n}">${n}명</option>`).join("")}
          </select>
          <span class="field-hint">실제보다 적게 고르면 다른 사람 말이 합쳐져요.</span>
        </label>
        <label class="field">
          <span class="field-label">과목 <span class="field-hint">(선택)</span></span>
          <input class="input" name="subject" list="subject-list" placeholder="예: 자료구조" autocomplete="off">
          <datalist id="subject-list">${subjectOptions}</datalist>
          <span class="field-hint">과목을 적으면 아래 용어가 저장돼서 다음에 자동으로 채워져요.</span>
        </label>
      </div>

      <details class="advanced" id="advanced">
        <summary>용어 힌트 · 용어 바꾸기 (선택)</summary>
        <div class="advanced-body">
          <label class="field">
            <span class="field-label">용어 힌트</span>
            <textarea class="textarea" name="hotwords" placeholder="hash table, collision, linked list"></textarea>
            <span class="field-hint">강의에 자주 나오는 전문 용어를 쉼표나 줄바꿈으로 적어주세요. 받아쓸 때 참고해요.</span>
          </label>
          <label class="field">
            <span class="field-label">용어 바꾸기</span>
            <textarea class="textarea" name="replacements" placeholder="컬리전 = collision&#10;세프리 체인잉 = separate chaining"></textarea>
            <span class="field-hint">자꾸 잘못 적히는 단어를 한 줄에 하나씩 <b>틀린 말 = 바른 말</b>로 적으면 고쳐줘요.</span>
          </label>
        </div>
      </details>`;
}

const OPTION_FIELDS = ["title", "language", "num_speakers", "subject", "hotwords", "replacements"];

function appendOptions(fd, form) {
  for (const name of OPTION_FIELDS) fd.append(name, form.elements[name].value);
}

function renderUploadArea() {
  const area = $("#upload-area");
  if (!area) return;
  if (recorder.phase === "recording" || recorder.phase === "stopping") return renderRecordingPanel(area);
  if (recorder.phase === "finished") return renderRecordedForm(area);

  if (!state.pendingFile) {
    area.innerHTML = `
      <div class="dropzone" id="dropzone" role="button" tabindex="0">
        <div class="dropzone-icon">🎧</div>
        <div class="dropzone-title">녹음 파일을 끌어다 놓거나 눌러서 고르세요</div>
        <div class="dropzone-sub">m4a · mp3 · wav · 영상 파일도 돼요. 2시간 강의도 괜찮아요. 바로 녹음하려면 위의 <b>● 녹음하기</b>를 누르세요.</div>
      </div>`;
    const dz = $("#dropzone");
    dz.addEventListener("click", pickFile);
    dz.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && pickFile());
    return;
  }

  const f = state.pendingFile;
  area.innerHTML = `
    <form class="card upload-form" id="upload-form" novalidate>
      <div class="upload-file">
        <span class="upload-file-icon">🎵</span>
        <div>
          <div class="upload-file-name">${esc(f.name)}</div>
          <div class="upload-file-meta">${fmtBytes(f.size)}</div>
        </div>
        <button type="button" class="btn-link btn" id="change-file">다른 파일</button>
      </div>
      ${optionsFieldsHTML(f.name.replace(/\.[^.]+$/, ""))}
      <div class="form-actions">
        <div class="upload-bar" id="upload-bar" hidden><div></div></div>
        <button type="button" class="btn btn-ghost" id="cancel-upload">취소</button>
        <button type="submit" class="btn btn-primary btn-lg" id="submit-upload">받아쓰기 시작</button>
      </div>
    </form>`;

  const form = $("#upload-form");
  $("#change-file").addEventListener("click", pickFile);
  $("#cancel-upload").addEventListener("click", () => { state.pendingFile = null; renderUploadArea(); });
  form.subject.addEventListener("change", () => fillSubjectTerms(form));
  form.addEventListener("submit", (e) => { e.preventDefault(); submitUpload(form); });
  form.title.focus();
  form.title.select();
}

function fillSubjectTerms(form) {
  const s = state.subjects.find((x) => x.name === form.subject.value.trim());
  if (!s) return;
  form.hotwords.value = s.hotwords.join(", ");
  form.replacements.value = Object.entries(s.replacements).map(([a, b]) => `${a} = ${b}`).join("\n");
  if (s.hotwords.length || Object.keys(s.replacements).length) $("#advanced").open = true;
}

function pickFile() {
  $("#file-input").value = "";
  $("#file-input").click();
}

function acceptFile(file) {
  if (!file) return;
  const ok = /^(audio|video)\//.test(file.type) || /\.(m4a|mp3|wav|webm|mp4|aac|ogg|flac|mov|caf)$/i.test(file.name);
  if (!ok) { toast("소리 파일이 아닌 것 같아요. m4a, mp3, wav 같은 파일을 골라주세요.", "error"); return; }
  if (recorder.phase !== "idle") { toast("녹음을 먼저 끝내주세요.", "error"); return; }
  if (location.hash.startsWith("#/r/")) location.hash = "#/";
  state.pendingFile = file;
  renderUploadArea();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function submitUpload(form) {
  if (state.uploading) return;
  const fd = new FormData();
  fd.append("file", state.pendingFile);
  appendOptions(fd, form);

  state.uploading = true;
  const btn = $("#submit-upload");
  const bar = $("#upload-bar");
  btn.disabled = true;
  btn.textContent = "올리는 중…";
  bar.hidden = false;

  // fetch 는 업로드 진행률을 못 알려줘서 XHR 사용 (큰 파일은 몇 초 걸림)
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/recordings");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.firstElementChild.style.width = `${(e.loaded / e.total) * 100}%`; };
  xhr.onload = () => {
    state.uploading = false;
    if (xhr.status === 201) {
      state.pendingFile = null;
      renderUploadArea();
      toast("올렸어요. 받아쓰기가 끝나면 목록에서 열 수 있어요.");
      refresh();
      loadSubjects();
    } else {
      let msg = "올리지 못했어요. 다시 시도해 주세요.";
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
      toast(msg, "error");
      btn.disabled = false;
      btn.textContent = "받아쓰기 시작";
      bar.hidden = true;
    }
  };
  xhr.onerror = () => {
    state.uploading = false;
    toast("앱과 연결이 끊겼어요. 까치녹음기가 켜져 있는지 확인해 주세요.", "error");
    btn.disabled = false;
    btn.textContent = "받아쓰기 시작";
    bar.hidden = true;
  };
  xhr.send(fd);
}

function statusChip(r) {
  if (r.status === "recording") {
    return r.id === recorder.id || !r.stalled
      ? `<span class="chip chip-live"><span class="dot"></span>녹음 중</span>`
      : `<span class="chip chip-failed"><span class="dot"></span>끊김</span>`;
  }
  if (r.status === "done") return `<span class="chip chip-done"><span class="dot"></span>완료</span>`;
  if (r.status === "failed") return `<span class="chip chip-failed"><span class="dot"></span>실패</span>`;
  if (r.status === "queued") return `<span class="chip chip-queued"><span class="dot"></span>대기 중</span>`;
  return `<span class="chip chip-working"><span class="dot"></span>처리 중</span>`;
}

function recItemHTML(r) {
  const meta = [
    fmtDate(r.created_at),
    r.duration ? fmtDuration(r.duration) : "",
    LANG_LABEL[r.language],
    r.subject ? `📚 ${esc(r.subject)}` : "",
  ].filter(Boolean).map((x) => `<span>${x}</span>`).join("");

  const pct = Math.round((r.progress || 0) * 100);
  const progress = isWorking(r) ? `
    <div class="rec-progress">
      <div class="rec-progress-top"><span class="stage">${esc(r.stage_label)}${r.status === "processing" ? "…" : ""}</span><span class="pct">${r.status === "processing" ? pct + "%" : ""}</span></div>
      <div class="bar ${r.status === "queued" ? "indeterminate" : ""}"><div style="width:${pct}%"></div></div>
    </div>` : "";
  const interrupted = r.status === "recording" && r.stalled && r.id !== recorder.id ? `
    <div class="rec-error"><span>녹음이 중간에 끊겼어요. 저장된 부분까지 받아쓸 수 있어요.</span><button class="btn btn-sm btn-ghost" data-action="finish-interrupted">여기까지 받아쓰기</button></div>` : "";
  const error = r.status === "failed" ? `
    <div class="rec-error"><span>${esc(r.error)}</span><button class="btn btn-sm btn-ghost" data-action="retry">다시 시도</button></div>` : "";

  return `
    <article class="card rec ${r.status === "done" ? "clickable" : ""}" data-id="${r.id}" data-status="${r.status}">
      <div>
        <div class="rec-title">${esc(r.title)}</div>
        <div class="rec-meta">${meta}</div>
      </div>
      <div class="rec-side">
        ${statusChip(r)}
        <button class="icon-btn" data-action="delete" title="삭제" aria-label="삭제">✕</button>
      </div>
      ${progress}${error}${interrupted}
    </article>`;
}

function renderList() {
  const list = $("#rec-list");
  if (!list) return;
  $("#rec-count").textContent = state.recordings.length ? `${state.recordings.length}개` : "";

  if (!state.recordings.length) {
    list.innerHTML = `<div class="empty"><div class="empty-icon">🐦‍⬛</div>아직 녹음이 없어요.<br>위에서 파일을 올려보세요.</div>`;
    return;
  }

  // 진행률 막대가 부드럽게 움직이도록, 상태가 같은 항목은 숫자만 바꾼다
  const existing = new Map([...list.querySelectorAll(".rec")].map((el) => [el.dataset.id, el]));
  const frag = document.createDocumentFragment();
  for (const r of state.recordings) {
    let el = existing.get(r.id);
    const html = recItemHTML(r).trim();
    if (el && el.dataset.status === r.status) {
      if (r.status === "processing") {
        el.querySelector(".stage").textContent = `${r.stage_label}…`;
        el.querySelector(".pct").textContent = `${Math.round(r.progress * 100)}%`;
        el.querySelector(".bar > div").style.width = `${Math.round(r.progress * 100)}%`;
      }
      if (el.dataset.html !== html && r.status !== "processing") el = null;
    } else {
      el = null;
    }
    if (!el) {
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      el = tmp.firstElementChild;
      el.dataset.html = html;
    }
    // 처리 중에 길이(duration)가 새로 생기면 메타 줄만 바꾼다
    if (r.status === "processing") {
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      el.querySelector(".rec-meta").replaceWith(tmp.querySelector(".rec-meta"));
    }
    frag.append(el);
  }
  list.replaceChildren(frag);
}

$("#view").addEventListener("click", async (e) => {
  const item = e.target.closest(".rec");
  if (!item) return;
  const id = item.dataset.id;
  const action = e.target.closest("[data-action]")?.dataset.action;

  if (action === "delete") {
    const r = state.recordings.find((x) => x.id === id);
    if (id === recorder.id) { toast("지금 녹음 중인 항목은 녹음을 끝낸 뒤 지울 수 있어요.", "error"); return; }
    const ok = await confirmDialog(`'${r?.title ?? "이 녹음"}'을(를) 삭제할까요? 녹음 파일과 받아쓴 내용이 모두 지워져요.`);
    if (!ok) return;
    try { await api(`/api/recordings/${id}`, { method: "DELETE" }); toast("삭제했어요."); refresh(); }
    catch (err) { toast(err.message, "error"); }
  } else if (action === "finish-interrupted") {
    finishInterrupted(id);
  } else if (action === "retry") {
    try { await api(`/api/recordings/${id}/retry`, { method: "POST" }); refresh(); }
    catch (err) { toast(err.message, "error"); }
  } else if (item.dataset.status === "done") {
    location.hash = `#/r/${id}`;
  }
});

/* ---------- 결과 화면 ---------- */

const detail = {
  id: null,
  data: null,
  activeUtt: null,
  activeWord: null,
  follow: true,
  editing: null,     // 고치는 중인 문단 id
  pollTimer: null,
};

const spkColor = (i) => `var(--spk-${((i % 8) + 8) % 8})`;
const speakerName = (idx) => detail.data?.speakers.find((s) => s.idx === idx)?.name ?? `화자 ${idx + 1}`;
const langBadge = (l) => (l === "mixed" ? "KO·EN" : String(l || "").toUpperCase());

async function renderDetail(id, params = new URLSearchParams()) {
  clearTimeout(detail.pollTimer);
  Object.assign(detail, { id, data: null, activeUtt: null, activeWord: null, editing: null });
  view.innerHTML = `<a class="back" href="#/">← 내 녹음</a><div class="empty">불러오는 중…</div>`;
  try { detail.data = await api(`/api/recordings/${id}`); }
  catch (err) { view.innerHTML = `<a class="back" href="#/">← 내 녹음</a><div class="empty">${esc(err.message)}</div>`; return; }
  const d = detail.data;

  view.innerHTML = `
    <a class="back" href="#/">← 내 녹음</a>
    <div class="detail-head">
      <h1 class="editable-title" id="title" title="눌러서 제목 바꾸기">${esc(d.title)}</h1>
      <div class="detail-meta-row">
        <div class="rec-meta"><span>${fmtDate(d.created_at)}</span><span>${fmtDuration(d.duration)}</span><span>${LANG_LABEL[d.language]}</span>${d.subject ? `<span>📚 ${esc(d.subject)}</span>` : ""}</div>
        <div class="detail-actions">
          <button class="btn btn-sm btn-primary" id="ai-copy">AI 요약용 복사 ▾</button>
          <button class="btn btn-sm btn-ghost" id="copy-all">전체 복사</button>
          <button class="btn btn-sm btn-ghost" id="export-menu">내보내기 ▾</button>
          <button class="btn btn-sm btn-ghost" id="redo-all" title="처음부터 다시 받아쓰기">↻</button>
        </div>
      </div>
    </div>
    <div class="player-bar">
      <audio id="player" controls preload="metadata" src="/api/recordings/${d.id}/audio"></audio>
      <div class="player-tools">
        <select class="select select-sm" id="speed" aria-label="재생 속도">
          ${[0.75, 1, 1.25, 1.5, 2].map((v) => `<option value="${v}" ${v === 1 ? "selected" : ""}>${v}배속</option>`).join("")}
        </select>
        <label class="toggle"><input type="checkbox" id="follow" ${detail.follow ? "checked" : ""}><span>재생 따라가기</span></label>
      </div>
    </div>
    <div class="speakers" id="speakers"></div>
    <p class="hint-line">이름을 누르면 바꾸거나 다른 화자와 합칠 수 있어요 · 문장을 두 번 누르면 고칠 수 있어요 · 단어를 누르면 그 부분부터 들려줘요</p>
    <div class="transcript" id="transcript"></div>`;

  renderSpeakers();
  renderTranscript();
  bindDetail();

  // 검색 결과에서 들어온 경우: 그 문단으로 스크롤하고 재생 위치를 맞춘다 (자동 재생은 안 함)
  const uttId = Number(params.get("u"));
  if (uttId) {
    const el = view.querySelector(`.utt[data-id="${uttId}"]`);
    if (el) {
      el.classList.add("found");
      setTimeout(() => el.scrollIntoView({ block: "center" }), 50);
      const t = Number(params.get("t"));
      const p = $("#player");
      const setTime = () => { p.currentTime = Math.max(0, t - 0.05); };
      p.readyState >= 1 ? setTime() : p.addEventListener("loadedmetadata", setTime, { once: true });
    }
  }
}

function downloadExport(format) {
  const a = document.createElement("a");
  a.href = `/api/recordings/${detail.id}/export?format=${format}`;
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
}

function transcriptText() {
  // 서버의 텍스트 내보내기(app/export.py to_txt)와 같은 모양. 화면에 있는 최신 내용으로 바로 만든다
  const d = detail.data;
  const head = [d.title, [fmtDate(d.created_at), fmtDuration(d.duration)].filter(Boolean).join(" · "), ""];
  const body = d.utterances.map((u) => `[${fmtClock(u.start)}] ${speakerName(u.speaker)}\n${u.text}\n`);
  return head.concat(body).join("\n").trimEnd() + "\n";
}

const AI_PROMPTS = {
  meeting: `아래는 회의 녹음을 자동으로 받아쓴 전사문입니다. 음성 인식이라 오인식·잡음이 섞여 있을 수 있어요.
다음 원칙으로 한국어 회의록을 작성해 주세요.

[내용]
- 맥락상 확실한 내용 위주로 정리
- 문장 자체를 알아듣기 어려운 부분에만 "(전사 불명확)"을 붙이기
- 같은 말이 계속 반복되는 구간이나 앞뒤와 상관없는 짧은 영어 문장은 인식 오류일 수 있으니 무시

[숫자]
- 숫자는 전사문에 적힌 그대로 쓰기
- 같은 대상에 숫자가 여러 개 나오면 오인식으로 단정하지 말고, 누가 어떤 맥락에서 말했는지 함께 적기
  (예: "목표 수량: 100개(이번 달, 화자 2) / 300개(연말까지, 화자 1) — 결론 안 남")

[사람]
- 화자 이름은 전사문에 적힌 것만 쓰기. "화자 N"은 아직 누군지 모르는 사람이니 그대로 두고,
  대화 속에 나온 이름을 화자에 추측으로 붙이지 않기
- 대화 속에서 언급된 사람(예: "민지님께 보고")은 언급된 이름 그대로 쓰기.
  같은 사람으로 보이는데 이름이 다르게 적혔으면(예: 지훈/지운) 하나로 정하지 말고 함께 적기
- 할 일의 담당자는 대화에서 분명할 때만 적고, 아니면 "담당 미정"

형식 (노션에 붙여넣기 좋게 마크다운으로):
1. 한 줄 요약
2. 주요 논의 (주제별 소제목 + 글머리표, 의견이 갈렸으면 누가 어떤 의견이었는지)
3. 결정된 사항
4. 할 일 (표: 담당 · 내용 · 기한)
5. 미정·추가 확인이 필요한 사항`,
  lecture: `아래는 강의 녹음을 자동으로 받아쓴 전사문입니다. 음성 인식이라 오인식·잡음이 섞여 있을 수 있어요.
다음 원칙으로 한국어 강의 노트를 작성해 주세요.
- 영어 전문 용어는 원어를 함께 적기 (예: 충돌(collision))
- 숫자·공식·날짜는 전사문에 적힌 그대로 쓰기
- 문장 자체를 알아듣기 어려운 부분에만 "(전사 불명확)"을 붙이기
- 같은 말이 계속 반복되는 구간이나 앞뒤와 상관없는 짧은 문장은 인식 오류일 수 있으니 무시
- 화자 이름은 전사문에 적힌 것만 쓰고, 추측으로 이름을 붙이지 않기

형식 (노션에 붙여넣기 좋게 마크다운으로):
1. 강의 주제와 핵심 요약 (3~5줄)
2. 개념 정리 (소제목별로 정의·예시·공식)
3. 교수님이 강조했거나 시험에 나온다고 한 내용
4. 질문과 답변
5. 과제·공지 (마감일 포함)`,
};

function transcriptForAI(kind) {
  // 같은 화자가 이어서 말한 문단은 한 줄로 합쳐서 짧고 읽기 쉽게 만든다
  const d = detail.data;
  const lines = [];
  let cur = null;
  for (const u of d.utterances) {
    if (cur && cur.speaker === u.speaker) { cur.text += " " + u.text; continue; }
    if (cur) lines.push(cur);
    cur = { speaker: u.speaker, start: u.start, text: u.text };
  }
  if (cur) lines.push(cur);
  const names = d.speakers.map((s) => s.name).join(", ");
  const parts = [];
  if (AI_PROMPTS[kind]) parts.push(AI_PROMPTS[kind], "");
  parts.push(
    "[녹음 정보]",
    `제목: ${d.title}`,
    `날짜: ${fmtDate(d.created_at)} · 길이 ${fmtDuration(d.duration)}`,
    `화자: ${names}`,
    ...(d.speakers.some((sp) => /^화자 \d+$/.test(sp.name))
      ? ["(이름이 붙은 화자는 사용자가 직접 확인한 사람이고, '화자 N'은 아직 누군지 모르는 사람이에요)"]
      : []),
    "",
    "[전사문]",
    ...lines.map((l) => `${speakerName(l.speaker)} (${fmtClock(l.start)}): ${l.text}`),
  );
  return parts.join("\n") + "\n";
}

function copyText(text, okMessage) {
  const legacyCopy = () => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch {}
    ta.remove();
    return ok;
  };
  const done = (ok) => ok ? toast(okMessage) : toast("복사하지 못했어요. '내보내기 › 텍스트'로 파일을 받아주세요.", "error");
  if (legacyCopy()) return done(true);
  if (!navigator.clipboard?.writeText) return done(false);
  navigator.clipboard.writeText(text).then(() => done(true), () => done(false));
}

function aiMenu(anchor) {
  const unnamed = detail.data.speakers.some((s) => /^화자 \d+$/.test(s.name));
  const tip = unnamed ? " (화자 이름을 먼저 바꾸면 요약이 더 정확해요)" : "";
  openMenu(anchor, [
    { label: "회의록으로 요약 요청", icon: "🗂", run: () => copyText(transcriptForAI("meeting"), "복사했어요. Claude 에 붙여넣으면 회의록으로 정리해줘요." + tip) },
    { label: "강의 노트로 요약 요청", icon: "📚", run: () => copyText(transcriptForAI("lecture"), "복사했어요. Claude 에 붙여넣으면 강의 노트로 정리해줘요." + tip) },
    "-",
    { label: "전사문만 (요청 문구 없이)", icon: "📄", run: () => copyText(transcriptForAI(""), "전사문을 복사했어요.") },
  ]);
}

async function redoAll() {
  const ok = await confirmDialog(
    "이 녹음을 처음부터 다시 받아쓸까요? 앱이 업데이트됐을 때 더 나은 결과가 나올 수 있어요. 바꾼 화자 이름은 남지만, 직접 고친 글자와 합친 화자는 사라져요.",
    "다시 받아쓰기", { primary: true });
  if (!ok) return;
  try {
    await api(`/api/recordings/${detail.id}/retry`, { method: "POST" });
    toast("다시 받아쓰기를 시작했어요. 목록에서 진행 상황을 볼 수 있어요.");
    location.hash = "#/";
  } catch (err) { toast(err.message, "error"); }
}

function copyAll() {
  copyText(transcriptText(), "전체 내용을 복사했어요. 원하는 곳에 붙여넣으세요.");
}

function renderSpeakers() {
  const el = $("#speakers");
  if (!el) return;
  const counts = {};
  for (const u of detail.data.utterances) counts[u.speaker] = (counts[u.speaker] || 0) + (u.end - u.start);
  el.innerHTML = detail.data.speakers.map((s) => `
    <button class="speaker-tag" data-rename="${s.idx}" title="이름 바꾸기 · 합치기">
      <i style="background:${spkColor(s.idx)}"></i>${esc(s.name)}
      <span class="speaker-time">${counts[s.idx] ? fmtDuration(counts[s.idx]) : "0초"}</span>
    </button>`).join("");
}

function wordsHTML(u) {
  if (!u.words.length) return esc(u.text);
  return u.words.map((w, i) => `<span class="w" data-i="${i}" data-t="${w[1]}">${esc(w[0])}</span>`).join(" ");
}

function uttHTML(u) {
  const editing = detail.editing === u.id;
  return `
    <div class="utt ${u.busy ? "busy" : ""} ${detail.activeUtt === u.id ? "active" : ""}" data-id="${u.id}" data-start="${u.start}" data-end="${u.end}">
      <div class="utt-bar" style="background:${spkColor(u.speaker)}"></div>
      <div class="utt-body">
        <div class="utt-head">
          <button class="utt-speaker" data-menu="speaker" style="color:${spkColor(u.speaker)}" title="다른 화자로 바꾸기">${esc(speakerName(u.speaker))} ▾</button>
          <button class="utt-time" data-seek="${u.start}">${fmtClock(u.start)}</button>
          <span class="utt-lang">${langBadge(u.language)}</span>
          <button class="utt-more" data-menu="more" aria-label="문단 메뉴" title="문단 메뉴">⋯</button>
        </div>
        ${editing ? `
          <div class="utt-edit">
            <textarea class="textarea" id="edit-text">${esc(u.text)}</textarea>
            <div class="utt-edit-actions">
              <span class="field-hint">⌘+Enter 저장 · Esc 취소</span>
              <button class="btn btn-sm btn-ghost" data-act="cancel-edit">취소</button>
              <button class="btn btn-sm btn-primary" data-act="save-edit">저장</button>
            </div>
          </div>` : `
          <div class="utt-text">${u.busy ? `<span class="busy-label">다시 받아쓰는 중…</span>` : wordsHTML(u)}</div>`}
      </div>
    </div>`;
}

function renderTranscript() {
  const el = $("#transcript");
  if (!el) return;
  const utts = detail.data.utterances;
  el.innerHTML = utts.length
    ? utts.map(uttHTML).join("")
    : `<div class="empty">받아쓴 내용이 없어요. 말소리가 없는 녹음일 수 있어요.</div>`;
  if (detail.editing) {
    const ta = $("#edit-text");
    if (ta) { autosize(ta); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  }
  scheduleBusyPoll();
}

function replaceUtt(u) {
  const i = detail.data.utterances.findIndex((x) => x.id === u.id);
  if (i >= 0) detail.data.utterances[i] = u;
  const el = view.querySelector(`.utt[data-id="${u.id}"]`);
  if (el) {
    const tmp = document.createElement("div");
    tmp.innerHTML = uttHTML(u).trim();
    el.replaceWith(tmp.firstElementChild);
  }
  renderSpeakers();
}

function autosize(ta) {
  ta.style.height = "auto";
  ta.style.height = `${ta.scrollHeight + 2}px`;
}

function scheduleBusyPoll() {
  clearTimeout(detail.pollTimer);
  if (!detail.data?.utterances.some((u) => u.busy)) return;
  detail.pollTimer = setTimeout(async () => {
    if (!location.hash.includes(detail.id)) return;
    try {
      const fresh = await api(`/api/recordings/${detail.id}`);
      for (const u of fresh.utterances) {
        const old = detail.data.utterances.find((x) => x.id === u.id);
        if (old && old.busy && !u.busy) {
          replaceUtt(u);
          toast(old.text === u.text ? "다시 받아썼지만 내용이 같아요." : "다시 받아썼어요.");
        }
      }
    } catch {}
    scheduleBusyPoll();
  }, 1500);
}

/* 팝업 메뉴 (화자 바꾸기 / 문단 메뉴) */
function openMenu(anchor, items) {
  closeMenu();
  const menu = document.createElement("div");
  menu.className = "menu";
  menu.id = "menu";
  menu.innerHTML = items.map((it, i) => it === "-"
    ? `<div class="menu-sep"></div>`
    : `<button class="menu-item ${it.danger ? "danger" : ""}" data-i="${i}" ${it.disabled ? "disabled" : ""}>${it.icon ? `<span class="menu-icon">${it.icon}</span>` : ""}${esc(it.label)}</button>`).join("");
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const left = Math.min(r.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - menu.offsetWidth - 12);
  menu.style.left = `${Math.max(12, left)}px`;
  menu.style.top = `${r.bottom + window.scrollY + 4}px`;
  menu.addEventListener("click", (e) => {
    const b = e.target.closest(".menu-item");
    if (!b) return;
    closeMenu();
    items[Number(b.dataset.i)].run();
  });
  setTimeout(() => document.addEventListener("click", closeMenuOutside), 0);
}
function closeMenuOutside(e) { if (!e.target.closest("#menu")) closeMenu(); }
function closeMenu() {
  $("#menu")?.remove();
  document.removeEventListener("click", closeMenuOutside);
}

async function patchUtt(id, body) {
  try { replaceUtt(await api(`/api/utterances/${id}`, jsonOpts("PATCH", body))); return true; }
  catch (err) { toast(err.message, "error"); return false; }
}

function jsonOpts(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function speakerMenu(anchor, u) {
  const items = detail.data.speakers.map((s) => ({
    label: s.name + (s.idx === u.speaker ? "  ✓" : ""),
    icon: `<i class="menu-dot" style="background:${spkColor(s.idx)}"></i>`,
    disabled: s.idx === u.speaker,
    run: () => patchUtt(u.id, { speaker: s.idx }),
  }));
  items.push("-", {
    label: "새 화자로 나누기", icon: "＋",
    run: async () => {
      try {
        const s = await api(`/api/recordings/${detail.id}/speakers`, { method: "POST" });
        detail.data.speakers.push(s);
        await patchUtt(u.id, { speaker: s.idx });
      } catch (err) { toast(err.message, "error"); }
    },
  });
  openMenu(anchor, items);
}

function moreMenu(anchor, u) {
  openMenu(anchor, [
    { label: "글자 고치기", icon: "✎", disabled: u.busy, run: () => startEdit(u.id) },
    { label: "여기부터 듣기", icon: "▶", run: () => seek(u.start) },
    "-",
    { label: "한국어로 다시 받아쓰기", icon: "가", disabled: u.busy, run: () => retranscribe(u, "ko") },
    { label: "영어로 다시 받아쓰기", icon: "A", disabled: u.busy, run: () => retranscribe(u, "en") },
    "-",
    { label: "문단 삭제", icon: "✕", danger: true, run: () => deleteUtt(u) },
  ]);
}

async function retranscribe(u, language) {
  try {
    replaceUtt(await api(`/api/utterances/${u.id}/retranscribe`, jsonOpts("POST", { language })));
    scheduleBusyPoll();
  } catch (err) { toast(err.message, "error"); }
}

async function deleteUtt(u) {
  if (!(await confirmDialog("이 문단을 지울까요? 녹음 파일은 그대로 남아요."))) return;
  try {
    await api(`/api/utterances/${u.id}`, { method: "DELETE" });
    detail.data.utterances = detail.data.utterances.filter((x) => x.id !== u.id);
    view.querySelector(`.utt[data-id="${u.id}"]`)?.remove();
    renderSpeakers();
  } catch (err) { toast(err.message, "error"); }
}

function startEdit(id) {
  if (detail.editing && detail.editing !== id) cancelEdit();
  detail.editing = id;
  replaceUtt(detail.data.utterances.find((x) => x.id === id));
  const ta = $("#edit-text");
  if (ta) { autosize(ta); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
}

function cancelEdit() {
  const id = detail.editing;
  detail.editing = null;
  if (id) replaceUtt(detail.data.utterances.find((x) => x.id === id));
}

async function saveEdit() {
  const id = detail.editing;
  const ta = $("#edit-text");
  if (!id || !ta) return;
  const text = ta.value;
  detail.editing = null;
  if (!(await patchUtt(id, { text }))) { detail.editing = id; replaceUtt(detail.data.utterances.find((x) => x.id === id)); }
}

function speakerTagMenu(tag, idx) {
  const others = detail.data.speakers.filter((s) => s.idx !== idx);
  const items = [{
    label: "이름 바꾸기", icon: "✎",
    run: () => inlineRename(tag, speakerName(idx), async (v) => {
      const r = await api(`/api/recordings/${detail.id}/speakers/${idx}`, jsonOpts("PATCH", { name: v }));
      detail.data.speakers.find((s) => s.idx === idx).name = r.name;
    }),
  }];
  if (others.length) {
    items.push("-");
    for (const o of others) {
      items.push({
        label: `${o.name}와(과) 같은 사람이에요`,
        icon: `<i class="menu-dot" style="background:${spkColor(o.idx)}"></i>`,
        run: () => mergeSpeaker(idx, o.idx),
      });
    }
  }
  openMenu(tag, items);
}

async function mergeSpeaker(src, dst) {
  const ok = await confirmDialog(
    `'${speakerName(src)}'의 말을 모두 '${speakerName(dst)}'(으)로 합칠까요? 이어지는 문단도 하나로 묶여요.`,
    "합치기", { primary: true });
  if (!ok) return;
  try {
    const t = await api(`/api/recordings/${detail.id}/speakers/${src}/merge`, jsonOpts("POST", { into: dst }));
    detail.data.speakers = t.speakers;
    detail.data.utterances = t.utterances;
    renderSpeakers();
    renderTranscript();
    toast("합쳤어요.");
  } catch (err) { toast(err.message, "error"); }
}

function inlineRename(el, current, save) {
  const input = document.createElement("input");
  input.className = "input inline-input";
  input.value = current;
  input.maxLength = el.id === "title" ? 120 : 40;
  el.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (commit && value && value !== current) {
      try { await save(value); } catch (err) { toast(err.message, "error"); }
    }
    renderDetailHeaderAndSpeakers(input);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

function renderDetailHeaderAndSpeakers(input) {
  if (input.parentElement?.classList.contains("detail-head")) {
    const h = document.createElement("h1");
    h.className = "editable-title";
    h.id = "title";
    h.title = "눌러서 제목 바꾸기";
    h.textContent = detail.data.title;
    input.replaceWith(h);
  } else {
    input.remove();
    renderSpeakers();
  }
  // 화자 이름이 바뀌었을 수 있으니 문단 머리도 다시 그린다
  renderTranscript();
}

function seek(t) {
  const p = $("#player");
  if (!p) return;
  p.currentTime = Math.max(0, t - 0.05);
  p.play().catch(() => {});
}

function onTimeUpdate() {
  const p = $("#player");
  const t = p.currentTime;
  const utts = detail.data.utterances;
  // 현재 시간이 들어있는 문단 (없으면 직전 문단)
  let cur = null;
  for (const u of utts) {
    if (u.start <= t + 0.05) cur = u;
    else break;
  }
  if (cur && t > cur.end + 1.5) cur = null;

  if ((cur?.id ?? null) !== detail.activeUtt) {
    view.querySelector(".utt.active")?.classList.remove("active");
    detail.activeUtt = cur?.id ?? null;
    if (cur) {
      const el = view.querySelector(`.utt[data-id="${cur.id}"]`);
      el?.classList.add("active");
      if (detail.follow && !p.paused && el && !detail.editing) el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  // 단어 강조
  view.querySelector(".w.on")?.classList.remove("on");
  if (cur && cur.words.length) {
    let wi = -1;
    for (let i = 0; i < cur.words.length; i++) {
      if (cur.words[i][1] <= t) wi = i; else break;
    }
    if (wi >= 0) view.querySelector(`.utt[data-id="${cur.id}"] .w[data-i="${wi}"]`)?.classList.add("on");
  }
}

function bindDetail() {
  const p = $("#player");
  p.addEventListener("timeupdate", onTimeUpdate);
  $("#speed").addEventListener("change", (e) => { p.playbackRate = Number(e.target.value); });
  $("#follow").addEventListener("change", (e) => { detail.follow = e.target.checked; });
}

// 화면(view)은 계속 재사용되므로 아래 이벤트는 한 번만 건다
view.addEventListener("click", detailClick);
view.addEventListener("dblclick", (e) => {
    if (!location.hash.startsWith("#/r/") || !detail.data) return;
    const t = e.target.closest(".utt-text");
    if (!t) return;
    const u = detail.data.utterances.find((x) => x.id === Number(t.closest(".utt").dataset.id));
    if (u && !u.busy) startEdit(u.id);
});
view.addEventListener("keydown", (e) => {
  if (e.target.id !== "edit-text") return;
  if (e.key === "Escape") cancelEdit();
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) saveEdit();
});
view.addEventListener("input", (e) => { if (e.target.id === "edit-text") autosize(e.target); });

function detailClick(e) {
  if (!detail.data || !location.hash.startsWith("#/r/")) return;
  if (e.target.closest("#copy-all")) return copyAll();
  if (e.target.closest("#redo-all")) return redoAll();
  const ai = e.target.closest("#ai-copy");
  if (ai) { e.stopPropagation(); return aiMenu(ai); }
  const ex = e.target.closest("#export-menu");
  if (ex) {
    e.stopPropagation();
    return openMenu(ex, [
      { label: "텍스트 (.txt)", icon: "📄", run: () => downloadExport("txt") },
      { label: "마크다운 (.md) · 노트 앱용", icon: "📝", run: () => downloadExport("md") },
      { label: "자막 (.srt) · 영상 플레이어용", icon: "💬", run: () => downloadExport("srt") },
    ]);
  }
  const title = e.target.closest("#title");
  if (title) {
    return inlineRename(title, detail.data.title, async (v) => {
      const r = await api(`/api/recordings/${detail.id}`, jsonOpts("PATCH", { title: v }));
      detail.data.title = r.title;
    });
  }
  const rn = e.target.closest("[data-rename]");
  if (rn) {
    e.stopPropagation();
    return speakerTagMenu(rn, Number(rn.dataset.rename));
  }
  const uttEl = e.target.closest(".utt");
  if (!uttEl) return;
  const u = detail.data.utterances.find((x) => x.id === Number(uttEl.dataset.id));
  const menu = e.target.closest("[data-menu]");
  if (menu) { e.stopPropagation(); return menu.dataset.menu === "speaker" ? speakerMenu(menu, u) : moreMenu(menu, u); }
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "save-edit") return saveEdit();
  if (act === "cancel-edit") return cancelEdit();
  const sk = e.target.closest("[data-seek]");
  if (sk) return seek(Number(sk.dataset.seek));
  const w = e.target.closest(".w");
  if (w && !window.getSelection().toString()) return seek(Number(w.dataset.t));
}

// 결과 화면에서 스페이스바로 재생/멈춤 (글자 입력 중엔 제외)
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || !location.hash.startsWith("#/r/")) return;
  if (e.target.closest("input, textarea, select, button, [contenteditable]")) return;
  const p = $("#player");
  if (!p) return;
  e.preventDefault();
  p.paused ? p.play() : p.pause();
});

/* ---------- 검색 ---------- */

let searchSeq = 0;

function markAll(text, q) {
  // 검색어를 대소문자 무시하고 <mark> 로 감싼다 (HTML 은 먼저 이스케이프)
  if (!q) return esc(text);
  const lower = text.toLowerCase(), ql = q.toLowerCase();
  let out = "", i = 0;
  for (;;) {
    const j = lower.indexOf(ql, i);
    if (j < 0) break;
    out += esc(text.slice(i, j)) + `<mark>${esc(text.slice(j, j + q.length))}</mark>`;
    i = j + q.length;
  }
  return out + esc(text.slice(i));
}

function snippet(text, q, radius = 70) {
  const j = text.toLowerCase().indexOf(q.toLowerCase());
  if (j < 0 || text.length <= radius * 2 + q.length) return text;
  const a = Math.max(0, j - radius), b = Math.min(text.length, j + q.length + radius);
  return (a > 0 ? "… " : "") + text.slice(a, b).trim() + (b < text.length ? " …" : "");
}

async function renderSearch(q) {
  const seq = ++searchSeq;
  if (!q.trim()) {
    view.innerHTML = `<a class="back" href="#/">← 내 녹음</a><div class="empty"><div class="empty-icon">⌕</div>찾고 싶은 단어를 위 검색창에 입력하세요.</div>`;
    return;
  }
  if (!view.querySelector(".search-results")) view.innerHTML = `<a class="back" href="#/">← 내 녹음</a><div class="empty">찾는 중…</div>`;
  let r;
  try { r = await api(`/api/search?q=${encodeURIComponent(q)}`); }
  catch (err) { if (seq === searchSeq) view.innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }
  if (seq !== searchSeq) return;  // 더 최근 검색이 있으면 버린다

  const groups = new Map();
  for (const h of r.results) {
    if (!groups.has(h.recording_id)) groups.set(h.recording_id, { title: h.title, created_at: h.created_at, hits: [] });
    groups.get(h.recording_id).hits.push(h);
  }
  view.innerHTML = `
    <a class="back" href="#/">← 내 녹음</a>
    <div class="search-results">
      <h2 class="search-title">‘${esc(r.query)}’ ${r.count ? `${r.count}건${r.count >= 200 ? " 이상" : ""}` : "결과 없음"}</h2>
      ${r.count ? "" : `<div class="empty">찾는 말이 들어간 문장이 없어요. 띄어쓰기나 철자를 바꿔보세요.</div>`}
      ${[...groups.entries()].map(([rid, g]) => `
        <section class="card search-group">
          <div class="search-group-head">
            <a class="rec-title" href="#/r/${rid}">${esc(g.title)}</a>
            <span class="rec-meta"><span>${fmtDate(g.created_at)}</span><span>${g.hits.length}건</span></span>
          </div>
          ${g.hits.map((h) => `
            <a class="hit" href="#/r/${rid}?u=${h.id}&t=${h.start}">
              <span class="hit-time">${fmtClock(h.start)}</span>
              <span class="hit-body"><b class="hit-speaker" style="color:${spkColor(h.speaker)}">${esc(h.speaker_name)}</b> ${markAll(snippet(h.text, r.query), r.query)}</span>
            </a>`).join("")}
        </section>`).join("")}
    </div>`;
}

let searchTimer = null;
$("#search-input").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => goSearch(e.target.value), 250);
});
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  clearTimeout(searchTimer);
  goSearch($("#search-input").value);
});
$("#search-input").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.target.value = ""; e.target.blur(); if (location.hash.startsWith("#/search")) location.hash = "#/"; }
});

function goSearch(q) {
  const target = `#/search?q=${encodeURIComponent(q.trim())}`;
  if (!q.trim() && !location.hash.startsWith("#/search")) return;
  // 글자를 칠 때마다 방문 기록이 쌓이지 않게, 검색 화면 안에서는 주소만 바꾼다
  if (location.hash.startsWith("#/search")) { history.replaceState(null, "", target); route(); }
  else location.hash = target;
}

// ⌘K 또는 / 로 검색창
document.addEventListener("keydown", (e) => {
  const typing = e.target.closest("input, textarea, select, [contenteditable]");
  if ((e.key === "k" && (e.metaKey || e.ctrlKey)) || (e.key === "/" && !typing)) {
    e.preventDefault();
    $("#search-input").focus();
    $("#search-input").select();
  }
});

/* ---------- 브라우저 녹음 ---------- */

const CHUNK_MS = 10_000;  // 10초마다 서버에 저장 → 브라우저가 꺼져도 그때까지는 남는다

const recorder = {
  phase: "idle",          // idle → recording → stopping → finished → idle
  id: null,
  title: "",
  media: null,
  stream: null,
  audioCtx: null,
  analyser: null,
  seq: 0,
  queue: [],
  sending: false,
  offline: false,
  elapsedMs: 0,
  resumedAt: 0,
  paused: false,
  tick: null,
  raf: null,
};

function pickMime() {
  const candidates = [
    ["audio/webm;codecs=opus", "webm"],
    ["audio/webm", "webm"],
    ["audio/mp4", "mp4"],
    ["audio/ogg;codecs=opus", "ogg"],
  ];
  for (const [mime, container] of candidates) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(mime)) return { mime, container };
  }
  return null;
}

function defaultRecordTitle() {
  const d = new Date();
  const t = d.toLocaleTimeString("ko-KR", { hour: "numeric", minute: "2-digit" });
  return `녹음 ${d.getMonth() + 1}월 ${d.getDate()}일 ${t}`;
}

function elapsed() {
  return recorder.elapsedMs + (recorder.paused || !recorder.resumedAt ? 0 : Date.now() - recorder.resumedAt);
}

async function startRecording() {
  if (recorder.phase !== "idle") return;
  if (state.pendingFile) { toast("올리던 파일을 먼저 처리하거나 취소해 주세요.", "error"); return; }
  const fmt = pickMime();
  if (!navigator.mediaDevices?.getUserMedia || !fmt) {
    toast("이 브라우저에서는 녹음을 할 수 없어요. 크롬이나 사파리에서 열어주세요.", "error");
    return;
  }

  let stream;
  try {
    // 강의실 먼 소리도 살리도록 잡음 제거·에코 제거는 끄고, 소리 크기 자동 조절만 켠다
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true },
    });
  } catch (err) {
    const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
    toast(denied
      ? "마이크를 쓸 수 없어요. 브라우저 주소창의 마이크 권한을 허용하고, 시스템 설정 › 개인정보 보호 및 보안 › 마이크에서 브라우저를 켜주세요."
      : "마이크를 찾을 수 없어요. 마이크가 연결돼 있는지 확인해 주세요.", "error");
    return;
  }

  const title = defaultRecordTitle();
  let rec;
  try {
    const fd = new FormData();
    fd.append("title", title);
    fd.append("container", fmt.container);
    rec = await api("/api/live", { method: "POST", body: fd });
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    toast(err.message, "error");
    return;
  }

  Object.assign(recorder, {
    phase: "recording", id: rec.id, title, stream, seq: 0, queue: [], offline: false,
    elapsedMs: 0, resumedAt: Date.now(), paused: false,
  });

  const media = new MediaRecorder(stream, { mimeType: fmt.mime, audioBitsPerSecond: 64000 });
  media.ondataavailable = (e) => { if (e.data && e.data.size) enqueueChunk(e.data); };
  media.onerror = () => toast("녹음 중에 문제가 생겼어요. 지금까지 녹음된 부분은 저장돼 있어요.", "error");
  media.start(CHUNK_MS);
  recorder.media = media;

  setupLevelMeter(stream);
  window.addEventListener("beforeunload", warnBeforeUnload);
  if (location.hash.startsWith("#/r/")) location.hash = "#/";
  else renderUploadArea();
  recorder.tick = setInterval(updateRecordingPanel, 250);
  refresh();
}

function warnBeforeUnload(e) {
  e.preventDefault();
  e.returnValue = "";  // 브라우저 기본 경고창: "사이트에서 나가시겠습니까?"
}

function setupLevelMeter(stream) {
  try {
    recorder.audioCtx = new AudioContext();
    const src = recorder.audioCtx.createMediaStreamSource(stream);
    recorder.analyser = recorder.audioCtx.createAnalyser();
    recorder.analyser.fftSize = 1024;
    src.connect(recorder.analyser);
  } catch {
    recorder.analyser = null;
  }
  const buf = new Float32Array(1024);
  const draw = () => {
    const meter = $("#level-meter > div");
    if (meter && recorder.analyser) {
      recorder.analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      const db = 20 * Math.log10(Math.sqrt(sum / buf.length) + 1e-8);
      const level = recorder.paused ? 0 : Math.max(0, Math.min(1, (db + 60) / 50));
      meter.style.width = `${level * 100}%`;
    }
    if (recorder.phase === "recording") recorder.raf = requestAnimationFrame(draw);
  };
  draw();
}

function enqueueChunk(blob) {
  recorder.queue.push({ seq: recorder.seq++, blob });
  pumpChunks();
}

async function pumpChunks() {
  if (recorder.sending) return;
  recorder.sending = true;
  const id = recorder.id;
  while (recorder.queue.length && recorder.id === id) {
    const { seq, blob } = recorder.queue[0];
    try {
      const res = await fetch(`/api/live/${id}/chunks/${seq}`, { method: "PUT", body: blob });
      if (res.status === 409 || res.status === 404) { recorder.queue = []; break; }  // 이미 끝났거나 삭제됨
      if (!res.ok) throw new Error(String(res.status));
      recorder.queue.shift();
      recorder.offline = false;
    } catch {
      // 앱이 잠깐 바쁘거나 꺼졌다 켜진 경우: 조각은 메모리에 들고 있다가 다시 보낸다
      recorder.offline = true;
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
  recorder.sending = false;
}

function togglePause() {
  const m = recorder.media;
  if (!m) return;
  if (recorder.paused) {
    m.resume();
    recorder.paused = false;
    recorder.resumedAt = Date.now();
  } else {
    m.pause();
    recorder.elapsedMs = elapsed();
    recorder.paused = true;
  }
  updateRecordingPanel();
}

async function stopRecording() {
  if (recorder.phase !== "recording") return;
  recorder.elapsedMs = elapsed();
  recorder.paused = true;
  recorder.phase = "stopping";
  renderUploadArea();

  await new Promise((resolve) => {
    recorder.media.addEventListener("stop", resolve, { once: true });
    recorder.media.stop();  // 마지막 조각이 ondataavailable 로 한 번 더 온다
  });
  cleanupMedia();
  // 남은 조각을 다 보낼 때까지 기다린다
  while (recorder.queue.length || recorder.sending) await new Promise((r) => setTimeout(r, 200));

  recorder.phase = "finished";
  renderUploadArea();
}

function cleanupMedia() {
  clearInterval(recorder.tick);
  cancelAnimationFrame(recorder.raf);
  recorder.stream?.getTracks().forEach((t) => t.stop());
  recorder.audioCtx?.close().catch(() => {});
  Object.assign(recorder, { stream: null, media: null, audioCtx: null, analyser: null });
}

function resetRecorder() {
  cleanupMedia();
  window.removeEventListener("beforeunload", warnBeforeUnload);
  Object.assign(recorder, { phase: "idle", id: null, queue: [], seq: 0, elapsedMs: 0, paused: false });
  renderUploadArea();
}

function renderRecordingPanel(area) {
  const stopping = recorder.phase === "stopping";
  area.innerHTML = `
    <section class="card rec-panel" aria-label="녹음 중">
      <div class="rec-panel-top">
        <span class="rec-live ${recorder.paused ? "paused" : ""}" id="rec-live">${stopping ? "저장 중" : recorder.paused ? "일시정지" : "녹음 중"}</span>
        <span class="rec-saved" id="rec-saved"></span>
      </div>
      <div class="rec-timer" id="rec-timer">${fmtClock(elapsed() / 1000)}</div>
      <div class="level" id="level-meter" aria-hidden="true"><div></div></div>
      <div class="rec-actions">
        <button class="btn btn-ghost btn-lg" id="btn-pause" ${stopping ? "disabled" : ""}>${recorder.paused ? "▶ 계속" : "❚❚ 일시정지"}</button>
        <button class="btn btn-danger btn-lg" id="btn-stop" ${stopping ? "disabled" : ""}>■ 녹음 끝내기</button>
      </div>
      <ul class="rec-tips">
        <li>맥북 뚜껑을 닫으면 녹음이 멈춰요. 긴 강의는 충전기를 연결해 두세요.</li>
        <li>이 창을 닫아도 그때까지 녹음된 부분은 저장돼요.</li>
      </ul>
    </section>`;
  $("#btn-pause").addEventListener("click", togglePause);
  $("#btn-stop").addEventListener("click", stopRecording);
  updateRecordingPanel();
}

function updateRecordingPanel() {
  const timer = $("#rec-timer");
  if (!timer) return;
  timer.textContent = fmtClock(elapsed() / 1000);
  const live = $("#rec-live");
  if (recorder.phase === "recording") {
    live.textContent = recorder.paused ? "일시정지" : "녹음 중";
    live.classList.toggle("paused", recorder.paused);
    $("#btn-pause").textContent = recorder.paused ? "▶ 계속" : "❚❚ 일시정지";
  }
  const saved = $("#rec-saved");
  saved.textContent = recorder.offline
    ? "⚠ 앱과 연결이 끊겨서 다시 시도 중이에요"
    : recorder.seq ? `${fmtClock((recorder.seq - recorder.queue.length) * CHUNK_MS / 1000)}까지 안전하게 저장됨` : "";
  saved.classList.toggle("warn", recorder.offline);
}

function renderRecordedForm(area) {
  area.innerHTML = `
    <form class="card upload-form" id="finish-form" novalidate>
      <div class="upload-file">
        <span class="upload-file-icon">🎙️</span>
        <div>
          <div class="upload-file-name">녹음 완료</div>
          <div class="upload-file-meta">${fmtDuration(recorder.elapsedMs / 1000)}</div>
        </div>
        <button type="button" class="btn-link btn" id="discard-rec">녹음 버리기</button>
      </div>
      ${optionsFieldsHTML(recorder.title)}
      <div class="form-actions">
        <button type="submit" class="btn btn-primary btn-lg" id="submit-finish">받아쓰기 시작</button>
      </div>
    </form>`;
  const form = $("#finish-form");
  form.subject.addEventListener("change", () => fillSubjectTerms(form));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#submit-finish");
    btn.disabled = true;
    btn.textContent = "저장하는 중…";
    const fd = new FormData();
    appendOptions(fd, form);
    try {
      await api(`/api/live/${recorder.id}/finish`, { method: "POST", body: fd });
      toast("녹음을 저장했어요. 받아쓰기가 끝나면 목록에서 열 수 있어요.");
      resetRecorder();
      refresh();
      loadSubjects();
    } catch (err) {
      toast(err.message, "error");
      btn.disabled = false;
      btn.textContent = "받아쓰기 시작";
    }
  });
  $("#discard-rec").addEventListener("click", async () => {
    if (!(await confirmDialog("방금 녹음한 내용을 버릴까요? 되돌릴 수 없어요.", "버리기"))) return;
    try { await api(`/api/recordings/${recorder.id}`, { method: "DELETE" }); } catch {}
    resetRecorder();
    refresh();
  });
  form.title.focus();
  form.title.select();
}

async function finishInterrupted(id) {
  // 브라우저가 꺼져서 끊긴 녹음: 저장된 조각까지 기본 설정(자동)으로 받아쓴다
  const fd = new FormData();
  fd.append("language", "auto");
  try {
    await api(`/api/live/${id}/finish`, { method: "POST", body: fd });
    toast("저장된 부분까지 받아쓰기를 시작했어요.");
    refresh();
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ---------- 앱 관리: 설정 메뉴 / 업데이트 / 종료 / 로그 / 첫 실행 안내 ---------- */

const appState = { closed: false, updating: false, failures: 0, version: null };

function showOverlay(icon, title, text) {
  $("#overlay-icon").textContent = icon;
  $("#overlay-title").textContent = title;
  $("#overlay-text").innerHTML = text;
  $("#overlay").hidden = false;
}

async function settingsMenu(anchor) {
  let info = null;
  try { info = await api("/api/app"); } catch {}
  const v = info?.version;
  const vLabel = v?.commit ? `버전 ${v.commit} · ${new Date(v.date).toLocaleDateString("ko-KR", { month: "long", day: "numeric" })}` : "버전 정보 없음";
  openMenu(anchor, [
    { label: "업데이트 확인", icon: "↻", run: checkUpdate },
    { label: "사용법 보기", icon: "?", run: () => showGuide(0) },
    { label: "문제 신고용 로그 저장", icon: "🧾", run: saveLogs },
    "-",
    { label: vLabel, icon: "ⓘ", disabled: true, run: () => {} },
    { label: "까치녹음기 끄기", icon: "⏻", danger: true, run: quitApp },
  ]);
}

async function checkUpdate() {
  toast("새 버전이 있는지 확인하는 중…");
  let r;
  try { r = await api("/api/app/update"); } catch (err) { toast(err.message, "error"); return; }
  if (r.available === null) { toast(r.message, "error"); return; }
  if (!r.available) { toast("지금이 최신 버전이에요."); return; }
  const list = r.changes.map((c) => `<li>${esc(c)}</li>`).join("");
  const ok = await confirmDialog(
    `<b>새 버전이 있어요.</b><ul class="change-list">${list}</ul><span class="field-hint">업데이트하면 앱이 잠깐 다시 시작돼요. 1~2분쯤 걸릴 수 있어요.</span>`,
    "지금 업데이트", { html: true, primary: true, cancelLabel: "나중에" });
  if (ok) applyUpdate();
}

async function applyUpdate() {
  if (recorder.phase !== "idle") { toast("녹음을 먼저 끝내주세요.", "error"); return; }
  appState.updating = true;
  showOverlay("↻", "업데이트하는 중", "1~2분쯤 걸릴 수 있어요.<br>이 창을 닫지 말고 기다려 주세요.");
  try {
    await api("/api/app/update", { method: "POST" });
  } catch (err) {
    appState.updating = false;
    $("#overlay").hidden = true;
    toast(err.message, "error");
    return;
  }
  // 서버가 새 코드로 다시 켜질 때까지 기다렸다가 화면을 새로 고친다
  await new Promise((r) => setTimeout(r, 2000));
  for (let i = 0; i < 180; i++) {
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      if (res.ok) { location.reload(); return; }
    } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  showOverlay("⚠", "다시 켜지지 않았어요", "Dock의 <b>까치녹음기</b>를 눌러 직접 켜주세요.");
}

async function saveLogs() {
  try {
    const r = await api("/api/app/logs", { method: "POST" });
    toast(`바탕화면에 '${r.name}' 파일을 만들었어요. 이 파일을 보내주세요.`);
  } catch (err) { toast(err.message, "error"); }
}

async function quitApp() {
  if (recorder.phase !== "idle") { toast("녹음을 먼저 끝내주세요.", "error"); return; }
  let busy = [];
  try { busy = (await api("/api/app")).busy; } catch {}
  const msg = busy.length
    ? `${busy[0]}. 지금 끄면 그 녹음은 나중에 [다시 시도]를 눌러야 해요. 그래도 끌까요?`
    : "까치녹음기를 끌까요? 다시 쓰려면 Dock의 까치녹음기를 누르면 돼요.";
  if (!(await confirmDialog(msg, "끄기"))) return;
  try { await api("/api/app/quit", { method: "POST" }); } catch {}
  appState.closed = true;
  clearTimeout(state.pollTimer);
  showOverlay("🐦‍⬛", "까치녹음기를 껐어요", "이 탭은 닫아도 돼요.<br>다시 쓰려면 Dock의 <b>까치녹음기</b>를 누르세요.");
}

const GUIDE = [
  { icon: "🎙️", title: "강의 녹음하기",
    text: "강의가 시작되면 위의 <b>● 녹음하기</b>를 누르세요.<br>10초마다 저장돼서 중간에 창이 꺼져도 괜찮아요.<br><b>맥북 뚜껑은 닫지 마세요.</b> 녹음이 멈춰요." },
  { icon: "📁", title: "녹음 파일 올리기",
    text: "휴대폰으로 녹음한 파일도 화면에 <b>끌어다 놓으면</b> 돼요.<br>받아쓰기는 인터넷 없이 이 맥에서 해요.<br>1시간 강의에 5~10분쯤 걸려요." },
  { icon: "✏️", title: "고치고 찾기",
    text: "<b>화자 1</b>을 눌러 '교수님'처럼 이름을 바꿀 수 있어요.<br>틀린 문장은 <b>두 번 눌러</b> 고치고,<br>위 검색창으로 모든 강의에서 찾을 수 있어요." },
];
let guideStep = 0;

function showGuide(step = 0) {
  guideStep = step;
  renderGuide();
  const dlg = $("#guide-dialog");
  if (!dlg.open) dlg.showModal();
}

function renderGuide() {
  const g = GUIDE[guideStep];
  $("#guide-steps").innerHTML = `<div class="guide-icon">${g.icon}</div><h2>${g.title}</h2><p>${g.text}</p>`;
  $("#guide-dots").innerHTML = GUIDE.map((_, i) => `<span class="${i === guideStep ? "on" : ""}"></span>`).join("");
  $("#guide-prev").style.visibility = guideStep ? "visible" : "hidden";
  $("#guide-next").textContent = guideStep === GUIDE.length - 1 ? "시작하기" : "다음";
}

$("#guide-prev").addEventListener("click", () => { guideStep = Math.max(0, guideStep - 1); renderGuide(); });
$("#guide-next").addEventListener("click", () => {
  if (guideStep < GUIDE.length - 1) { guideStep++; renderGuide(); return; }
  $("#guide-dialog").close();
  try { localStorage.setItem("kkachi.guide.v1", "seen"); } catch {}
});
$("#guide-dialog").addEventListener("cancel", () => { try { localStorage.setItem("kkachi.guide.v1", "seen"); } catch {} });

function maybeShowGuide() {
  let seen = false;
  try { seen = localStorage.getItem("kkachi.guide.v1") === "seen"; } catch {}
  if (!seen) showGuide(0);
}

$("#btn-settings").addEventListener("click", (e) => { e.stopPropagation(); settingsMenu(e.currentTarget); });

/* ---------- 상태 갱신 ---------- */

async function refresh() {
  clearTimeout(state.pollTimer);
  if (appState.closed) return;
  try {
    state.recordings = await api("/api/recordings");
    renderList();
    appState.failures = 0;
    $("#offline-banner").hidden = true;
  } catch {
    // 서버가 잠깐 바쁠 수 있음. 두 번 연속 실패하면 꺼진 걸로 보고 안내
    if (++appState.failures >= 2 && !appState.updating) $("#offline-banner").hidden = false;
  }
  const busy = state.recordings.some((r) => isWorking(r) || r.status === "recording");
  state.pollTimer = setTimeout(refresh, busy || appState.failures ? 1000 : 5000);
}

async function loadSubjects() {
  try { state.subjects = await api("/api/subjects"); } catch {}
  const dl = $("#subject-list");
  if (dl) dl.innerHTML = state.subjects.map((s) => `<option value="${esc(s.name)}">`).join("");
}

/* ---------- 파일 선택 / 끌어다 놓기 ---------- */

$("#btn-upload").addEventListener("click", pickFile);
$("#btn-record").addEventListener("click", () => {
  if (recorder.phase === "idle") startRecording();
  else if (location.hash.startsWith("#/r/")) location.hash = "#/";
});
$("#file-input").addEventListener("change", (e) => acceptFile(e.target.files[0]));

let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (![...e.dataTransfer.types].includes("Files")) return;
  e.preventDefault();
  dragDepth++;
  $("#drop-overlay").hidden = false;
});
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").hidden = true; } });
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  $("#drop-overlay").hidden = true;
  acceptFile(e.dataTransfer.files[0]);
});

loadSubjects();
route();
maybeShowGuide();
