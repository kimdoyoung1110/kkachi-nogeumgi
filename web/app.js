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

function confirmDialog(text, okLabel = "삭제") {
  const dlg = $("#confirm-dialog");
  $("#confirm-text").textContent = text;
  $("#confirm-ok").textContent = okLabel;
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
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

function route() {
  const m = location.hash.match(/^#\/r\/([\w-]+)/);
  if (m) renderDetail(m[1]);
  else renderHome();
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

function renderUploadArea() {
  const area = $("#upload-area");
  if (!area) return;
  if (!state.pendingFile) {
    area.innerHTML = `
      <div class="dropzone" id="dropzone" role="button" tabindex="0">
        <div class="dropzone-icon">🎧</div>
        <div class="dropzone-title">녹음 파일을 끌어다 놓거나 눌러서 고르세요</div>
        <div class="dropzone-sub">m4a · mp3 · wav · 영상 파일도 돼요. 2시간 강의도 괜찮아요.</div>
      </div>`;
    const dz = $("#dropzone");
    dz.addEventListener("click", pickFile);
    dz.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && pickFile());
    return;
  }

  const f = state.pendingFile;
  const subjectOptions = state.subjects.map((s) => `<option value="${esc(s.name)}">`).join("");
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

      <label class="field">
        <span class="field-label">제목</span>
        <input class="input" name="title" value="${esc(f.name.replace(/\.[^.]+$/, ""))}" maxlength="120">
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
      </details>

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
  if (location.hash.startsWith("#/r/")) location.hash = "#/";
  state.pendingFile = file;
  renderUploadArea();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function submitUpload(form) {
  if (state.uploading) return;
  const fd = new FormData();
  fd.append("file", state.pendingFile);
  for (const name of ["title", "language", "num_speakers", "subject", "hotwords", "replacements"]) {
    fd.append(name, form.elements[name].value);
  }

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
      ${progress}${error}
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
    const ok = await confirmDialog(`'${r?.title ?? "이 녹음"}'을(를) 삭제할까요? 녹음 파일과 받아쓴 내용이 모두 지워져요.`);
    if (!ok) return;
    try { await api(`/api/recordings/${id}`, { method: "DELETE" }); toast("삭제했어요."); refresh(); }
    catch (err) { toast(err.message, "error"); }
  } else if (action === "retry") {
    try { await api(`/api/recordings/${id}/retry`, { method: "POST" }); refresh(); }
    catch (err) { toast(err.message, "error"); }
  } else if (item.dataset.status === "done") {
    location.hash = `#/r/${id}`;
  }
});

/* ---------- 결과 화면 (기본 보기, 편집은 다음 단계) ---------- */

async function renderDetail(id) {
  view.innerHTML = `<a class="back" href="#/">← 내 녹음</a><div class="empty">불러오는 중…</div>`;
  let d;
  try { d = await api(`/api/recordings/${id}`); }
  catch (err) { view.innerHTML = `<a class="back" href="#/">← 내 녹음</a><div class="empty">${esc(err.message)}</div>`; return; }

  const names = Object.fromEntries(d.speakers.map((s) => [s.idx, s.name]));
  const color = (i) => `var(--spk-${i % 8})`;
  view.innerHTML = `
    <a class="back" href="#/">← 내 녹음</a>
    <div class="detail-head">
      <h1>${esc(d.title)}</h1>
      <div class="rec-meta"><span>${fmtDate(d.created_at)}</span><span>${fmtDuration(d.duration)}</span><span>${LANG_LABEL[d.language]}</span></div>
    </div>
    <audio class="player" id="player" controls preload="metadata" src="/api/recordings/${d.id}/audio"></audio>
    <div class="speakers">${d.speakers.map((s) => `<span class="speaker-tag"><i style="background:${color(s.idx)}"></i>${esc(s.name)}</span>`).join("")}</div>
    <div class="transcript">
      ${d.utterances.map((u) => `
        <div class="utt">
          <div class="utt-bar" style="background:${color(u.speaker)}"></div>
          <div>
            <div class="utt-head">
              <span class="utt-speaker" style="color:${color(u.speaker)}">${esc(names[u.speaker] ?? `화자 ${u.speaker + 1}`)}</span>
              <span class="utt-time" data-t="${u.start}">${fmtClock(u.start)}</span>
              <span class="utt-lang">${u.language === "mixed" ? "KO·EN" : esc(u.language.toUpperCase())}</span>
            </div>
            <div class="utt-text">${esc(u.text)}</div>
          </div>
        </div>`).join("") || `<div class="empty">받아쓴 내용이 없어요. 말소리가 없는 녹음일 수 있어요.</div>`}
    </div>`;

  view.querySelector(".transcript").addEventListener("click", (e) => {
    const t = e.target.closest(".utt-time");
    if (!t) return;
    const p = $("#player");
    p.currentTime = Number(t.dataset.t);
    p.play();
  });
}

/* ---------- 상태 갱신 ---------- */

async function refresh() {
  clearTimeout(state.pollTimer);
  try {
    state.recordings = await api("/api/recordings");
    renderList();
  } catch {
    // 서버가 잠깐 바쁠 수 있음. 다음 주기에 다시 시도
  }
  const busy = state.recordings.some(isWorking);
  state.pollTimer = setTimeout(refresh, busy ? 1000 : 5000);
}

async function loadSubjects() {
  try { state.subjects = await api("/api/subjects"); } catch {}
  const dl = $("#subject-list");
  if (dl) dl.innerHTML = state.subjects.map((s) => `<option value="${esc(s.name)}">`).join("");
}

/* ---------- 파일 선택 / 끌어다 놓기 ---------- */

$("#btn-upload").addEventListener("click", pickFile);
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
