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

// 까치녹음기 앱 창(WKWebView) 안에서 열렸는지 (브라우저 탭이 아니라)
const IN_APP = navigator.userAgent.includes("KkachiApp");

// 앱 창(실행기)에 맥 알림을 부탁한다. 브라우저에서 열었으면 아무 일도 안 함
function nativeNotify(title, body, hash) {
  try { window.webkit?.messageHandlers?.kkachi?.postMessage({ type: "notify", title, body, hash }); } catch {}
}

// 앱 창의 실행기가 '다른 앱 소리 녹음'을 할 수 있는지 (업데이트 직후 예전 실행기면 없음 → 앱을 껐다 켜야 함)
const NATIVE_CAPTURE = IN_APP && !!window.KKACHI_NATIVE?.capture;
// 크롬(·엣지)에서 열었으면 탭 소리를 녹음할 수 있다. 사파리는 화면 공유로 소리를 못 가져옴
const CAN_TAB = !IN_APP && !!navigator.mediaDevices?.getDisplayMedia && /Chrome\/|Edg\//.test(navigator.userAgent);

// 앱 창(실행기)에 답을 받아야 하는 요청을 보낸다
function nativeCall(type, data = {}) {
  const h = window.webkit?.messageHandlers?.kkachiCall;
  if (!h) return Promise.reject(new Error("까치녹음기를 완전히 껐다(⌘Q) 다시 켜면 쓸 수 있어요."));
  return h.postMessage({ type, ...data });
}

async function openInChrome() {
  let res;
  try {
    res = await nativeCall("open-chrome", { hash: "#/" });
  } catch {
    window.open(`${location.origin}/`, "_blank");  // 예전 실행기: 기본 브라우저로 열린다
    toast("브라우저에서 열었어요. 탭 소리 녹음은 크롬에서만 돼요.");
    return;
  }
  toast(res?.ok
    ? "크롬에서 열었어요. 거기서 ● 녹음하기 › 크롬 탭 소리를 고르세요."
    : "크롬이 없어서 기본 브라우저로 열었어요. 탭 소리 녹음은 크롬에서만 돼요.", res?.ok ? "" : "error");
}

// 선물 설정 (이 맥의 gift.json: 이름). 없으면 빈 객체
state.gift = {};

function greetingText() {
  const name = state.gift.name;
  const n = name ? `, ${name}` : "";
  const h = new Date().getHours();
  if (h >= 5 && h < 11) return `좋은 아침이에요${n}! 오늘 수업도 화이팅 ☀️`;
  if (h >= 11 && h < 17) return `${name ? name + ", " : ""}오늘도 열공 중이네요 📚`;
  if (h >= 17 && h < 22) return `오늘도 고생 많았어요${n} 🌙`;
  return `늦게까지 고생이에요${n}. 너무 무리하지 마요 💤`;
}

function applyGiftBranding() {
  const name = state.gift.name;
  const title = name ? `${name}의 까치녹음기` : "까치녹음기";
  document.title = title;
  $(".brand-name").textContent = title;
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
  view.classList.remove("wide");
  closeSlides();
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
    <p class="greeting" id="greeting">${esc(greetingText())}</p>
    <section id="upload-area"></section>
    <section class="card study" id="study" hidden></section>
    <section>
      <div class="section-title"><h2>내 녹음</h2><span class="count" id="rec-count"></span></div>
      <div class="cat-bar" id="cat-bar" role="tablist" aria-label="분류"></div>
      <div class="rec-list" id="rec-list"></div>
    </section>`;
  renderUploadArea();
  renderList();
  refresh();
}

let optionsSeq = 0;

function optionsFieldsHTML(defaultTitle) {
  optionsSeq++;
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
          <span class="field-label">분류 <span class="field-hint">(선택)</span></span>
          <input class="input" name="subject" list="subject-list" placeholder="예: 자료구조, 팀 회의" autocomplete="off" value="${esc(currentCategoryName())}">
          <datalist id="subject-list">${subjectOptions}</datalist>
          <span class="field-hint">같은 분류끼리 모아 볼 수 있고, 아래 용어가 저장돼서 다음에 자동으로 채워져요.</span>
        </label>
      </div>

      <details class="advanced" id="advanced">
        <summary>용어 힌트 · 용어 바꾸기 (선택)</summary>
        <div class="advanced-body">
          <div class="field">
            <div class="field-label-row">
              <label class="field-label" for="hotwords-${optionsSeq}">용어 힌트</label>
              <button type="button" class="btn btn-sm btn-ghost" data-act="pick-doc">📄 강의자료에서 뽑기</button>
            </div>
            <textarea class="textarea" name="hotwords" id="hotwords-${optionsSeq}" placeholder="hash table, collision, linked list"></textarea>
            <span class="field-hint">강의에 자주 나오는 전문 용어를 쉼표나 줄바꿈으로 적어주세요. 강의자료(PDF·PPT)를 넣으면 알아서 찾아줘요.</span>
          </div>
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
    dz.addEventListener("click", chooseUploadSource);
    dz.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && chooseUploadSource());
    return;
  }

  const f = state.pendingFile;
  const files = state.pendingFiles || [f];
  const multi = files.length > 1;
  area.innerHTML = `
    <form class="card upload-form" id="upload-form" novalidate>
      <div class="upload-file">
        <span class="upload-file-icon">${multi ? "🎶" : "🎵"}</span>
        <div>
          <div class="upload-file-name">${multi ? `파일 ${files.length}개` : esc(f.name)}</div>
          <div class="upload-file-meta">${fmtBytes(files.reduce((a, x) => a + x.size, 0))}</div>
        </div>
        <button type="button" class="btn-link btn" id="change-file">다른 파일</button>
      </div>
      ${multi ? `<ul class="upload-list">${files.map((x) => `<li>${esc(x.name)}</li>`).join("")}</ul>
        <p class="field-hint">아래 설정을 모든 파일에 똑같이 써요. 제목은 각 파일 이름으로 붙고, 하나씩 차례대로 받아써요.</p>` : ""}
      ${optionsFieldsHTML(f.name.replace(/\.[^.]+$/, ""))}
      <div class="form-actions">
        <div class="upload-bar" id="upload-bar" hidden><div></div></div>
        <button type="button" class="btn btn-ghost" id="cancel-upload">취소</button>
        <button type="submit" class="btn btn-primary btn-lg" id="submit-upload">받아쓰기 시작</button>
      </div>
    </form>`;

  const form = $("#upload-form");
  if (multi) form.title.closest(".field").hidden = true;
  $("#change-file").addEventListener("click", pickFile);
  $("#cancel-upload").addEventListener("click", () => { state.pendingFile = state.pendingFiles = null; renderUploadArea(); });
  form.subject.addEventListener("change", () => fillSubjectTerms(form));
  if (form.subject.value) fillSubjectTerms(form);
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

/* 강의자료(PDF·PPT·워드)에서 전공 용어를 뽑아 용어 힌트에 넣는다 */
const DOC_RE = /\.(pdf|pptx|docx|txt|md)$/i;

function pickDoc(form) {
  const input = $("#doc-input");
  input.value = "";
  input.onchange = () => input.files[0] && extractTermsInto(form, input.files[0]);
  input.click();
}

async function extractTermsInto(form, file) {
  if (/\.(ppt|doc|hwp|hwpx)$/i.test(file.name)) {
    toast("예전 형식(ppt·doc)과 한글 파일은 못 읽어요. PDF 로 저장해서 넣어주세요.", "error");
    return;
  }
  const btn = form.querySelector("[data-act=pick-doc]");
  if (btn) { btn.disabled = true; btn.textContent = "📄 읽는 중…"; }
  const fd = new FormData();
  fd.append("file", file);
  let r;
  try {
    r = await api("/api/terms/extract", { method: "POST", body: fd });
  } catch (err) {
    toast(err.message, "error");
    return;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "📄 강의자료에서 뽑기"; }
  }
  const isPdf = /\.pdf$/i.test(file.name);
  if (!r.terms.length && !isPdf) { toast("강의자료에서 전공 용어를 찾지 못했어요.", "error"); return; }
  const have = new Set(form.hotwords.value.split(/[,\n]/).map((t) => t.trim().toLowerCase()).filter(Boolean));
  const picked = await termsDialog(file.name, r.terms.filter((t) => !have.has(t.toLowerCase())), isPdf);
  if (!picked) return;
  form._slides = picked.attach ? file : null;
  if (picked.attach) toast("받아쓰기를 시작하면 이 PDF 를 녹음에 붙여둘게요.");
  if (!picked.terms.length) return;
  const cur = form.hotwords.value.trim();
  form.hotwords.value = (cur ? cur.replace(/[,\s]+$/, "") + ", " : "") + picked.terms.join(", ");
  form.querySelector("#advanced").open = true;
  if (!picked.attach) toast(`용어 ${picked.terms.length}개를 넣었어요.`);
}

function termsDialog(fileName, terms, canAttach = false) {
  if (!terms.length && !canAttach) { toast("뽑은 용어가 이미 다 들어 있어요."); return Promise.resolve(null); }
  return sheetDialog((body, close) => {
    body.innerHTML = `
      <h3 class="source-title">📄 ${esc(fileName)}</h3>
      <p class="field-hint">${terms.length ? `받아쓰기에 참고할 용어 ${terms.length}개를 찾았어요. 빼고 싶은 건 눌러서 끄세요.` : "새로 넣을 용어는 없어요."}</p>
      <div class="term-chips">${terms.map((t, i) => `
        <label class="term-chip"><input type="checkbox" data-i="${i}" checked><span>${esc(t)}</span></label>`).join("")}</div>
      ${canAttach ? `<label class="toggle source-mic"><input type="checkbox" name="attach" checked><span>이 PDF 를 녹음에 붙여두기 (결과에서 슬라이드와 같이 보기)</span></label>` : ""}
      <div class="dialog-actions">
        <button type="button" class="btn btn-ghost" data-act="close">취소</button>
        <button type="button" class="btn btn-primary" data-act="ok">용어 넣기</button>
      </div>`;
    body.onclick = (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "close") close(null);
      if (act === "ok") close({
        terms: [...body.querySelectorAll("[data-i]:checked")].map((c) => terms[Number(c.dataset.i)]),
        attach: !!body.querySelector("[name=attach]")?.checked,
      });
    };
  });
}

/* 여러 용도로 쓰는 창: render(body, close) 로 내용을 그리고, close(값) 하면 그 값으로 끝난다 */
function sheetDialog(render) {
  const dlg = $("#sheet-dialog");
  const body = $("#sheet-body");
  return new Promise((resolve) => {
    const close = (value) => {
      body.onclick = dlg.oncancel = null;
      dlg.close();
      resolve(value);
    };
    dlg.oncancel = (e) => { e.preventDefault(); close(null); };
    render(body, close);
    if (!dlg.open) dlg.showModal();
  });
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act=pick-doc]");
  if (b) pickDoc(b.closest("form"));
});

/* ---------- 아이폰 음성 메모 가져오기 ---------- */

/* ---------- 아이폰에서 와이파이로 바로 보내기 (iCloud 없이) ---------- */

// 아이폰 음성 메모 → 맥: 와이파이로 바로 보내기가 기본, iCloud 로 넘어온 음성 메모 가져오기는 보조
async function phoneDialog(showGuide = false) {
  let st;
  try { st = await api("/api/phone"); } catch (err) { toast(err.message, "error"); return; }
  const url = st.urls?.[0] || "";
  const ask = `공유 시트에서 미디어와 파일을 받아서, ${url} 로 POST 방식으로 보내줘. 요청 본문은 파일로 하고 단축어 입력을 그대로 넣고, 헤더 X-File-Name 에 단축어 입력의 이름을 넣어줘. 받은 응답은 알림으로 보여줘. 공유 시트에서 보이게 해줘.`;
  const ready = st.enabled && st.running;
  await sheetDialog((body, close) => {
    body.innerHTML = `
      <h3 class="source-title">📱 아이폰 음성 메모 보내기</h3>
      <p class="guide-sub">아이폰 <b>음성 메모</b> 앱에서 녹음을 열고 <b>공유 › 까치녹음기로 보내기</b>를 누르면, 같은 와이파이의 이 맥으로 바로 올라가서 받아써요.</p>
      <label class="auto-row ${ready ? "is-on" : ""}">
        <input type="checkbox" name="phone-on" ${st.enabled ? "checked" : ""}>
        <span><b>${ready ? "✅ 와이파이로 받는 중" : "와이파이로 받기"}</b>
          <small>켜 둔 동안만 같은 와이파이의 아이폰이 PIN 이 들어간 주소로 녹음을 올릴 수 있어요. 녹음 목록이나 받아쓴 글은 볼 수 없어요.</small></span>
      </label>
      ${st.error ? `<div class="source-warn">${esc(st.error)}</div>` : ""}
      ${st.enabled ? `
        <div class="field">
          <span class="field-label">단축어에 넣을 주소</span>
          <div class="copy-row"><code class="copy-code">${esc(url)}</code><button type="button" class="btn btn-sm btn-ghost" data-copy="${esc(url)}">복사</button></div>
          ${st.urls[1] ? `<span class="field-hint">위 주소가 안 되면: <code>${esc(st.urls[1])}</code> <button type="button" class="btn-link btn" data-copy="${esc(st.urls[1])}">복사</button> (와이파이가 바뀌면 이 주소는 달라질 수 있어요)</span>` : ""}
          <span class="field-hint">PIN <b>${esc(st.pin)}</b> · <button type="button" class="btn-link btn" data-act="pin">PIN 바꾸기</button> (바꾸면 단축어 주소도 고쳐야 해요)</span>
        </div>
        <details class="guide-steps-box" ${showGuide ? "open" : ""}>
          <summary>처음 한 번: 아이폰에 단축어 만들기</summary>
          <p class="field-hint"><b>쉬운 방법</b> — 단축어 앱 › ＋ › 이름을 <b>까치녹음기로 보내기</b>로 바꾸고, 아래 <b>변경할 내용 설명</b> 칸에 이 문장을 붙여 넣어요.</p>
          <div class="copy-row"><code class="copy-code small">${esc(ask)}</code><button type="button" class="btn btn-sm btn-ghost" data-copy="${esc(ask)}">복사</button></div>
          <p class="field-hint"><b>직접 만들기</b></p>
          <ol class="source-steps">
            <li>단축어 앱 › <b>＋</b> › 이름을 <b>까치녹음기로 보내기</b>로 바꿔요.</li>
            <li><b>URL 콘텐츠 가져오기</b>를 넣고 URL 에 위 주소를 붙여 넣어요.</li>
            <li>▸ 를 펼쳐 방법 <b>POST</b>, 요청 본문 <b>파일</b>, 파일은 <b>단축어 입력</b>.</li>
            <li>(선택) 헤더 <code>X-File-Name</code> = <b>단축어 입력 › 이름</b> → 녹음 제목이 그대로 붙어요.</li>
            <li><b>알림 보기</b>를 넣고 내용에 <b>URL의 콘텐츠</b>를 넣어요.</li>
            <li>ⓘ(세부사항) › <b>공유 시트에서 보기</b>를 켜고, 받을 항목에 <b>미디어</b>와 <b>파일</b>을 체크해요.</li>
          </ol>
          <p class="field-hint">와이파이가 바뀌어도 위 주소(<b>맥 이름.local</b>)는 그대로예요. 아이폰과 맥이 <b>같은 와이파이</b>에만 있으면 돼요. 학교 와이파이처럼 기기끼리 막힌 곳에서는 아이폰 <b>개인용 핫스팟</b>을 켜고 맥을 거기에 연결한 뒤 보내면 돼요.</p>
          <p class="field-hint">처음 한 번 아이폰이 '로컬 네트워크' 접근을 물으면 허용해 주세요. 맥에서 '들어오는 연결 허용'을 물으면 허용을 눌러주세요.</p>
        </details>` : ""}
      <button type="button" class="phone-link" data-act="icloud">☁️ iCloud 로 맥에 넘어온 음성 메모 가져오기 · 자동으로 가져오기 ›</button>
      <div class="dialog-actions"><button type="button" class="btn btn-primary" data-act="close">확인</button></div>`;
    body.querySelector("[name=phone-on]").onchange = async (e) => {
      try {
        const on = e.target.checked;
        await api("/api/phone", jsonOpts("POST", { enabled: on }));
        close(null);
        setTimeout(() => phoneDialog(on), 50);
      } catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
    };
    body.onclick = async (e) => {
      const c = e.target.closest("[data-copy]");
      if (c) return copyText(c.dataset.copy, "복사했어요. 아이폰으로 보내서 붙여 넣어주세요.");
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "close") close(null);
      if (act === "icloud") { close(null); setTimeout(voiceMemoDialog, 50); }
      if (act === "pin") {
        if (!(await confirmDialog("PIN 을 바꿀까요? 아이폰 단축어의 주소도 새로 고쳐야 해요.", "바꾸기", { primary: true }))) return;
        await api("/api/phone/pin", { method: "POST" }).catch(() => {});
        close(null);
        setTimeout(() => phoneDialog(), 50);
      }
    };
  });
}

async function voiceMemoDialog() {
  let r, auto = { enabled: false };
  try { r = await api("/api/voicememos"); } catch (err) { toast(err.message, "error"); return; }
  try { auto = await api("/api/voicememos/auto"); } catch {}
  const phoneLinkHTML = `
    <button type="button" class="phone-link" data-act="phone">‹ 📶 <b>와이파이로 보내기</b>로 돌아가기</button>`;
  const autoHTML = `
    <label class="auto-row">
      <input type="checkbox" name="vm-auto" ${auto.enabled ? "checked" : ""}>
      <span><b>새 음성 메모 자동으로 가져오기</b>
        <small>켜두면 아이폰에서 녹음한 음성 메모가 맥으로 넘어오는 대로 알아서 받아써요. 켠 뒤에 녹음한 것만 가져와요.</small></span>
    </label>`;
  const picked = await sheetDialog((body, close) => {
    if (r.status === "permission") {
      body.innerHTML = `
        <h3 class="source-title">☁️ iCloud 로 넘어온 음성 메모</h3>
        <p>음성 메모 폴더를 읽으려면 맥에서 한 번 허용해야 해요. 허용하면 자동으로 가져오기도 켤 수 있어요.</p>
        <ol class="source-steps">
          <li><b>시스템 설정 › 개인정보 보호 및 보안 › 전체 디스크 접근 권한</b>을 열어요.</li>
          <li>목록에 <b>까치녹음기</b>가 이미 있으면 골라서 <b>−</b>로 지워요. (켜져 있어도 예전 버전 권한이라 안 될 수 있어요)</li>
          <li><b>＋</b>를 눌러 응용 프로그램의 <b>까치녹음기</b>를 추가하고 켜요.</li>
          <li>까치녹음기를 <b>⌘Q로 껐다 다시 켜요.</b></li>
        </ol>
        ${phoneLinkHTML}
        <div class="dialog-actions">
          <button type="button" class="btn btn-ghost" data-act="close">닫기</button>
          ${IN_APP ? `<button type="button" class="btn btn-primary" data-act="settings">설정 열기</button>` : ""}
        </div>`;
    } else if (r.status === "missing" || !r.items.length) {
      body.innerHTML = `
        <h3 class="source-title">☁️ iCloud 로 넘어온 음성 메모</h3>
        <p>이 맥에서 음성 메모를 찾을 수 없어요.</p>
        <ol class="source-steps">
          <li>아이폰 <b>설정 › 내 이름 › iCloud</b>에서 <b>음성 메모</b>를 켜요.</li>
          <li>맥에서도 같은 iCloud 계정으로 <b>음성 메모</b> 앱을 한 번 열어요.</li>
          <li>잠시 뒤 아이폰 녹음이 맥에 내려오면 여기서 가져올 수 있어요.</li>
        </ol>
        ${phoneLinkHTML}
        <div class="dialog-actions"><button type="button" class="btn btn-primary" data-act="close">확인</button></div>`;
    } else {
      const items = r.items.slice(0, 60);
      body.innerHTML = `
        <h3 class="source-title">☁️ iCloud 로 넘어온 음성 메모</h3>
        ${autoHTML}
        <div class="memo-list">${items.map((m, i) => `
          <label class="memo-item ${m.imported || !m.available ? "done" : ""}">
            <input type="checkbox" data-i="${i}" ${m.imported || !m.available ? "disabled" : ""}>
            <span class="memo-text"><b>${esc(m.title)}</b>
              <small>${m.date ? fmtDate(m.date) : ""}${m.duration ? ` · ${fmtDuration(m.duration)}` : ""}${m.imported ? " · 가져옴" : !m.available ? " · ☁️ 아직 맥에 안 내려왔어요" : ""}</small></span>
          </label>`).join("")}</div>
        <div class="memo-sync">
          <span>방금 녹음한 게 안 보이면 맥의 <b>음성 메모</b> 앱을 열어서 내려받아 주세요.</span>
          <span class="inbox-btns"><button type="button" class="btn btn-sm btn-ghost" data-act="open-vm">음성 메모 앱 열기</button>
          <button type="button" class="btn btn-sm btn-ghost" data-act="reload">다시 불러오기</button></span>
        </div>
        ${phoneLinkHTML}
        <label class="field">
          <span class="field-label">분류 <span class="field-hint">(선택)</span></span>
          <input class="input" name="vm-subject" list="subject-list" placeholder="예: 소비자행동" autocomplete="off" value="${esc(currentCategoryName())}">
          <span class="field-hint">분류에 저장해 둔 용어 힌트로 받아써요.</span>
        </label>
        <div class="dialog-actions">
          <button type="button" class="btn btn-ghost" data-act="close">취소</button>
          <button type="button" class="btn btn-primary" data-act="import" disabled>가져와서 받아쓰기</button>
        </div>`;
      const go = body.querySelector("[data-act=import]");
      body.querySelector("[name=vm-auto]").onchange = async (e) => {
        e.stopPropagation();
        try {
          const st = await api("/api/voicememos/auto", jsonOpts("POST", { enabled: e.target.checked }));
          toast(st.enabled ? "이제 새 음성 메모가 들어오면 알아서 받아써요. 🐦‍⬛" : "자동으로 가져오기를 껐어요.");
        } catch (err) { toast(err.message, "error"); e.target.checked = !e.target.checked; }
      };
      body.onchange = () => {
        const n = body.querySelectorAll("[data-i]:checked").length;
        go.disabled = !n;
        go.textContent = n ? `${n}개 가져와서 받아쓰기` : "가져와서 받아쓰기";
      };
      body.onclick = (e) => {
        const act = e.target.closest("[data-act]")?.dataset.act;
        if (act === "close") close(null);
        if (act === "phone") { close(null); setTimeout(phoneDialog, 50); }
        if (act === "open-vm") api("/api/voicememos/open", { method: "POST" }).catch(() => {});
        if (act === "reload") { close(null); setTimeout(voiceMemoDialog, 50); }
        if (act === "import") close({
          keys: [...body.querySelectorAll("[data-i]:checked")].map((c) => items[Number(c.dataset.i)].key),
          subject: body.querySelector("[name=vm-subject]").value.trim(),
        });
      };
      return;
    }
    body.onclick = (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "close") close(null);
      if (act === "phone") { close(null); setTimeout(phoneDialog, 50); }
      if (act === "settings") nativeCall("open-settings", { pane: "files" }).catch(() => toast("시스템 설정을 직접 열어주세요.", "error"));
    };
  });
  $("#sheet-body").onchange = null;
  if (!picked?.keys.length) return;
  try {
    const made = await api("/api/voicememos/import", jsonOpts("POST", picked));
    toast(made.length ? `음성 메모 ${made.length}개를 가져왔어요. 받아쓰기가 끝나면 알려드릴게요.` : "이미 가져온 음성 메모예요.");
    refresh();
    loadSubjects();
  } catch (err) {
    toast(err.message, "error");
  }
}

/* 파일 올리기: 이 맥의 파일 / 아이폰 음성 메모 중에서 고른다 */
async function chooseUploadSource() {
  if (recorder.phase !== "idle") { toast("녹음을 먼저 끝내주세요.", "error"); return; }
  const pick = await sheetDialog((body, close) => {
    body.innerHTML = `
      <h3 class="source-title">어디에 있는 녹음을 올릴까요?</h3>
      <div class="source-list">
        <button type="button" class="source-opt" data-src="file">
          <span class="source-icon">💻</span>
          <span class="source-text"><b>이 맥에 있는 파일</b><small>m4a · mp3 · wav · 영상 파일. 화면에 끌어다 놓아도 돼요</small></span>
        </button>
        <button type="button" class="source-opt" data-src="iphone">
          <span class="source-icon">📱</span>
          <span class="source-text"><b>아이폰 음성 메모</b><small>음성 메모에서 공유 › 까치녹음기로 보내기 하면 와이파이로 바로 와요</small></span>
        </button>
      </div>
      <div class="dialog-actions"><button type="button" class="btn btn-ghost" data-src="cancel">취소</button></div>`;
    body.onclick = (e) => {
      const src = e.target.closest("[data-src]")?.dataset.src;
      if (!src) return;
      close(src === "cancel" ? null : src);
      // 파일 고르기 창은 누른 그 순간에 열어야 앱 창(WebKit)이 막지 않는다
      if (src === "file") pickFile();
    };
  });
  if (pick === "iphone") phoneDialog();
}

/* ---------- 할 일 알림 (예: 융합전공 신청) ---------- */
// 그날 정한 시각(9·12·15·18·21시)마다 알림. [했어요]나 [알림 끄기]를 누르면 멈춘다.
// 새 실행기는 맥 알림으로 예약해서 앱이 꺼져 있어도 울리고, 화면은 위쪽 띠와 당일 안내 창을 맡는다.

const NATIVE_REMINDERS = IN_APP && !!window.KKACHI_NATIVE?.reminders;
state.reminders = [];

function daysUntil(date) {
  const [y, m, d] = date.split("-").map(Number);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((new Date(y, m - 1, d) - today) / 86_400_000);
}

function fmtDay(date) {
  const [y, m, d] = date.split("-").map(Number);
  const days = ["일", "월", "화", "수", "목", "금", "토"];
  return `${m}월 ${d}일 (${days[new Date(y, m - 1, d).getDay()]})`;
}

// 기간 알림이면 마지막 날까지. 시작이 30일 넘게 남은 건 아직 안 보여준다
const remEnd = (r) => r.end_date || r.date;
const remActive = (r) => daysUntil(r.date) <= 0 && daysUntil(remEnd(r)) >= 0;   // 오늘이 그날(기간 안)
const remindersShown = () => state.reminders.filter((r) => !r.done && daysUntil(remEnd(r)) >= 0 && daysUntil(r.date) <= 30);

function remWhen(r) {
  return r.end_date ? `${fmtDay(r.date)} ~ ${fmtDay(r.end_date).replace(/^\d+월 /, "")}` : fmtDay(r.date);
}

// 띠·안내 창 머리말: D-4 / 오늘! / 신청 기간 · 마지막 날
function remBadge(r) {
  if (!remActive(r)) return `📌 <b>D-${daysUntil(r.date)}</b>`;
  if (!r.end_date) return "🔔 <b>오늘!</b>";
  const left = daysUntil(r.end_date);
  return left === 0 ? "🔔 <b>오늘이 마지막 날!</b>" : `🔔 <b>지금 기간이에요 · ${left + 1}일 남음</b>`;
}

const DONE_LABEL = "다 했어요! 그만 보여줘도 돼요 ✓";

async function loadReminders(entry = false) {
  try { state.reminders = await api("/api/reminders"); } catch { return; }
  renderReminderBar();
  syncReminders();
  if (!entry) checkReminderNow();   // 앱에 들어올 때는 reminderOnEntry 가 대신 띄운다
}

function renderReminderBar() {
  const bar = $("#reminder-bar");
  const items = remindersShown();
  bar.hidden = !items.length;
  bar.innerHTML = items.map((r) => `
      <div class="rem ${remActive(r) ? "today" : ""}" data-rid="${esc(r.id)}">
        <span class="rem-text">${remBadge(r)} ${esc(r.title)}
          <span class="rem-date">· ${remWhen(r)}</span>${r.muted ? ` <span class="rem-muted" title="맥 알림 꺼짐">🔕</span>` : ""}</span>
        <span class="rem-actions">
          ${r.link ? `<a class="btn btn-sm btn-ghost" href="${esc(r.link)}" target="_blank" rel="noopener">바로 가기</a>` : ""}
          <button class="btn btn-sm btn-primary" data-rem="done">다 했어요 ✓</button>
          <button class="icon-btn" data-rem="menu" aria-label="알림 메뉴" title="알림 메뉴">⋯</button>
        </span>
      </div>`).join("");
}

async function patchReminder(id, body) {
  try {
    await api(`/api/reminders/${id}`, jsonOpts("PATCH", body));
  } catch (err) { toast(err.message, "error"); }
  await loadReminders();
}

async function reminderDone(id) {
  await patchReminder(id, { done: true });
  toast("잘했어요! 🎉 까치가 박수 치고 있어요. 이제 안 보여드릴게요.");
}

$("#reminder-bar").addEventListener("click", (e) => {
  const el = e.target.closest("[data-rid]");
  const act = e.target.closest("[data-rem]")?.dataset.rem;
  if (!el || !act) return;
  const r = state.reminders.find((x) => x.id === el.dataset.rid);
  if (act === "done") return reminderDone(r.id);
  if (act === "menu") {
    e.stopPropagation();
    openMenu(e.target.closest("[data-rem]"), [
      r.muted
        ? { label: "알림 다시 켜기", icon: "🔔", run: () => patchReminder(r.id, { muted: false }) }
        : { label: "맥 알림만 끄기", icon: "🔕", run: async () => { await patchReminder(r.id, { muted: true }); toast("맥 알림을 껐어요. 다 했으면 [다 했어요]를 눌러주세요."); } },
      { label: "할 일 알림 관리", icon: "📋", run: remindersDialog },
      "-",
      { label: "지우기", icon: "✕", danger: true, run: () => deleteReminder(r) },
    ]);
  }
});

async function deleteReminder(r) {
  if (!(await confirmDialog(`'${r.title}' 알림을 지울까요?`, "지우기"))) return;
  try { await api(`/api/reminders/${r.id}`, { method: "DELETE" }); } catch (err) { toast(err.message, "error"); }
  loadReminders();
}

// 맥 알림 예약: 끝냈거나 끈 알림은 빼고 보낸다 (실행기가 예전 것은 지우고 새로 건다)
function syncReminders() {
  if (!NATIVE_REMINDERS) return;
  const items = [];
  for (const r of state.reminders) {
    if (r.done || r.muted || daysUntil(remEnd(r)) < 0) continue;
    for (const date of remDays(r)) {
      items.push({ id: `${r.id}-${date}`, title: r.title, date, hours: r.hours, link: r.link || "" });
    }
  }
  nativeCall("reminders-sync", { items }).catch(() => {});
}

function remDays(r) {
  const [y, m, d] = r.date.split("-").map(Number);
  const out = [];
  for (let i = 0; i < 31; i++) {
    const day = new Date(y, m - 1, d + i);
    const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    out.push(key);
    if (key >= remEnd(r)) break;
  }
  return out;
}

// 당일 정한 시각이 지나면 화면에 큰 안내 창 (그 시각마다 한 번). 예전 실행기면 맥 알림도 화면이 보낸다
// 지금 시각의 알림 칸 (예: 13시면 12시 칸). 그 칸에 이미 보여줬는지 기억하는 열쇠
function remSlotKey(r) {
  const now = new Date();
  const slot = [...r.hours].reverse().find((h) => now.getHours() >= h);
  if (slot == null) return null;
  const today = new Date(now - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 10);
  return `kkachi.rem.${r.id}.${today}.${slot}`;
}

function markSlotSeen(r) {
  const key = remSlotKey(r);
  if (!key) return false;
  let seen = false;
  try { seen = localStorage.getItem(key) === "1"; localStorage.setItem(key, "1"); } catch {}
  return seen;
}

function checkReminderNow() {
  renderReminderBar();
  if (document.querySelector("dialog[open]")) return;   // 다른 창이 떠 있으면 다음에
  for (const r of state.reminders) {
    if (r.done || r.muted || !remActive(r) || !remSlotKey(r)) continue;
    if (markSlotSeen(r)) continue;
    if (!NATIVE_REMINDERS) nativeNotify(`🔔 ${r.title}!`, "아직 안 했으면 지금 해주세요. 다 했으면 까치녹음기에서 [다 했어요]를 눌러주세요.", "#/");
    reminderPopup(r);
    return;
  }
}

// 앱에 들어올 때마다 안내 창 (popup 이 켜진 알림). [다 했어요]를 눌러야 그만 뜬다
let entryShownAt = 0;
function reminderOnEntry() {
  const r = state.reminders.find((x) => x.popup && !x.done && daysUntil(remEnd(x)) >= 0 && daysUntil(x.date) <= 30);
  if (!r) return;
  if (document.querySelector("dialog[open]")) { setTimeout(reminderOnEntry, 1500); return; }   // 사용법·새 소식 창 다음에
  entryShownAt = Date.now();
  if (remActive(r)) markSlotSeen(r);   // 같은 시각 칸의 안내 창이 또 뜨지 않게
  reminderPopup(r);
}

async function reminderPopup(r) {
  const active = remActive(r);
  const lead = !active
    ? `<b>${remWhen(r)}</b>은<br><b>${esc(r.title)}</b> ${r.end_date ? "기간" : "날"}이에요! (D-${daysUntil(r.date)})`
    : r.end_date
      ? `지금 <b>${esc(r.title)}</b> 기간이에요!<br>${daysUntil(r.end_date) === 0 ? "<b>오늘이 마지막 날</b>이에요." : `${remWhen(r)} · <b>${daysUntil(r.end_date) + 1}일 남았어요</b>`}`
      : `오늘은 <b>${esc(r.title)}</b> 날이에요!`;
  const act = await sheetDialog((body, close) => {
    body.innerHTML = `
      <div class="rem-pop">
        <div class="rem-pop-icon">${active ? "🔔" : "📌"}</div>
        <h2>${lead}</h2>
        <p>${active ? "아직 안 했으면 지금 해주세요." : "잊지 않게 까치가 계속 알려드릴게요."}<br>다 했으면 아래 버튼을 눌러주세요. 그때부터 안 보여요.</p>
        <div class="rem-pop-main">
          ${r.link ? `<a class="btn btn-ghost btn-lg" href="${esc(r.link)}" target="_blank" rel="noopener">바로 가기</a>` : ""}
          <button type="button" class="btn btn-primary btn-lg" data-act="done">${DONE_LABEL}</button>
        </div>
        <div class="rem-pop-sub">
          <button type="button" class="btn-link btn" data-act="later">나중에 (다음에 또 알려줘요)</button>
        </div>
      </div>`;
    body.onclick = (e) => {
      const a = e.target.closest("[data-act]")?.dataset.act;
      if (a) close(a);
    };
  });
  if (act === "done") reminderDone(r.id);
}

async function remindersDialog() {
  await loadReminders();
  const list = state.reminders.filter((r) => daysUntil(r.date) >= -7);
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60_000).toISOString().slice(0, 10);
  const saved = await sheetDialog((body, close) => {
    body.innerHTML = `
      <h3 class="source-title">📋 할 일 알림</h3>
      <p class="field-hint">그날(기간이면 매일) 9·12·15·18·21시에 알려줘요. 전날 밤 9시에도 한 번 알려줘요. [다 했어요]를 누르면 멈춰요.</p>
      <div class="rem-list">${list.length ? list.map((r) => `
        <div class="rem-row ${r.done ? "done" : ""}">
          <span>${r.done ? "✅" : r.muted ? "🔕" : "🔔"} <b>${esc(r.title)}</b> <small>${remWhen(r)}${r.done ? " · 다 했어요" : ""}</small></span>
          <button type="button" class="btn btn-sm btn-ghost" data-toggle="${esc(r.id)}">${r.done ? "되돌리기" : "했어요"}</button>
        </div>`).join("") : `<p class="field-hint">아직 알림이 없어요.</p>`}</div>
      <div class="rem-add">
        <input class="input" name="rem-title" maxlength="100" placeholder="할 일 (예: 마케팅원론 과제 제출)">
        <input class="input" type="date" name="rem-date" min="${today}" title="날짜 (기간이면 시작하는 날)">
        <input class="input" name="rem-link" placeholder="바로 가기 주소 (선택)">
        <label class="rem-end">끝나는 날 (기간이면) <input class="input" type="date" name="rem-end" min="${today}"></label>
        <label class="toggle rem-pop-opt"><input type="checkbox" name="rem-popup"><span>앱 열 때마다 안내 창 띄우기</span></label>
      </div>
      <div class="dialog-actions">
        <button type="button" class="btn btn-ghost" data-act="close">닫기</button>
        <button type="button" class="btn btn-primary" data-act="add">알림 추가</button>
      </div>`;
    body.onclick = async (e) => {
      const t = e.target.closest("[data-toggle]");
      if (t) {
        const r = state.reminders.find((x) => x.id === t.dataset.toggle);
        close(null);
        await patchReminder(r.id, { done: !r.done });
        return remindersDialog();
      }
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "close") close(null);
      if (act === "add") {
        const title = body.querySelector("[name=rem-title]").value.trim();
        const date = body.querySelector("[name=rem-date]").value;
        if (!title || !date) { toast("할 일과 날짜를 적어주세요.", "error"); return; }
        close({ title, date, link: body.querySelector("[name=rem-link]").value.trim(),
          end_date: body.querySelector("[name=rem-end]").value, popup: body.querySelector("[name=rem-popup]").checked });
      }
    };
  });
  if (!saved) return;
  try {
    await api("/api/reminders", jsonOpts("POST", saved));
    toast(`알림을 추가했어요. ${fmtDay(saved.date)}부터 알려드릴게요.`);
  } catch (err) { toast(err.message, "error"); }
  loadReminders();
}

let hiddenAt = 0;
loadReminders(true).then(() => setTimeout(reminderOnEntry, 800));
setInterval(checkReminderNow, 60_000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { hiddenAt = Date.now(); return; }
  if (hiddenAt && Date.now() - hiddenAt > 10 * 60_000 && Date.now() - entryShownAt > 10 * 60_000) {
    loadReminders(true).then(reminderOnEntry);   // 한참 뒤에 다시 들어오면 또 안내
  } else {
    checkReminderNow();
  }
});

function pickFile() {
  $("#file-input").value = "";
  $("#file-input").click();
}

// 여러 파일을 고르거나 끌어다 놓았을 때: 소리·영상 파일만 모아 한꺼번에 올린다
function acceptFiles(list) {
  const all = [...(list || [])];
  if (all.length <= 1) return acceptFile(all[0]);
  const media = all.filter(isMediaFile);
  if (!media.length) return acceptFile(all[0]);   // 강의자료 등은 하나씩
  if (recorder.phase !== "idle") { toast("녹음을 먼저 끝내주세요.", "error"); return; }
  if (media.length < all.length) toast(`소리·영상 파일 ${media.length}개만 올릴게요.`);
  if (location.hash.startsWith("#/r/")) location.hash = "#/";
  state.pendingFile = media[0];
  state.pendingFiles = media.length > 1 ? media : null;
  renderUploadArea();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function isMediaFile(file) {
  return /^(audio|video)\//.test(file.type) || /\.(m4a|mp3|wav|webm|mp4|aac|ogg|flac|mov|caf|qta|m4v)$/i.test(file.name);
}

function acceptFile(file) {
  if (!file) return;
  if (DOC_RE.test(file.name) || /\.(ppt|doc|hwp|hwpx)$/i.test(file.name)) {
    const form = $("#upload-form") || $("#finish-form");
    if (form) extractTermsInto(form, file);
    else toast("강의자료는 녹음 파일을 고른 뒤 '용어 힌트'의 📄 강의자료에서 뽑기로 넣어주세요.", "error");
    return;
  }
  if (!isMediaFile(file)) { toast("소리 파일이 아닌 것 같아요. m4a, mp3, wav 같은 파일을 골라주세요.", "error"); return; }
  if (recorder.phase !== "idle") { toast("녹음을 먼저 끝내주세요.", "error"); return; }
  if (location.hash.startsWith("#/r/")) location.hash = "#/";
  state.pendingFile = file;
  state.pendingFiles = null;
  renderUploadArea();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// 파일 하나 올리기 (XHR: fetch 는 업로드 진행률을 못 알려줘서). 만든 녹음을 돌려준다
function uploadOne(file, form, onProgress, title) {
  const fd = new FormData();
  fd.append("file", file);
  appendOptions(fd, form);
  if (title != null) fd.set("title", title);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/recordings");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      if (xhr.status === 201) return resolve(JSON.parse(xhr.responseText));
      let msg = "올리지 못했어요. 다시 시도해 주세요.";
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch {}
      reject(new Error(msg));
    };
    xhr.onerror = () => reject(new Error("앱과 연결이 끊겼어요. 까치녹음기가 켜져 있는지 확인해 주세요."));
    xhr.send(fd);
  });
}

async function submitUpload(form) {
  if (state.uploading) return;
  const files = state.pendingFiles || [state.pendingFile];
  const multi = files.length > 1;
  state.uploading = true;
  const btn = $("#submit-upload");
  const bar = $("#upload-bar");
  btn.disabled = true;
  bar.hidden = false;

  const total = files.reduce((a, f) => a + f.size, 0) || 1;
  let sent = 0, done = 0;
  try {
    for (const [i, file] of files.entries()) {
      btn.textContent = multi ? `올리는 중 (${i + 1}/${files.length})…` : "올리는 중…";
      const rec = await uploadOne(file, form, (p) => {
        bar.firstElementChild.style.width = `${((sent + p * file.size) / total) * 100}%`;
      }, multi ? file.name.replace(/\.[^.]+$/, "") : null);
      sent += file.size;
      done++;
      if (form._slides && !multi) attachSlides(rec.id, form._slides);
    }
  } catch (err) {
    state.uploading = false;
    toast(done ? `${done}개는 올렸고, ${files[done].name}에서 멈췄어요: ${err.message}` : err.message, "error");
    if (done) { state.pendingFiles = files.slice(done); state.pendingFile = files[done]; renderUploadArea(); refresh(); return; }
    btn.disabled = false;
    btn.textContent = "받아쓰기 시작";
    bar.hidden = true;
    return;
  }
  state.uploading = false;
  state.pendingFile = state.pendingFiles = null;
  renderUploadArea();
  toast(multi ? `${files.length}개를 올렸어요. 하나씩 차례대로 받아써요.` : "올렸어요. 받아쓰기가 끝나면 목록에서 열 수 있어요.");
  refresh();
  loadSubjects();
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

// 남은 시간: 초 → "약 12분", "약 1시간 5분"
function fmtEta(sec) {
  if (sec < 60) return "1분 안에";
  const m = Math.round(sec / 60);
  return m < 60 ? `약 ${m}분` : `약 ${Math.floor(m / 60)}시간${m % 60 ? ` ${m % 60}분` : ""}`;
}

function queueInfo(r) {
  if (r.status === "processing") return r.eta_end != null ? `${fmtEta(r.eta_end)} 남음` : "";
  if (r.status !== "queued" || !r.queue_pos) return "";
  const when = r.eta_start == null ? "" : r.eta_start < 60 ? " · 곧 시작" : ` · ${fmtEta(r.eta_start)} 뒤 시작`;
  return `${r.queue_pos}번째${when}`;
}

function recItemHTML(r) {
  const meta = [
    fmtDate(r.created_at),
    r.duration ? fmtDuration(r.duration) : "",
    LANG_LABEL[r.language],
    r.subject ? `<span class="cat-tag">${esc(r.subject)}</span>` : "",
  ].filter(Boolean).map((x) => `<span>${x}</span>`).join("");

  const pct = Math.round((r.progress || 0) * 100);
  const progress = isWorking(r) ? `
    <div class="rec-progress">
      <div class="rec-progress-top">
        <span><img class="inline-emoji" src="emoji/magpie.webp" alt=""> <span class="stage">${esc(r.stage_label)}${r.status === "processing" ? "…" : ""}</span>
          ${queueInfo(r) ? `<span class="eta">${queueInfo(r)}</span>` : ""}</span>
        <span class="pct">${r.status === "processing" ? pct + "%" : ""}${r.status === "queued" && r.queue_pos > 1
          ? `<button class="btn btn-sm btn-ghost" data-action="prioritize" title="대기열 맨 앞으로">이것 먼저</button>` : ""}</span>
      </div>
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

/* 분류: "__all__" = 전체, "__none__" = 분류 없음, 그 외는 분류 이름 */
const CAT_ALL = "__all__", CAT_NONE = "__none__";
state.category = (() => { try { return localStorage.getItem("kkachi.category") || CAT_ALL; } catch { return CAT_ALL; } })();

function currentCategoryName() {
  return state.category === CAT_ALL || state.category === CAT_NONE ? "" : state.category;
}

function setCategory(cat) {
  state.category = cat;
  try { localStorage.setItem("kkachi.category", cat); } catch {}
  renderList();
}

function categoriesWithCounts() {
  // 최근에 쓴 분류가 앞으로
  const map = new Map();
  for (const r of state.recordings) {
    if (!r.subject) continue;
    const c = map.get(r.subject) || { name: r.subject, count: 0, last: 0 };
    c.count++;
    c.last = Math.max(c.last, r.created_at);
    map.set(r.subject, c);
  }
  return [...map.values()].sort((a, b) => b.last - a.last);
}

function inCategory(r) {
  if (state.day && dayKey(r.created_at) !== state.day) return false;
  if (state.category === CAT_ALL) return true;
  if (state.category === CAT_NONE) return !r.subject;
  return r.subject === state.category;
}

function renderCategories() {
  const bar = $("#cat-bar");
  if (!bar) return;
  const cats = categoriesWithCounts();
  const none = state.recordings.filter((r) => !r.subject).length;
  // 고른 분류가 사라졌으면 전체로
  if (state.category !== CAT_ALL && state.category !== CAT_NONE && !cats.some((c) => c.name === state.category)) state.category = CAT_ALL;
  if (state.category === CAT_NONE && !none) state.category = CAT_ALL;
  if (!cats.length && !state.day) { bar.innerHTML = ""; return; }
  const chip = (value, label, count) => `
    <button class="cat-chip ${state.category === value ? "on" : ""}" role="tab" aria-selected="${state.category === value}" data-cat="${esc(value)}">
      ${esc(label)}<span>${count}</span>
    </button>`;
  const dayChip = state.day
    ? `<button class="cat-chip on day-chip" data-clear-day>📅 ${esc(fmtDay(state.day))} ✕</button>` : "";
  bar.innerHTML = dayChip + chip(CAT_ALL, "전체", state.recordings.length)
    + cats.map((c) => chip(c.name, c.name, c.count)).join("")
    + (none ? chip(CAT_NONE, "분류 없음", none) : "");
}

/* ---------- 공부 기록: 까치 레벨 + 공부 잔디 ---------- */

// 그림: Google Noto 애니메이션 이모지 (web/emoji, CC BY 4.0).
// 4단계부터는 까치가 주인공: 레벨마다 깃털 색이 바뀌고 학사모·왕관을 머리에 쓴다 (scripts/make_level_art.py 로 생성)
const LEVELS = [
  { hours: 0, icon: "🥚", main: "egg.svg", title: "까치 알", line: "첫 강의를 받아쓰면 알을 깨고 나와요" },
  { hours: 1, icon: "🐣", main: "hatching.webp", title: "아기 까치", line: "알을 깨고 나왔어요!" },
  { hours: 5, icon: "🐥", main: "chick.webp", title: "꼬마 까치", line: "날갯짓을 배우는 중이에요" },
  { hours: 10, icon: "🐦‍⬛", main: "magpie-lv4.webp", title: "부지런한 까치", line: "매일매일 물어 나르는 중" },
  { hours: 25, icon: "📚", main: "magpie-lv5.webp", badge: "books.webp", title: "똑똑한 까치", line: "깃털에 푸른빛이 돌기 시작했어요" },
  { hours: 50, icon: "🎓", main: "magpie-lv6.webp", title: "박사 까치", line: "학사모를 썼어요! 깃털도 보랏빛으로 빛나요" },
  { hours: 100, icon: "👑", main: "magpie-lv7.webp", title: "수석 까치", line: "왕관을 썼어요! 날개선이 금빛이에요" },
  { hours: 200, icon: "✨", main: "magpie-lv8.webp", aura: true, title: "전설의 까치", line: "무지개빛 깃털, 전설로 남을 공부량이에요" },
];

function levelArt(li, size = "") {
  const lv = LEVELS[li];
  return `
    <div class="kk-art ${size} lv${li}" aria-hidden="true">
      ${lv.aura ? `<img class="kk-aura" src="emoji/sparkles.webp" alt="">` : ""}
      <img class="kk-main ${lv.main === "egg.svg" ? "wobble" : ""} ${lv.main.startsWith("magpie-lv") ? "padded" : ""}" src="emoji/${lv.main}" alt="">
      ${lv.badge ? `<img class="kk-badge" src="emoji/${lv.badge}" alt="">` : ""}
    </div>`;
}
const GRASS_WEEKS = 18;

function dayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function fmtDay(key) {
  const [y, m, d] = key.split("-").map(Number);
  const days = ["일", "월", "화", "수", "목", "금", "토"];
  return `${m}월 ${d}일 (${days[new Date(y, m - 1, d).getDay()]})`;
}

function fmtHours(sec) {
  const m = Math.round(sec / 60);
  if (m < 60) return `${m}분`;
  return m % 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : `${m / 60}시간`;
}

function studyStats() {
  // 받아쓰기가 끝난 녹음의 길이만 센다
  const byDay = new Map();
  let total = 0;
  for (const r of state.recordings) {
    if (r.status !== "done" || !r.duration) continue;
    const k = dayKey(r.created_at);
    const v = byDay.get(k) || { sec: 0, count: 0 };
    v.sec += r.duration;
    v.count++;
    byDay.set(k, v);
    total += r.duration;
  }
  // 연속 공부 일수: 오늘(또는 어제)부터 거꾸로
  let streak = 0;
  const d = new Date();
  if (!byDay.has(dayKey(d / 1000))) d.setDate(d.getDate() - 1);
  while (byDay.has(dayKey(d / 1000))) { streak++; d.setDate(d.getDate() - 1); }
  const now = new Date();
  const monthPrefix = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  let month = 0;
  for (const [k, v] of byDay) if (k.startsWith(monthPrefix)) month += v.sec;
  return { byDay, total, streak, month };
}

function levelFor(totalSec) {
  const h = totalSec / 3600;
  let i = 0;
  while (i + 1 < LEVELS.length && h >= LEVELS[i + 1].hours) i++;
  return i;
}

function grassLevel(sec) {
  const m = sec / 60;
  if (!m) return 0;
  if (m < 30) return 1;
  if (m < 60) return 2;
  if (m < 120) return 3;
  return 4;
}

function grassHTML(byDay) {
  // 월요일 시작 주 단위, 오늘이 마지막 열
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const start = new Date(today);
  const mondayOffset = (today.getDay() + 6) % 7;
  start.setDate(today.getDate() - mondayOffset - (GRASS_WEEKS - 1) * 7);
  const cols = [];
  const months = [];
  for (let w = 0; w < GRASS_WEEKS; w++) {
    const cells = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(start);
      d.setDate(start.getDate() + w * 7 + i);
      if (d > today) { cells.push(`<i class="grass-cell future"></i>`); continue; }
      const k = dayKey(d / 1000);
      const v = byDay.get(k) || { sec: 0, count: 0 };
      const tip = v.count ? `${fmtDay(k)} · ${fmtHours(v.sec)} · ${v.count}개` : `${fmtDay(k)} · 쉬는 날`;
      cells.push(`<i class="grass-cell lv${grassLevel(v.sec)} ${state.day === k ? "picked" : ""}" data-day="${k}" data-min="${Math.round(v.sec / 60)}" title="${tip}"></i>`);
      if (d.getDate() === 1 || (w === 0 && i === 0)) months[w] = `${d.getMonth() + 1}월`;
    }
    cols.push(`<div class="grass-col">${cells.join("")}</div>`);
  }
  const monthRow = Array.from({ length: GRASS_WEEKS }, (_, w) => `<span>${months[w] || ""}</span>`).join("");
  return `
    <div class="grass-wrap">
      <div class="grass-days"><span></span><span>월</span><span></span><span>수</span><span></span><span>금</span><span></span><span></span></div>
      <div>
        <div class="grass-months">${monthRow}</div>
        <div class="grass">${cols.join("")}</div>
      </div>
    </div>
    <div class="grass-legend">적게 <i class="grass-cell lv0"></i><i class="grass-cell lv1"></i><i class="grass-cell lv2"></i><i class="grass-cell lv3"></i><i class="grass-cell lv4"></i> 많이</div>`;
}

let studySig = "";
function renderStudy() {
  const el = $("#study");
  if (!el) return;
  const st = studyStats();
  const sig = JSON.stringify([...st.byDay]) + state.day;
  if (sig === studySig && !el.hidden) return;  // 바뀐 게 없으면 다시 그리지 않음 (목록은 1~5초마다 갱신됨)
  studySig = sig;

  const li = levelFor(st.total);
  const lv = LEVELS[li], next = LEVELS[li + 1];
  const name = state.gift.name ? `${esc(state.gift.name)}의 까치` : "내 까치";
  const progress = next
    ? Math.min(100, ((st.total / 3600 - lv.hours) / (next.hours - lv.hours)) * 100)
    : 100;
  el.hidden = false;
  el.innerHTML = `
    <div class="kk-level">
      <div class="level-icon">${levelArt(li)}</div>
      <div class="level-body">
        <div class="level-name">${name} · <b>Lv.${li + 1} ${lv.title}</b></div>
        <div class="level-line">${lv.line}</div>
        <div class="level-bar"><div style="width:${progress}%"></div></div>
        <div class="level-next">${next
          ? `다음 레벨 <b>${next.title}</b>까지 ${fmtHours(next.hours * 3600 - st.total)}`
          : "최고 레벨이에요! 🎉"}</div>
      </div>
    </div>
    <div class="study-grass">
      <div class="study-stats">
        <span><img class="inline-emoji" src="emoji/fire.webp" alt="🔥"> <b>${st.streak}일</b> 연속</span>
        <span>이번 달 <b>${fmtHours(st.month)}</b></span>
        <span>전체 <b>${fmtHours(st.total)}</b></span>
      </div>
      ${grassHTML(st.byDay)}
    </div>`;

  // 레벨업 축하 (처음 보는 경우엔 지금 레벨을 기억만 한다)
  let seen = null;
  try { seen = localStorage.getItem("kkachi.level"); } catch {}
  if (seen === null) {
    try { localStorage.setItem("kkachi.level", String(li)); } catch {}
  } else if (li > Number(seen)) {
    try { localStorage.setItem("kkachi.level", String(li)); } catch {}
    celebrateLevel(li);
  }
}

// 상단 이름 옆 로고 = 지금 레벨의 까치 (모든 화면에서 보임)
let brandLevel = null;
function updateBrandMark() {
  const li = levelFor(studyStats().total);
  if (li === brandLevel) return;
  brandLevel = li;
  const mark = $(".brand-mark");
  mark.innerHTML = levelArt(li, "tiny");
  mark.title = `Lv.${li + 1} ${LEVELS[li].title}`;
}

function celebrateLevel(li) {
  const lv = LEVELS[li];
  const name = state.gift.name ? `${state.gift.name}의 ` : "";
  nativeNotify("까치가 레벨업했어요! 🎉", `${name}까치가 Lv.${li + 1} ${lv.title}${lv.icon}이(가) 됐어요`);
  const box = document.createElement("div");
  box.className = "levelup";
  const confetti = Array.from({ length: 18 }, (_, i) =>
    `<span style="left:${(i * 53) % 100}%;animation-delay:${(i % 6) * 0.15}s">${["🎉", "✨", "⭐", "🪶"][i % 4]}</span>`).join("");
  box.innerHTML = `
    <div class="levelup-confetti" aria-hidden="true">${confetti}</div>
    <div class="levelup-card" role="dialog" aria-label="레벨업">
      <img class="levelup-party" src="emoji/party.webp" alt="">
      ${levelArt(li, "big")}
      <div class="levelup-small">레벨업!</div>
      <h2>Lv.${li + 1} ${esc(lv.title)}</h2>
      <p>${esc(lv.line)}</p>
      <button class="btn btn-primary">고마워 까치야 🐦‍⬛</button>
    </div>`;
  document.body.append(box);
  const close = () => box.remove();
  box.addEventListener("click", close);
  setTimeout(close, 12000);
}

// 받아쓰기가 끝나면 까치가 종이를 물어다 주는 작은 연출
function deliver(r) {
  const el = $(`#rec-list .rec[data-id="${r.id}"]`);
  if (!el) return;
  const bird = document.createElement("span");
  bird.className = "delivery";
  bird.innerHTML = `<img src="emoji/magpie.webp" alt="">📜`;
  el.append(bird);
  el.classList.add("delivered");
  setTimeout(() => { bird.remove(); el.classList.remove("delivered"); }, 2600);
  toast(`까치가 ‘${r.title}’ 받아쓴 걸 물어왔어요! 칭찬해 주세요 🐦‍⬛`);
}

function renderList() {
  const list = $("#rec-list");
  if (!list) return;
  $("#rec-count").textContent = state.recordings.length ? `${state.recordings.length}개` : "";
  renderStudy();
  renderCategories();

  if (!state.recordings.length) {
    list.innerHTML = `<div class="empty"><div class="empty-icon">🐦‍⬛</div>아직 녹음이 없어요.<br>위에서 파일을 올려보세요.</div>`;
    return;
  }
  const shown = state.recordings.filter(inCategory);
  if (!shown.length) {
    list.innerHTML = `<div class="empty">${state.day ? "이 날에는 녹음이 없어요." : "이 분류에는 녹음이 없어요."}</div>`;
    return;
  }

  // 진행률 막대가 부드럽게 움직이도록, 상태가 같은 항목은 숫자만 바꾼다
  const existing = new Map([...list.querySelectorAll(".rec")].map((el) => [el.dataset.id, el]));
  const frag = document.createDocumentFragment();
  const delivered = [];
  for (const r of shown) {
    const before = state.prevStatus?.[r.id];
    if (r.status === "done" && (before === "processing" || before === "queued")) delivered.push(r);
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
  state.prevStatus = Object.fromEntries(state.recordings.map((r) => [r.id, r.status]));
  for (const r of delivered) deliver(r);
}

$("#view").addEventListener("click", async (e) => {
  if (e.target.closest("[data-clear-day]")) { state.day = null; return renderList(); }
  const chip = e.target.closest(".cat-chip");
  if (chip) return setCategory(chip.dataset.cat);
  const cell = e.target.closest(".grass-cell[data-day]");
  if (cell && cell.dataset.min !== "0") {
    state.day = state.day === cell.dataset.day ? null : cell.dataset.day;
    renderList();
    $("#rec-list")?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
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
  } else if (action === "prioritize") {
    try { await api(`/api/recordings/${id}/prioritize`, { method: "POST" }); toast("다음 차례로 당겼어요."); refresh(); }
    catch (err) { toast(err.message, "error"); }
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
  pdf: null,         // 붙여 둔 강의자료 (PDF.js 문서)
  slidePage: 1,
  slideFollow: true,
  slideManualUntil: 0,
};

const spkColor = (i) => `var(--spk-${((i % 8) + 8) % 8})`;
const speakerName = (idx) => detail.data?.speakers.find((s) => s.idx === idx)?.name ?? `화자 ${idx + 1}`;
const langBadge = (l) => (l === "mixed" ? "KO·EN" : String(l || "").toUpperCase());

async function renderDetail(id, params = new URLSearchParams()) {
  clearTimeout(detail.pollTimer);
  closeSlides();
  Object.assign(detail, { id, data: null, activeUtt: null, activeWord: null, editing: null, onlyStarred: false });
  view.innerHTML = `<a class="back" href="#/">← 내 녹음</a><div class="empty">불러오는 중…</div>`;
  try { detail.data = await api(`/api/recordings/${id}`); }
  catch (err) { view.innerHTML = `<a class="back" href="#/">← 내 녹음</a><div class="empty">${esc(err.message)}</div>`; return; }
  const d = detail.data;

  view.innerHTML = `
    <a class="back" href="#/">← 내 녹음</a>
    <div class="detail-head">
      <h1 class="editable-title" id="title" title="눌러서 제목 바꾸기">${esc(d.title)}</h1>
      <div class="detail-meta-row">
        <div class="rec-meta"><span>${fmtDate(d.created_at)}</span><span>${fmtDuration(d.duration)}</span><span>${LANG_LABEL[d.language]}</span><button class="cat-btn" id="cat-btn" title="분류 바꾸기">${d.subject ? esc(d.subject) : "분류 없음"} ▾</button></div>
        <div class="detail-actions">
          <button class="btn btn-sm btn-primary" id="ai-copy">AI 요약용 복사 ▾</button>
          <button class="btn btn-sm btn-ghost" id="copy-all">전체 복사</button>
          <button class="btn btn-sm btn-ghost" id="export-menu">내보내기 ▾</button>
          <button class="btn btn-sm btn-ghost" id="slides-btn" title="강의 PDF를 붙여서 슬라이드와 같이 보기">📄 강의자료${d.slides ? " ▾" : " 붙이기"}</button>
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
        <label class="toggle star-toggle" id="star-filter" hidden><input type="checkbox" id="only-starred"><span>⭐ 중요만</span></label>
      </div>
    </div>
    <div class="speakers" id="speakers"></div>
    <p class="hint-line">이름을 누르면 바꾸거나 다른 화자와 합칠 수 있어요 · 문장을 두 번 누르면 고칠 수 있어요 · 단어를 누르면 그 부분부터 들려줘요</p>
    <div class="detail-body ${d.slides ? "with-slides" : ""}">
      <div class="transcript" id="transcript"></div>
      ${d.slides ? slidePaneHTML(d.slides) : ""}
    </div>`;
  view.classList.toggle("wide", !!d.slides);

  renderSpeakers();
  renderTranscript();
  bindDetail();
  if (d.slides) openSlides(Number(params.get("page")) || null);

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

/* ---------- 강의자료(슬라이드) 붙여 두기 ---------- */

function slidePaneHTML(sl) {
  return `
    <aside class="slide-pane" id="slide-pane">
      <div class="slide-head">
        <span class="slide-name" title="${esc(sl.name)}">📄 ${esc(sl.name)}</span>
        ${sl.has_text ? `<label class="toggle"><input type="checkbox" id="slide-follow" ${detail.slideFollow ? "checked" : ""}><span>따라가기</span></label>` : ""}
      </div>
      <div class="slide-frame" id="slide-frame"><canvas id="slide-canvas"></canvas><div class="slide-loading" id="slide-loading">불러오는 중…</div></div>
      <div class="slide-nav">
        <button class="icon-btn" id="slide-prev" aria-label="이전 쪽">◀</button>
        <span class="slide-num" id="slide-num">- / ${sl.pages}</span>
        <button class="icon-btn" id="slide-next" aria-label="다음 쪽">▶</button>
        <button class="btn btn-sm btn-ghost" id="slide-play" ${sl.has_text ? "" : "hidden"}>▶ 이 쪽 설명 듣기</button>
      </div>
      ${sl.has_text ? "" : `<p class="field-hint">글자가 없는 PDF(스캔본)라 재생에 맞춰 넘어가지는 않아요. 직접 넘겨서 보세요.</p>`}
    </aside>`;
}

// 문단 → 쪽 (서버가 맞춰 둔 것)
const slideOf = (u) => detail.data?.slides?.map?.[u.id];

// 쪽이 바뀌는 첫 문단에만 '📄 n쪽' 표시
function slideStarts() {
  const starts = new Map();
  if (!detail.data?.slides?.has_text) return starts;
  let prev = null;
  for (const u of detail.data.utterances) {
    const p = slideOf(u);
    if (p && (p !== prev || detail.onlyStarred)) starts.set(u.id, p);
    prev = p;
  }
  return starts;
}

let pdfjsLib = null;
async function loadPdfjs() {
  if (!pdfjsLib) {
    pdfjsLib = await import("/vendor/pdfjs/pdf.min.mjs");
    pdfjsLib.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";
  }
  return pdfjsLib;
}

async function openSlides(startPage) {
  const id = detail.id;
  try {
    const lib = await loadPdfjs();
    const pdf = await lib.getDocument({
      url: `/api/recordings/${id}/slides.pdf`,
      cMapUrl: "/vendor/pdfjs/cmaps/", cMapPacked: true,   // 한글 글꼴이 안 들어 있는 PDF 용
      standardFontDataUrl: "/vendor/pdfjs/standard_fonts/",
    }).promise;
    if (detail.id !== id || !$("#slide-canvas")) { pdf.destroy(); return; }
    detail.pdf = pdf;
  } catch {
    const l = $("#slide-loading");
    if (l) l.textContent = "강의자료를 열지 못했어요.";
    return;
  }
  $("#slide-prev").onclick = () => { detail.slideManualUntil = Date.now() + 20_000; showSlide(detail.slidePage - 1); };
  $("#slide-next").onclick = () => { detail.slideManualUntil = Date.now() + 20_000; showSlide(detail.slidePage + 1); };
  $("#slide-play").onclick = playSlide;
  $("#slide-follow")?.addEventListener("change", (e) => { detail.slideFollow = e.target.checked; detail.slideManualUntil = 0; });
  const first = detail.data.utterances.map(slideOf).find(Boolean) || 1;
  await showSlide(startPage || first);
  if (startPage) seekToSlide(startPage, false);  // 검색에서 쪽으로 들어온 경우: 그 쪽 설명 위치로
}

function closeSlides() {
  detail.renderTask?.cancel();
  detail.pdf?.destroy();
  Object.assign(detail, { pdf: null, renderTask: null, slidePage: 1 });
}

async function showSlide(n) {
  const pdf = detail.pdf;
  if (!pdf) return;
  n = Math.min(Math.max(1, n), pdf.numPages);
  detail.slidePage = n;
  $("#slide-num").textContent = `${n} / ${pdf.numPages}`;
  $("#slide-prev").disabled = n <= 1;
  $("#slide-next").disabled = n >= pdf.numPages;
  const page = await pdf.getPage(n);
  if (detail.pdf !== pdf || detail.slidePage !== n) return;
  const canvas = $("#slide-canvas");
  const frame = $("#slide-frame");
  if (!canvas) return;
  const base = page.getViewport({ scale: 1 });
  const scale = (frame.clientWidth / base.width) * (window.devicePixelRatio || 1);
  const vp = page.getViewport({ scale });
  detail.renderTask?.cancel();
  const off = document.createElement("canvas");   // 다 그린 뒤 바꿔 끼워서 깜빡이지 않게
  off.width = vp.width;
  off.height = vp.height;
  detail.renderTask = page.render({ canvasContext: off.getContext("2d"), viewport: vp });
  try { await detail.renderTask.promise; } catch { return; }  // 다른 쪽으로 넘어가서 취소됨
  canvas.width = off.width;
  canvas.height = off.height;
  canvas.getContext("2d").drawImage(off, 0, 0);
  $("#slide-loading").hidden = true;
}

function followSlide(u) {
  if (!detail.pdf || !detail.slideFollow || Date.now() < detail.slideManualUntil) return;
  const p = slideOf(u);
  if (p && p !== detail.slidePage) showSlide(p);
}

// 지금 보는 쪽을 설명한 첫 부분으로
function seekToSlide(n, play = true) {
  const u = detail.data.utterances.find((x) => slideOf(x) === n);
  if (!u) { toast("이 쪽을 설명한 부분을 찾지 못했어요."); return; }
  detail.slideManualUntil = 0;
  view.querySelector(`.utt[data-id="${u.id}"]`)?.scrollIntoView({ block: "center" });
  if (play) seek(u.start);
  else {
    const p = $("#player");
    const set = () => { p.currentTime = Math.max(0, u.start - 0.05); };
    p.readyState >= 1 ? set() : p.addEventListener("loadedmetadata", set, { once: true });
  }
}

function playSlide() { seekToSlide(detail.slidePage); }

let slideResizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(slideResizeTimer);
  slideResizeTimer = setTimeout(() => detail.pdf && showSlide(detail.slidePage), 200);
});

async function slidesMenu(anchor) {
  if (!detail.data.slides) return pickSlides();
  openMenu(anchor, [
    { label: "다른 PDF로 바꾸기", icon: "📄", run: pickSlides },
    { label: "강의자료 떼기", icon: "✕", danger: true, run: detachSlides },
  ]);
}

function pickSlides() {
  const input = $("#doc-input");
  input.value = "";
  input.accept = ".pdf";
  input.onchange = () => {
    input.accept = ".pdf,.pptx,.docx,.txt,.md";
    if (input.files[0]) attachSlides(detail.id, input.files[0]).then((ok) => ok && renderDetail(detail.id));
  };
  input.click();
}

/* 녹음에 PDF 붙이기. 결과 화면과 업로드 화면에서 같이 쓴다 */
async function attachSlides(recId, file) {
  if (!/\.pdf$/i.test(file.name)) {
    toast("PDF 만 붙일 수 있어요. 파워포인트는 'PDF로 내보내기' 한 뒤 붙여주세요.", "error");
    return false;
  }
  toast("강의자료를 붙이는 중…");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await api(`/api/recordings/${recId}/slides`, { method: "POST", body: fd });
    toast(r.terms_added.length
      ? `강의자료를 붙였어요. 전공 용어 ${r.terms_added.length}개는 용어 힌트에 넣어뒀어요.`
      : "강의자료를 붙였어요.");
    return true;
  } catch (err) {
    toast(err.message, "error");
    return false;
  }
}

async function detachSlides() {
  if (!(await confirmDialog("붙여 둔 강의자료를 뗄까요? 받아쓴 내용은 그대로예요.", "떼기"))) return;
  try {
    await api(`/api/recordings/${detail.id}/slides`, { method: "DELETE" });
    renderDetail(detail.id);
  } catch (err) { toast(err.message, "error"); }
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
- ★로 시작하는 줄은 녹음한 사람이 '중요'라고 표시한 부분이니 빠뜨리지 말고 강조하기
- 📝 메모는 녹음한 사람이 그 순간 적어 둔 것이니 정리에 반영하기

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
- ★로 시작하는 줄은 녹음한 사람이 '중요'라고 표시한 부분이니 빠뜨리지 말고 강조하기

형식 (노션에 붙여넣기 좋게 마크다운으로):
1. 강의 주제와 핵심 요약 (3~5줄)
2. 개념 정리 (소제목별로 정의·예시·공식)
3. 교수님이 강조했거나 시험에 나온다고 한 내용 (★ 표시한 부분 포함)
4. 질문과 답변
5. 과제·공지 (마감일 포함)`,
};

function transcriptForAI(kind) {
  // 같은 화자가 이어서 말한 문단은 한 줄로 합쳐서 짧고 읽기 쉽게 만든다
  const d = detail.data;
  const stars = starredIds();
  const notes = noteMap();
  const lines = [];
  let cur = null;
  for (const u of d.utterances) {
    const star = stars.has(u.id);
    const memo = (notes.get(u.id) || []).map((n) => n.text);
    if (cur && cur.speaker === u.speaker) { cur.text += " " + u.text; cur.star ||= star; cur.memo.push(...memo); continue; }
    if (cur) lines.push(cur);
    cur = { speaker: u.speaker, start: u.start, text: u.text, star, memo };
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
    ...lines.map((l) => `${l.star ? "★ " : ""}${speakerName(l.speaker)} (${fmtClock(l.start)}): ${l.text}`
      + l.memo.map((m) => `\n  📝 녹음한 사람의 메모: ${m}`).join("")),
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

async function categoryMenu(anchor) {
  const names = [...new Set([...categoriesWithCounts().map((c) => c.name), ...state.subjects.map((x) => x.name)])];
  const cur = detail.data.subject || "";
  const items = names.map((n) => ({
    label: n + (n === cur ? "  ✓" : ""), icon: "📁", disabled: n === cur, run: () => setRecordingCategory(n),
  }));
  if (items.length) items.push("-");
  items.push({ label: "새 분류 만들기…", icon: "＋", run: newCategory });
  if (cur) items.push({ label: "분류 없음으로", icon: "✕", run: () => setRecordingCategory("") });
  openMenu(anchor, items);
}

async function newCategory() {
  const ok = await confirmDialog(
    `<b>새 분류 이름</b><input class="input" id="new-cat" maxlength="40" placeholder="예: 자료구조, 팀 회의" style="margin-top:10px">`,
    "만들기", { html: true, primary: true });
  const name = ($("#new-cat")?.value || "").trim();
  if (ok && name) setRecordingCategory(name);
}

async function setRecordingCategory(name) {
  try {
    const r = await api(`/api/recordings/${detail.id}`, jsonOpts("PATCH", { subject: name }));
    detail.data.subject = r.subject;
    $("#cat-btn").textContent = `${r.subject || "분류 없음"} ▾`;
    const i = state.recordings.findIndex((x) => x.id === r.id);
    if (i >= 0) state.recordings[i] = r;
    loadSubjects();
    toast(r.subject ? `'${r.subject}' 분류로 옮겼어요.` : "분류를 뺐어요.");
  } catch (err) { toast(err.message, "error"); }
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

/* 시간 → 그 시간에 말하던 문단 (중요 표시·메모 공통) */
function uttAt(t) {
  const utts = detail.data.utterances;
  let hit = utts[0];
  for (const u of utts) {
    if (u.start <= t + 0.3) hit = u; else break;
  }
  return hit;
}

/* 메모 → 문단 id 별 목록 */
function noteMap() {
  const map = new Map();
  for (const n of detail.data.notes || []) {
    const u = uttAt(n.t);
    if (!u) continue;
    if (!map.has(u.id)) map.set(u.id, []);
    map.get(u.id).push(n);
  }
  return map;
}

function notesHTML(u, notes = []) {
  const draft = detail.noteDraft === u.id;
  if (!notes.length && !draft) return "";
  return `<div class="utt-notes">${notes.map((n) => `
      <div class="utt-note" data-note="${n.id}">
        <button class="utt-note-text" data-note-edit title="눌러서 고치기">📝 ${esc(n.text)}</button>
        <button class="utt-note-del" data-note-del title="메모 지우기" aria-label="메모 지우기">×</button>
      </div>`).join("")}
    ${draft ? `<input class="input utt-note-input" data-note-new maxlength="500" placeholder="메모 적고 Enter (Esc 취소)">` : ""}
  </div>`;
}

function startNote(u) {
  detail.noteDraft = u.id;
  renderTranscript();
  view.querySelector("[data-note-new]")?.focus();
}

async function saveNewNote(input) {
  const u = detail.data.utterances.find((x) => x.id === detail.noteDraft);
  const text = input.value.trim();
  detail.noteDraft = null;
  if (u && text) {
    try {
      detail.data.notes = (await api(`/api/recordings/${detail.id}/notes`, jsonOpts("POST", { t: u.start + 0.05, text }))).notes;
    } catch (err) { toast(err.message, "error"); }
  }
  renderTranscript();
}

function editNote(btn) {
  const id = Number(btn.closest("[data-note]").dataset.note);
  const note = detail.data.notes.find((n) => n.id === id);
  const input = document.createElement("input");
  input.className = "input utt-note-input";
  input.maxLength = 500;
  input.value = note.text;
  btn.replaceWith(input);
  input.focus();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (commit && v && v !== note.text) {
      try {
        detail.data.notes = (await api(`/api/recordings/${detail.id}/notes/${id}`, jsonOpts("PATCH", { text: v }))).notes;
      } catch (err) { toast(err.message, "error"); }
    }
    renderTranscript();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

async function deleteNote(btn) {
  const id = Number(btn.closest("[data-note]").dataset.note);
  try {
    detail.data.notes = (await api(`/api/recordings/${detail.id}/notes/${id}`, { method: "DELETE" })).notes;
    renderTranscript();
  } catch (err) { toast(err.message, "error"); }
}

/* 중요 표시(시간) → 그 시간에 말하던 문단 */
function starredIds() {
  const ids = new Set();
  for (const t of detail.data.marks || []) {
    const hit = uttAt(t);
    if (hit) ids.add(hit.id);
  }
  return ids;
}

async function toggleStar(u) {
  const utts = detail.data.utterances;
  const i = utts.findIndex((x) => x.id === u.id);
  try {
    let r;
    if (starredIds().has(u.id)) {
      const end = i + 1 < utts.length ? utts[i + 1].start - 0.31 : u.end + 60;
      r = await api(`/api/recordings/${detail.id}/marks?start=${u.start - 0.3}&end=${end}`, { method: "DELETE" });
    } else {
      r = await api(`/api/recordings/${detail.id}/marks`, jsonOpts("POST", { t: u.start + 0.05 }));
    }
    detail.data.marks = r.marks;
    renderTranscript();
    updateStarFilter();
  } catch (err) { toast(err.message, "error"); }
}

function updateStarFilter() {
  const ids = new Set([...starredIds(), ...noteMap().keys()]);
  const n = ids.size;
  const el = $("#star-filter");
  if (el) {
    el.hidden = !n && !detail.onlyStarred;
    el.querySelector("span").textContent = `⭐ 중요·📝 메모만 (${n})`;
  }
}

function uttHTML(u, starred = false, notes = []) {
  const editing = detail.editing === u.id;
  return `
    <div class="utt ${u.busy ? "busy" : ""} ${detail.activeUtt === u.id ? "active" : ""} ${starred ? "starred" : ""}" data-id="${u.id}" data-start="${u.start}" data-end="${u.end}">
      <div class="utt-bar" style="background:${spkColor(u.speaker)}"></div>
      <div class="utt-body">
        <div class="utt-head">
          <button class="utt-speaker" data-menu="speaker" style="color:${spkColor(u.speaker)}" title="다른 화자로 바꾸기">${esc(speakerName(u.speaker))} ▾</button>
          <button class="utt-time" data-seek="${u.start}">${fmtClock(u.start)}</button>
          <button class="utt-star ${starred ? "on" : ""}" data-star title="${starred ? "중요 표시 빼기" : "중요 표시"}">${starred ? "⭐" : "☆"}</button>
          <span class="utt-lang">${langBadge(u.language)}</span>
          ${detail.slideStarts?.has(u.id) ? `<button class="utt-slide" data-slide="${detail.slideStarts.get(u.id)}" title="이 슬라이드 보기">📄 ${detail.slideStarts.get(u.id)}쪽</button>` : ""}
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
        ${notesHTML(u, notes)}
      </div>
    </div>`;
}

function renderTranscript() {
  const el = $("#transcript");
  if (!el) return;
  detail.slideStarts = slideStarts();
  const stars = starredIds();
  const notes = noteMap();
  const utts = detail.onlyStarred ? detail.data.utterances.filter((u) => stars.has(u.id) || notes.has(u.id)) : detail.data.utterances;
  el.innerHTML = utts.length
    ? utts.map((u) => uttHTML(u, stars.has(u.id), notes.get(u.id))).join("")
    : `<div class="empty">받아쓴 내용이 없어요. 말소리가 없는 녹음일 수 있어요.</div>`;
  updateStarFilter();
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
    tmp.innerHTML = uttHTML(u, starredIds().has(u.id), noteMap().get(u.id)).trim();
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
    { label: "메모 달기", icon: "📝", run: () => startNote(u) },
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
      followSlide(cur);
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
  $("#slides-btn").addEventListener("click", (e) => { e.stopPropagation(); slidesMenu(e.currentTarget); });
  $("#only-starred").addEventListener("change", (e) => { detail.onlyStarred = e.target.checked; renderTranscript(); });
  updateStarFilter();
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
  const cb = e.target.closest("#cat-btn");
  if (cb) { e.stopPropagation(); return categoryMenu(cb); }
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
  if (e.target.closest("[data-star]")) return toggleStar(u);
  const sl = e.target.closest("[data-slide]");
  if (sl) { detail.slideManualUntil = Date.now() + 20_000; return showSlide(Number(sl.dataset.slide)); }
  if (e.target.closest("[data-note-new]")) return;
  const ne = e.target.closest("[data-note-edit]");
  if (ne) return editNote(ne);
  const nd = e.target.closest("[data-note-del]");
  if (nd) return deleteNote(nd);
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "save-edit") return saveEdit();
  if (act === "cancel-edit") return cancelEdit();
  const sk = e.target.closest("[data-seek]");
  if (sk) return seek(Number(sk.dataset.seek));
  const w = e.target.closest(".w");
  if (w && !window.getSelection().toString()) return seek(Number(w.dataset.t));
}

view.addEventListener("keydown", (e) => {
  const input = e.target.closest?.("[data-note-new]");
  if (!input) return;
  if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); saveNewNote(input); }
  if (e.key === "Escape") { detail.noteDraft = null; renderTranscript(); }
});
view.addEventListener("focusout", (e) => {
  const input = e.target.closest?.("[data-note-new]");
  if (input && detail.noteDraft) saveNewNote(input);
});

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
      <h2 class="search-title">‘${esc(r.query)}’ ${r.count ? `${r.count}건${r.count >= 200 ? " 이상" : ""}` : (r.slides?.length ? "" : "결과 없음")}</h2>
      ${r.count || r.slides?.length ? "" : `<div class="empty">찾는 말이 들어간 문장이 없어요. 띄어쓰기나 철자를 바꿔보세요.</div>`}
      ${r.slides?.length ? `
        <section class="card search-group">
          <div class="search-group-head"><span class="rec-title">📄 강의자료에서</span><span class="rec-meta"><span>${r.slides.length}쪽</span></span></div>
          ${r.slides.map((h) => `
            <a class="hit" href="#/r/${h.recording_id}?page=${h.page}">
              <span class="hit-time">${h.page}쪽</span>
              <span class="hit-body"><b class="hit-speaker">${esc(h.title)}</b> ${markAll(h.text, r.query)}</span>
            </a>`).join("")}
        </section>` : ""}
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
  phase: "idle",          // idle → starting → recording → stopping → finished → idle
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

const MIC_CONSTRAINTS = { echoCancellation: false, noiseSuppression: false, autoGainControl: true };

function micErrorMessage(err) {
  const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
  return denied
    ? (IN_APP
      ? "마이크를 쓸 수 없어요. 시스템 설정 › 개인정보 보호 및 보안 › 마이크에서 '까치녹음기'를 켜주세요."
      : "마이크를 쓸 수 없어요. 브라우저 주소창의 마이크 권한을 허용하고, 시스템 설정 › 개인정보 보호 및 보안 › 마이크에서 브라우저를 켜주세요.")
    : "마이크를 찾을 수 없어요. 마이크가 연결돼 있는지 확인해 주세요.";
}

/* 녹음 버튼 → 무엇을 녹음할지 고르는 창. 고른 것을 돌려준다 (취소하면 null) */
function loadSourcePref() {
  try { return JSON.parse(localStorage.getItem("kkachi.recordSource") || "{}"); } catch { return {}; }
}
function saveSourcePref(p) {
  try { localStorage.setItem("kkachi.recordSource", JSON.stringify({ ...loadSourcePref(), ...p })); } catch {}
}

function chooseRecordSource() {
  const dlg = $("#source-dialog");
  const body = $("#source-body");
  const pref = loadSourcePref();
  const opt = (src, icon, title, sub, disabled = false) => `
    <button type="button" class="source-opt" data-src="${src}" ${disabled ? "disabled" : ""}>
      <span class="source-icon">${icon}</span>
      <span class="source-text"><b>${title}</b><small>${sub}</small></span>
    </button>`;
  const micCheck = (checked) => `<label class="toggle source-mic"><input type="checkbox" name="mic" ${checked ? "checked" : ""}><span>내 목소리(마이크)도 같이 녹음</span></label>`;

  const home = () => {
    const appOpt = IN_APP
      ? opt("app", "🖥️", "다른 앱 소리", NATIVE_CAPTURE ? "Zoom·유튜브·카카오톡 통화처럼 이 맥에서 나는 소리" : "까치녹음기를 완전히 껐다(⌘Q) 다시 켜면 쓸 수 있어요", !NATIVE_CAPTURE)
      : opt("app", "🖥️", "다른 앱 소리", "까치녹음기 앱 창에서만 돼요", true);
    const tabOpt = IN_APP
      ? opt("chrome", "🌐", "크롬 탭 소리", "크롬에서 열어서 녹음해요 (온라인 강의·유튜브 탭)")
      : opt("tab", "🌐", "크롬 탭 소리", CAN_TAB ? "온라인 강의·유튜브처럼 크롬 탭에서 나는 소리" : "크롬에서 열었을 때만 돼요", !CAN_TAB);
    body.innerHTML = `
      <h3 class="source-title">무엇을 녹음할까요?</h3>
      <div class="source-list">${opt("mic", "🎙️", "마이크", "강의실·회의처럼 내 주변 소리")}${appOpt}${tabOpt}</div>
      <div class="dialog-actions"><button type="button" class="btn btn-ghost" data-act="cancel">취소</button></div>`;
  };

  const appStep = async () => {
    body.innerHTML = `<h3 class="source-title">어떤 앱 소리를 녹음할까요?</h3><p class="field-hint">앱 목록을 불러오는 중…</p>`;
    let info;
    try { info = await nativeCall("capture-apps"); } catch (err) { toast(err.message, "error"); home(); return; }
    // 최근에 녹음한 앱이 앞으로
    const recent = pref.recent || [];
    const rank = (a) => { const i = recent.indexOf(a.bundle); return i < 0 ? recent.length : i; };
    const apps = [...info.apps].sort((a, b) => rank(a) - rank(b));
    const chosen = apps.some((a) => a.bundle === pref.bundle) ? pref.bundle : "";
    const card = (bundle, name, icon, sub = "") => `
      <label class="app-card" data-name="${esc(name.toLowerCase())}" title="${esc(name)}">
        <input type="radio" name="app" value="${esc(bundle)}" data-label="${esc(name)}" ${bundle === chosen ? "checked" : ""}>
        <span class="app-icon">${icon}</span>
        <span class="app-name">${esc(name)}</span>
        ${sub ? `<span class="app-sub">${sub}</span>` : ""}
      </label>`;
    const appIcon = (a) => a.icon ? `<img src="${a.icon}" alt="">` : `<span class="app-letter">${esc(a.name.slice(0, 1))}</span>`;
    body.innerHTML = `
      <h3 class="source-title">어떤 앱 소리를 녹음할까요?</h3>
      ${info.permitted ? "" : `
        <div class="source-warn">
          <span>처음 한 번 맥에서 <b>화면 및 시스템 오디오 녹음</b>을 허용해야 해요. 허용한 뒤에는 까치녹음기를 껐다(⌘Q) 다시 켜주세요.</span>
          <button type="button" class="btn btn-sm btn-ghost" data-act="permit">권한 허용하기</button>
        </div>`}
      ${apps.length > 8 ? `<input class="input app-filter" name="app-filter" placeholder="앱 이름으로 찾기" autocomplete="off">` : ""}
      <div class="app-grid">
        ${card("", "맥 전체 소리", `<span class="app-emoji">🖥️</span>`, "모든 앱")}
        ${apps.map((a) => card(a.bundle, a.name, appIcon(a), recent.includes(a.bundle) ? "최근" : "")).join("")}
      </div>
      <p class="field-hint">녹음할 앱이 없으면 그 앱을 먼저 켜고 다시 열어주세요. 고른 앱 소리만 들어가요. 두 번 누르면 바로 시작해요.</p>
      ${info.mic ? micCheck(pref.mic) : ""}
      <div class="dialog-actions">
        <button type="button" class="btn btn-ghost" data-act="back">이전</button>
        <button type="button" class="btn btn-primary" data-act="start-app">● 녹음 시작</button>
      </div>`;
    const filter = body.querySelector("[name=app-filter]");
    filter?.addEventListener("input", () => {
      const q = filter.value.trim().toLowerCase();
      body.querySelectorAll(".app-card").forEach((c) => { c.hidden = !!q && !c.dataset.name.includes(q); });
    });
    body.querySelector(".app-grid").ondblclick = (e) => {
      if (e.target.closest(".app-card")) body.querySelector("[data-act=start-app]").click();
    };
    (body.querySelector("[name=app]:checked") || body.querySelector("[name=app]")).focus();
  };

  const tabStep = () => {
    body.innerHTML = `
      <h3 class="source-title">크롬 탭 소리 녹음</h3>
      <p class="field-hint">녹음할 탭(온라인 강의·유튜브)을 먼저 열어두세요. 시작하면 크롬이 아래 같은 창을 띄워요.</p>
      <div class="chrome-mock" aria-hidden="true">
        <div class="cm-head">공유할 항목을 고르세요</div>
        <div class="cm-tabs"><span class="on">Chrome 탭<i class="cm-n">1</i></span><span>창</span><span>전체 화면</span></div>
        <div class="cm-list">
          <div class="cm-item on"><span class="cm-fav cm-red">▶</span>강의 영상 - YouTube<i class="cm-n">2</i></div>
          <div class="cm-item"><span class="cm-fav">📄</span>다른 탭</div>
        </div>
        <div class="cm-foot">
          <span class="cm-audio"><span class="cm-switch"></span>탭 오디오도 공유<i class="cm-n">3</i></span>
          <span class="cm-btns"><span class="cm-btn">취소</span><span class="cm-btn cm-primary">공유<i class="cm-n">4</i></span></span>
        </div>
      </div>
      <ol class="source-steps cm-steps">
        <li><b>Chrome 탭</b>을 눌러요.</li>
        <li>녹음할 탭을 골라요.</li>
        <li><b>탭 오디오도 공유</b>가 켜져 있는지 봐요. 꺼져 있으면 소리가 안 들어가요.</li>
        <li><b>공유</b>를 누르면 녹음이 시작돼요. 끝낼 땐 크롬 위쪽의 <b>공유 중지</b>나 ■ 녹음 끝내기.</li>
      </ol>
      ${micCheck(pref.tabMic)}
      <p class="field-hint">마이크를 같이 켤 땐 이어폰을 끼면 소리가 두 번 겹치지 않아요.</p>
      <div class="dialog-actions">
        <button type="button" class="btn btn-ghost" data-act="back">이전</button>
        <button type="button" class="btn btn-primary" data-act="start-tab">공유 창 열기</button>
      </div>`;
  };

  return new Promise((resolve) => {
    const finish = (src) => {
      body.onclick = dlg.oncancel = null;
      dlg.close();
      resolve(src);
    };
    body.onclick = async (e) => {
      const b = e.target.closest("[data-src], [data-act]");
      if (!b || b.disabled) return;
      const mic = !!body.querySelector("[name=mic]")?.checked;
      switch (b.dataset.src || b.dataset.act) {
        case "mic": finish({ kind: "mic" }); break;
        case "app": appStep(); break;
        case "tab": tabStep(); break;
        case "chrome": finish(null); openInChrome(); break;
        case "cancel": finish(null); break;
        case "back": home(); break;
        case "permit":
          try { if ((await nativeCall("capture-permission")).permitted) appStep(); } catch {}
          break;
        case "start-app": {
          const sel = body.querySelector("[name=app]:checked");
          const bundle = sel?.value || "";
          const recent = bundle ? [bundle, ...(pref.recent || []).filter((x) => x !== bundle)].slice(0, 5) : pref.recent;
          saveSourcePref({ bundle, mic, recent });
          finish({ kind: "app", bundle, name: bundle ? sel.dataset.label : "맥 전체", mic });
          break;
        }
        case "start-tab":
          saveSourcePref({ tabMic: mic });
          finish({ kind: "tab", mic });
          break;
      }
    };
    dlg.oncancel = (e) => { e.preventDefault(); finish(null); };  // Esc
    home();
    dlg.showModal();
  });
}

/* source.kind: mic(마이크) · tab(크롬 탭 소리, 브라우저에서) · app(다른 앱 소리, 앱 창에서) */
async function startRecording(source = { kind: "mic" }) {
  if (recorder.phase !== "idle") return;
  if (state.pendingFile) { toast("올리던 파일을 먼저 처리하거나 취소해 주세요.", "error"); return; }
  // 권한 창·서버 응답을 기다리는 사이에 버튼을 또 누르면 녹음이 두 개 생긴다 → 바로 막아 둔다
  recorder.phase = "starting";
  const title = defaultRecordTitle();
  let ok = false;
  try {
    ok = source.kind === "app" ? await startAppCapture(source, title) : await startBrowserCapture(source, title);
  } catch (err) {
    toast(err.message, "error");
  }
  if (!ok) { recorder.phase = "idle"; return; }
  afterRecordingStarted();
}

function afterRecordingStarted() {
  setupLevelMeter(recorder.stream);
  window.addEventListener("beforeunload", warnBeforeUnload);
  if (location.hash.startsWith("#/r/")) location.hash = "#/";
  else renderUploadArea();
  recorder.tick = setInterval(updateRecordingPanel, 250);
  checkPower();
  recorder.powerTimer = setInterval(checkPower, 60_000);
  refresh();
}

function beginRecorder(id, title, source, extra = {}) {
  Object.assign(recorder, {
    phase: "recording", id, title, source, seq: 0, queue: [], offline: false,
    elapsedMs: 0, resumedAt: Date.now(), paused: false,
    marks: [], quietSince: null, quietWarned: false, power: null, powerWarned: false,
    nativeDb: null, nativeSaved: 0, notes: [], noteT: null, ...extra,
  });
}

function createLiveSession(title, container) {
  const fd = new FormData();
  fd.append("title", title);
  fd.append("container", container);
  return api("/api/live", { method: "POST", body: fd });
}

/* 브라우저(MediaRecorder)로 녹음: 마이크, 또는 크롬 탭 소리(+마이크) */
async function startBrowserCapture(source, title) {
  const fmt = pickMime();
  if (!navigator.mediaDevices?.getUserMedia || !fmt) {
    toast(IN_APP
      ? "이 맥에서는 앱 창 녹음을 쓸 수 없어요. 메뉴의 '브라우저에서 열기'로 열어서 녹음해 주세요."
      : "이 브라우저에서는 녹음을 할 수 없어요. 크롬이나 사파리에서 열어주세요.", "error");
    return false;
  }
  const input = source.kind === "tab" ? await openTabAudio(source.mic) : await openMic();
  if (!input) return false;

  let rec;
  try {
    rec = await createLiveSession(title, fmt.container);
  } catch (err) {
    input.release();
    throw err;
  }
  beginRecorder(rec.id, title, source, { stream: input.stream, release: input.release });

  const media = new MediaRecorder(input.stream, { mimeType: fmt.mime, audioBitsPerSecond: 64000 });
  media.ondataavailable = (e) => { if (e.data && e.data.size) enqueueChunk(e.data); };
  media.onerror = () => toast("녹음 중에 문제가 생겼어요. 지금까지 녹음된 부분은 저장돼 있어요.", "error");
  media.start(CHUNK_MS);
  recorder.media = media;

  // 크롬 위쪽의 '공유 중지'를 누르거나 녹음하던 탭을 닫으면 녹음도 끝낸다
  for (const t of input.ends || []) {
    t.addEventListener("ended", () => {
      if (recorder.phase !== "recording" || recorder.id !== rec.id) return;
      toast("탭 공유가 끝나서 녹음을 멈췄어요.");
      stopRecording();
    });
  }
  return true;
}

async function openMic() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: MIC_CONSTRAINTS });
    return { stream, release: () => stream.getTracks().forEach((t) => t.stop()) };
  } catch (err) {
    toast(micErrorMessage(err), "error");
    return null;
  }
}

/* 크롬 탭 소리: 화면 공유 창에서 탭을 고르고 '탭 오디오도 공유'를 켜면 그 소리가 들어온다 */
async function openTabAudio(withMic) {
  if (!navigator.mediaDevices?.getDisplayMedia) {
    toast("이 브라우저에서는 탭 소리를 녹음할 수 없어요. 크롬에서 열어주세요.", "error");
    return null;
  }
  let display;
  try {
    display = await navigator.mediaDevices.getDisplayMedia({
      video: true,  // 소리만 따로 공유할 수는 없어서 화면도 받지만 녹음에는 안 쓴다
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, suppressLocalAudioPlayback: false },
      preferCurrentTab: false,
      selfBrowserSurface: "exclude",
      surfaceSwitching: "include",
      systemAudio: "include",
    });
  } catch (err) {
    if (err?.name !== "NotAllowedError") toast("화면 공유를 시작하지 못했어요.", "error");  // 취소는 조용히
    return null;
  }
  const stopDisplay = () => display.getTracks().forEach((t) => t.stop());
  const tabAudio = display.getAudioTracks()[0];
  if (!tabAudio) {
    stopDisplay();
    toast("소리가 공유되지 않았어요. 공유 창에서 '탭' 을 고르고 아래의 '탭 오디오도 공유'를 켜주세요.", "error");
    return null;
  }
  if (!withMic) return { stream: new MediaStream([tabAudio]), release: stopDisplay, ends: display.getTracks() };

  let mic;
  try {
    // 내 목소리는 가까이서 말하니 잡음·울림 제거를 켠다
    mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (err) {
    stopDisplay();
    toast(micErrorMessage(err), "error");
    return null;
  }
  const ctx = new AudioContext();
  ctx.resume().catch(() => {});
  const dest = ctx.createMediaStreamDestination();
  ctx.createMediaStreamSource(new MediaStream([tabAudio])).connect(dest);
  ctx.createMediaStreamSource(mic).connect(dest);
  return {
    stream: dest.stream,
    ends: display.getTracks(),
    release: () => {
      stopDisplay();
      mic.getTracks().forEach((t) => t.stop());
      ctx.close().catch(() => {});
    },
  };
}

/* 다른 앱 소리: 앱 창(실행기)이 직접 받아서 서버로 보낸다. 화면은 상태만 보여준다 */
async function startAppCapture(source, title) {
  const rec = await createLiveSession(title, "pcm");
  let res;
  try {
    res = await nativeCall("capture-start", { id: rec.id, bundle: source.bundle || "", name: source.name, mic: !!source.mic });
  } catch (err) {
    res = { ok: false, message: err.message };
  }
  if (!res?.ok) {
    api(`/api/recordings/${rec.id}`, { method: "DELETE" }).catch(() => {});
    toast(res?.message || "앱 소리를 녹음하지 못했어요.", "error");
    return false;
  }
  beginRecorder(rec.id, title, source);
  return true;
}

// 앱(실행기)이 보내는 녹음 상태: 소리 크기·저장된 시간 / 맥 사정으로 멈춤
window.kkachiNative = (e) => {
  if (e.type === "capture-level") {
    recorder.nativeDb = e.db;
    recorder.nativeSaved = e.saved;
    recorder.offline = e.offline;
  } else if (e.type === "capture-ended" && recorder.phase === "recording" && recorder.source?.kind === "app") {
    toast(e.message, "error");
    stopRecording();
  }
};

// 화면을 새로고침해도 앱은 계속 녹음 중일 수 있다 → 녹음 화면을 되살린다
async function restoreAppCapture() {
  if (!NATIVE_CAPTURE) return;
  let s;
  try { s = await nativeCall("capture-status"); } catch { return; }
  if (!s || recorder.phase !== "idle") return;
  let title = "";
  try { title = (await api(`/api/recordings/${s.id}`)).title; } catch {}
  beginRecorder(s.id, title, { kind: "app", name: s.app, mic: s.mic },
    { elapsedMs: s.elapsed, paused: s.paused, resumedAt: Date.now() });
  afterRecordingStarted();
}

function recNotesHTML() {
  return (recorder.notes || []).slice(-3).map((n) => `<li><span>${fmtClock(n.t)}</span> ${esc(n.text)}</li>`).join("");
}

async function addRecordingNote() {
  const input = $("#rec-note");
  const text = input.value.trim();
  if (!text || recorder.phase !== "recording") return;
  const t = recorder.noteT ?? Math.max(0, elapsed() / 1000 - 1);
  input.value = "";
  recorder.noteT = null;
  recorder.notes.push({ t, text });
  $("#rec-notes").innerHTML = recNotesHTML();
  try {
    await api(`/api/recordings/${recorder.id}/notes`, jsonOpts("POST", { t, text }));
  } catch {
    toast("메모를 저장하지 못했어요. 다시 적어주세요.", "error");
  }
}

/* 중요 표시: 녹음 중 스페이스바나 ⭐ 버튼. 녹음 시간(일시정지 뺀 시간) 기준으로 저장 */
async function markImportant() {
  if (recorder.phase !== "recording" || recorder.paused) return;
  const t = Math.max(0, elapsed() / 1000 - 1);  // 누르는 데 걸린 시간만큼 살짝 앞으로
  recorder.marks.push(t);
  const btn = $("#btn-mark");
  if (btn) {
    btn.querySelector(".mark-count").textContent = recorder.marks.length;
    btn.classList.remove("pop");
    void btn.offsetWidth;  // 애니메이션 다시 시작
    btn.classList.add("pop");
  }
  toast(`⭐ ${fmtClock(t)} 중요 표시했어요`);
  try { await api(`/api/recordings/${recorder.id}/marks`, jsonOpts("POST", { t })); } catch {}
}

async function checkPower() {
  try { recorder.power = await api("/api/app/power"); } catch { return; }
  const p = recorder.power;
  if (!p.on_ac && p.percent !== null && p.percent <= 15 && !recorder.powerWarned) {
    recorder.powerWarned = true;
    nativeNotify("배터리가 얼마 안 남았어요 🔋", `${p.percent}% 남았어요. 녹음이 멈추지 않게 충전기를 꽂아주세요.`);
  }
}

/* 무음 감지: 30초 넘게 소리가 거의 없으면 경고 (창이 숨겨져도 동작하도록 타이머에서 잰다) */
function sampleQuiet() {
  const db = recorder.paused || recorder.phase !== "recording" ? null : currentDb();
  if (db == null) { recorder.quietSince = null; return; }
  if (db > -55) { recorder.quietSince = null; recorder.quietWarned = false; return; }
  recorder.quietSince ??= Date.now();
  if (Date.now() - recorder.quietSince > 30_000 && !recorder.quietWarned) {
    recorder.quietWarned = true;
    nativeNotify("녹음에 소리가 안 들려요 🔇", quietMessage());
  }
}

function quietMessage() {
  const kind = recorder.source?.kind;
  if (kind === "app") return `30초째 소리가 거의 없어요. ${recorder.source.name || "녹음하는 앱"}에서 소리가 나고 있는지 확인해 주세요.`;
  if (kind === "tab") return "30초째 소리가 거의 없어요. 녹음하는 탭에서 소리가 나고 있는지 확인해 주세요.";
  return "30초째 소리가 거의 없어요. 마이크가 가려지거나 꺼지지 않았는지 확인해 주세요.";
}

// 지금 소리 크기(dB). 앱 소리 녹음은 실행기가 알려주고, 나머지는 브라우저에서 잰다
function currentDb() {
  if (recorder.source?.kind === "app") return recorder.nativeDb;
  if (!recorder.analyser) return null;
  const buf = new Float32Array(1024);
  recorder.analyser.getFloatTimeDomainData(buf);
  let sum = 0;
  for (const v of buf) sum += v * v;
  return 20 * Math.log10(Math.sqrt(sum / buf.length) + 1e-8);
}

function warnBeforeUnload(e) {
  e.preventDefault();
  e.returnValue = "";  // 브라우저 기본 경고창: "사이트에서 나가시겠습니까?"
}

function setupLevelMeter(stream) {
  if (stream) {
    try {
      recorder.audioCtx = new AudioContext();
      const src = recorder.audioCtx.createMediaStreamSource(stream);
      recorder.analyser = recorder.audioCtx.createAnalyser();
      recorder.analyser.fftSize = 1024;
      src.connect(recorder.analyser);
    } catch {
      recorder.analyser = null;
    }
  }
  const draw = () => {
    const meter = $("#level-meter > div");
    const db = currentDb();
    if (meter && db != null) {
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
  if (recorder.phase !== "recording") return;
  const app = recorder.source?.kind === "app";
  const m = recorder.media;
  if (!app && !m) return;
  if (recorder.paused) {
    if (app) nativeCall("capture-resume").catch(() => {});
    else m.resume();
    recorder.paused = false;
    recorder.resumedAt = Date.now();
  } else {
    if (app) nativeCall("capture-pause").catch(() => {});
    else m.pause();
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

  if (recorder.source?.kind === "app") {
    // 앱이 남은 소리를 서버에 다 보낸 뒤에 답한다
    try { await nativeCall("capture-stop"); } catch {}
  } else {
    await new Promise((resolve) => {
      recorder.media.addEventListener("stop", resolve, { once: true });
      recorder.media.stop();  // 마지막 조각이 ondataavailable 로 한 번 더 온다
    });
  }
  cleanupMedia();
  // 남은 조각을 다 보낼 때까지 기다린다
  while (recorder.queue.length || recorder.sending) await new Promise((r) => setTimeout(r, 200));

  recorder.phase = "finished";
  renderUploadArea();
}

function cleanupMedia() {
  clearInterval(recorder.tick);
  clearInterval(recorder.powerTimer);
  cancelAnimationFrame(recorder.raf);
  recorder.stream?.getTracks().forEach((t) => t.stop());
  recorder.release?.();
  recorder.audioCtx?.close().catch(() => {});
  Object.assign(recorder, { stream: null, release: null, media: null, audioCtx: null, analyser: null });
}

function resetRecorder() {
  cleanupMedia();
  window.removeEventListener("beforeunload", warnBeforeUnload);
  Object.assign(recorder, { phase: "idle", id: null, source: null, queue: [], seq: 0, elapsedMs: 0, paused: false });
  renderUploadArea();
}

function sourceLabel(src) {
  if (src?.kind === "app") return `🖥️ ${src.name || "맥 전체"} 소리${src.mic ? " + 내 목소리" : ""}`;
  if (src?.kind === "tab") return `🌐 크롬 탭 소리${src.mic ? " + 내 목소리" : ""}`;
  return "🎙️ 마이크";
}

function recordingTips() {
  const kind = recorder.source?.kind;
  const lid = "맥북 뚜껑을 닫으면 녹음이 멈춰요. 긴 강의는 충전기를 연결해 두세요.";
  if (kind === "tab") return [
    "크롬 위쪽의 '공유 중지'를 누르거나 녹음하는 탭을 닫으면 녹음이 끝나요.",
    "이 까치녹음기 탭은 닫지 마세요. 닫으면 그때까지만 저장돼요.",
    lid,
  ];
  if (kind === "app") return [
    "이어폰을 써도 녹음돼요. 녹음하는 앱을 끄면 소리가 더 안 들어와요.",
    "창을 닫아도 녹음은 계속돼요. 끝낼 땐 Dock 의 까치녹음기를 눌러 창을 다시 여세요.",
    lid,
  ];
  return [lid, IN_APP ? "창을 닫아도 녹음은 계속돼요. 끝낼 땐 Dock 의 까치녹음기를 눌러 창을 다시 여세요." : "이 창을 닫아도 그때까지 녹음된 부분은 저장돼요."];
}

function renderRecordingPanel(area) {
  const stopping = recorder.phase === "stopping";
  area.innerHTML = `
    <section class="card rec-panel" aria-label="녹음 중">
      <div class="rec-panel-top">
        <span class="rec-live ${recorder.paused ? "paused" : ""}" id="rec-live">${stopping ? "저장 중" : recorder.paused ? "일시정지" : "녹음 중"}</span>
        <span class="rec-saved" id="rec-saved"></span>
      </div>
      <div class="rec-source">${esc(sourceLabel(recorder.source))}</div>
      <div class="rec-timer" id="rec-timer">${fmtClock(elapsed() / 1000)}</div>
      <div class="level" id="level-meter" aria-hidden="true"><div></div></div>
      <div class="rec-warn" id="rec-warn" hidden></div>
      <button class="btn btn-star" id="btn-mark" ${stopping ? "disabled" : ""} title="교수님이 중요하다고 할 때 눌러두면 결과에서 ⭐로 찾을 수 있어요">
        ⭐ 중요! <span class="mark-count">${recorder.marks?.length || 0}</span><small>스페이스바</small>
      </button>
      <form class="rec-note" id="rec-note-form">
        <input class="input" id="rec-note" maxlength="500" autocomplete="off" ${stopping ? "disabled" : ""}
          placeholder="📝 메모 적고 Enter (예: 시험에 나온대요)">
      </form>
      <ul class="rec-notes" id="rec-notes">${recNotesHTML()}</ul>
      <div class="rec-actions">
        <button class="btn btn-ghost btn-lg" id="btn-pause" ${stopping ? "disabled" : ""}>${recorder.paused ? "▶ 계속" : "❚❚ 일시정지"}</button>
        <button class="btn btn-danger btn-lg" id="btn-stop" ${stopping ? "disabled" : ""}>■ 녹음 끝내기</button>
      </div>
      <ul class="rec-tips">${recordingTips().map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
    </section>`;
  $("#btn-pause").addEventListener("click", togglePause);
  $("#btn-stop").addEventListener("click", stopRecording);
  $("#btn-mark").addEventListener("click", markImportant);
  // 메모 시간은 '적기 시작한 때'로 (다 적고 Enter 누를 땐 이미 지나갔으니)
  $("#rec-note").addEventListener("input", (e) => {
    if (e.target.value.trim()) recorder.noteT ??= Math.max(0, elapsed() / 1000 - 1);
    else recorder.noteT = null;
  });
  $("#rec-note-form").addEventListener("submit", (e) => { e.preventDefault(); addRecordingNote(); });
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
  const savedSec = recorder.source?.kind === "app"
    ? recorder.nativeSaved
    : (recorder.seq - recorder.queue.length) * CHUNK_MS / 1000;
  saved.textContent = recorder.offline
    ? "⚠ 앱과 연결이 끊겨서 다시 시도 중이에요"
    : savedSec ? `${fmtClock(savedSec)}까지 안전하게 저장됨` : "";
  saved.classList.toggle("warn", recorder.offline);

  sampleQuiet();
  const warns = [];
  const p = recorder.power;
  if (p && !p.on_ac && p.percent !== null) {
    warns.push(`🔌 충전기가 연결돼 있지 않아요 (배터리 ${p.percent}%). 긴 강의는 충전기를 꽂아주세요.`);
  }
  if (recorder.quietSince && Date.now() - recorder.quietSince > 30_000) {
    warns.push(`🔇 ${quietMessage()}`);
  }
  const w = $("#rec-warn");
  if (w) {
    w.hidden = !warns.length;
    w.innerHTML = warns.map((x) => `<div>${esc(x)}</div>`).join("");
  }
}

// 녹음 중 스페이스바 = 중요 표시 (글자 입력 중엔 제외)
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || recorder.phase !== "recording") return;
  if (e.target.closest("input, textarea, select, [contenteditable]")) return;
  e.preventDefault();
  markImportant();
});

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
  if (form.subject.value) fillSubjectTerms(form);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#submit-finish");
    btn.disabled = true;
    btn.textContent = "저장하는 중…";
    const fd = new FormData();
    appendOptions(fd, form);
    try {
      await api(`/api/live/${recorder.id}/finish`, { method: "POST", body: fd });
      if (form._slides) attachSlides(recorder.id, form._slides);
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
  const vLabel = v?.commit
    ? `버전 ${v.release ? `${v.release} (${v.commit})` : v.commit} · ${new Date(v.date).toLocaleDateString("ko-KR", { month: "long", day: "numeric" })}`
    : "버전 정보 없음";
  openMenu(anchor, [
    { label: "할 일 알림", icon: "📋", run: remindersDialog },
    { label: "업데이트 확인", icon: "↻", run: checkUpdate },
    { label: "새로 바뀐 점", icon: "✨", run: showAllNotes },
    { label: "사용법 보기", icon: "?", run: () => showGuide(0) },
    { label: "아이폰 음성 메모 보내기", icon: "📱", run: () => phoneDialog() },
    ...(IN_APP ? [{ label: "크롬에서 열기", icon: "🌐", run: openInChrome }] : []),
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
  // 업데이트 노트가 있으면 쉬운 설명을, 없으면 커밋 제목을 보여준다
  const items = (r.notes || []).flatMap((n) => n.items);
  const list = items.length
    ? items.map((it) => `<li class="note-li"><span>${it.icon || "✨"}</span><span><b>${esc(it.title)}</b><br><small>${it.text}</small></span></li>`).join("")
    : r.changes.map((c) => `<li>${esc(c)}</li>`).join("");
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
  showOverlay("🐦‍⬛", "까치녹음기를 껐어요", (IN_APP ? "" : "이 탭은 닫아도 돼요.<br>") + "다시 쓰려면 Dock의 <b>까치녹음기</b>를 누르세요.");
}

const GUIDE = [
  { icon: "🎙️", title: "강의 녹음하기",
    text: "강의가 시작되면 위의 <b>● 녹음하기</b>를 누르세요.<br>Zoom·유튜브 같은 <b>다른 앱 소리</b>도 녹음할 수 있어요.<br>10초마다 저장돼서 중간에 창이 꺼져도 괜찮아요.<br><b>맥북 뚜껑은 닫지 마세요.</b> 녹음이 멈춰요." },
  { icon: "📁", title: "녹음 파일 올리기",
    text: "휴대폰으로 녹음한 파일도 화면에 <b>끌어다 놓으면</b> 돼요.<br>받아쓰기는 인터넷 없이 이 맥에서 해요.<br>1시간 강의에 5~10분쯤 걸려요." },
  { icon: "✏️", title: "고치고 찾기",
    text: "<b>화자 1</b>을 눌러 '교수님'처럼 이름을 바꿀 수 있어요.<br>틀린 문장은 <b>두 번 눌러</b> 고치고,<br>위 검색창으로 모든 강의에서 찾을 수 있어요." },
];
let guideStep = 0;
let guidePages = GUIDE;
let guideDone = { label: "시작하기", onClose: markGuideSeen };

function markGuideSeen() {
  try { localStorage.setItem("kkachi.guide.v1", "seen"); } catch {}
}

function showGuide(step = 0) {
  showPages(GUIDE, { label: "시작하기", onClose: markGuideSeen }, step);
}

/* 넘겨 보는 안내 창 (사용법 / 새로 바뀐 점). 마지막 장의 버튼 글자와 닫힐 때 할 일을 정한다 */
function showPages(pages, done, step = 0) {
  guidePages = pages;
  guideDone = done;
  guideStep = step;
  renderGuide();
  const dlg = $("#guide-dialog");
  if (!dlg.open) dlg.showModal();
}

function renderGuide() {
  const g = guidePages[guideStep];
  $("#guide-steps").innerHTML = `${g.kicker ? `<div class="guide-kicker">${esc(g.kicker)}</div>` : ""}`
    + `<div class="guide-icon">${g.icon}</div><h2>${g.title}</h2><p>${g.text}</p>`;
  $("#guide-dots").innerHTML = guidePages.length > 1
    ? guidePages.map((_, i) => `<span class="${i === guideStep ? "on" : ""}"></span>`).join("") : "";
  $("#guide-prev").style.visibility = guideStep ? "visible" : "hidden";
  $("#guide-next").textContent = guideStep === guidePages.length - 1 ? guideDone.label : "다음";
}

$("#guide-prev").addEventListener("click", () => { guideStep = Math.max(0, guideStep - 1); renderGuide(); });
$("#guide-next").addEventListener("click", () => {
  if (guideStep < guidePages.length - 1) { guideStep++; renderGuide(); return; }
  $("#guide-dialog").close();
  guideDone.onClose?.();
});
$("#guide-dialog").addEventListener("cancel", () => guideDone.onClose?.());

function maybeShowGuide() {
  let seen = false;
  try { seen = localStorage.getItem("kkachi.guide.v1") === "seen"; } catch {}
  if (!seen) { showGuide(0); return true; }
  return false;
}

/* 업데이트 노트: 노트(날짜별 묶음) → 넘겨 보는 장들 */
function notePages(notes) {
  return notes.flatMap((n) => n.items.map((it) => ({
    kicker: `새로 바뀐 점 · ${n.version ? `버전 ${n.version}` : new Date(n.date).toLocaleDateString("ko-KR", { month: "long", day: "numeric" })}`,
    icon: it.icon || "✨", title: esc(it.title), text: it.text,
  })));
}

// 업데이트 뒤 처음 켜면 새로 바뀐 점을 한 번 보여준다
async function maybeShowWhatsNew() {
  let r;
  try { r = await api("/api/app/whatsnew"); } catch { return; }
  const unseen = r.notes.filter((n) => r.unseen.includes(n.id));
  const pages = notePages(unseen);
  if (!pages.length) return;
  showPages(pages, { label: "확인", onClose: () => api("/api/app/whatsnew/seen", { method: "POST" }).catch(() => {}) });
}

async function showAllNotes() {
  let r;
  try { r = await api("/api/app/whatsnew"); } catch (err) { toast(err.message, "error"); return; }
  const pages = notePages(r.notes.slice(0, 3));
  if (!pages.length) { toast("아직 업데이트 노트가 없어요."); return; }
  showPages(pages, { label: "닫기" });
}

$("#btn-settings").addEventListener("click", (e) => { e.stopPropagation(); settingsMenu(e.currentTarget); });

/* ---------- 상태 갱신 ---------- */

async function refresh() {
  clearTimeout(state.pollTimer);
  if (appState.closed) return;
  try {
    state.recordings = await api("/api/recordings");
    updateBrandMark();
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

$("#btn-upload").addEventListener("click", chooseUploadSource);
$("#btn-record").addEventListener("click", async () => {
  if (recorder.phase === "idle") {
    const source = await chooseRecordSource();
    if (source) startRecording(source);
  }
  else if (location.hash.startsWith("#/r/")) location.hash = "#/";
});
$("#file-input").addEventListener("change", (e) => acceptFiles(e.target.files));

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
  acceptFiles(e.dataTransfer.files);
});

loadSubjects();
(async () => {
  try { state.gift = await api("/api/app/gift"); } catch {}
  applyGiftBranding();
  route();
  if (!maybeShowGuide()) maybeShowWhatsNew();
})();

restoreAppCapture();

// 이 화면에서 녹음이 되는지 서버 로그에 남긴다 (문제 신고 때 확인용)
fetch("/api/app/client", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    inApp: IN_APP,
    mediaRecorder: typeof MediaRecorder !== "undefined",
    mime: pickMime()?.mime || null,
    getUserMedia: !!navigator.mediaDevices?.getUserMedia,
    nativeCapture: NATIVE_CAPTURE,
    tabAudio: CAN_TAB,
    ua: navigator.userAgent,
  }),
}).catch(() => {});
