// 까치 3D: 뼈대 없이 나온 AI 모델에 코드로 뼈대를 심고 움직인다.
//
// 1) normalize: 키 1, 바닥 y=0, 가운데 x=z=0, 앞(+Z)을 보게 맞춘 뒤 변환을 모양에 굳힌다
// 2) buildRig: 높이·위치로 머리·꼬리·날개·발 자리를 나눠 뼈마다 '얼마나 따라 움직일지'(가중치)를 준다
// 3) Animator: 숨쉬기·두리번·꼬리 까딱·날개 파닥·콕콕 쪼기·통통 뛰기를 겹쳐서 움직인다
//
// 모델마다 다른 값(목 높이, 날개 범위 등)은 cfg 로 넘긴다. 날개를 편 모델이 오면 cfg.wing 만 바꾸면 된다.
import * as THREE from "three";

export function normalize(root, rotY = 0) {
  root.rotation.y = rotY;
  root.updateMatrixWorld(true);
  const meshes = [];
  root.traverse((o) => { if (o.isMesh) meshes.push(o); });
  const box = new THREE.Box3();
  for (const m of meshes) box.expandByObject(m);
  const size = box.getSize(new THREE.Vector3());
  const s = 1 / size.y;
  const c = box.getCenter(new THREE.Vector3());
  const fix = new THREE.Matrix4().makeScale(s, s, s)
    .multiply(new THREE.Matrix4().makeTranslation(-c.x, -box.min.y, -c.z));
  return meshes.map((m) => {
    const g = m.geometry.clone();
    g.applyMatrix4(new THREE.Matrix4().multiplyMatrices(fix, m.matrixWorld));
    g.computeBoundingBox();
    return { geometry: g, material: m.material };
  });
}

const smooth = THREE.MathUtils.smoothstep;

// 다리 줄이기: feetTop~legTop 사이(다리)만 k 배로 눌러 짧게 하고, 그 위는 줄어든 만큼 내린다.
// 발(feetTop 아래)은 그대로. 바뀐 높이를 알려주는 mapY 를 돌려준다 (뼈 자리값도 같이 옮기려고)
// 한 곳만 펴기: center 에서 radius 안의 점을 이웃 평균 쪽으로 여러 번 당긴다 (가운데일수록 세게)
// 예: AI 모델 얼굴에 남은 접힌 자국
export function smoothArea(geometry, center, radius, iters = 30, keep = null) {   // keep(점) → true 면 그대로 둠
  const pos = geometry.attributes.position, idx = geometry.index, n = pos.count;
  const nb = Array.from({ length: n }, () => new Set());
  for (let t = 0; t < idx.count; t += 3) {
    const a = idx.getX(t), b = idx.getX(t + 1), c = idx.getX(t + 2);
    nb[a].add(b).add(c); nb[b].add(a).add(c); nb[c].add(a).add(b);
  }
  const v = new THREE.Vector3(), w = new Float32Array(n), inside = [];
  for (let i = 0; i < n; i++) {
    v.fromBufferAttribute(pos, i);
    const d = v.distanceTo(center) / radius;
    if (d < 1 && !(keep && keep(v))) { w[i] = 0.5 * (1 + Math.cos(Math.PI * d)); inside.push(i); }
  }
  const P = new Float32Array(pos.array);
  for (let it = 0; it < iters; it++) {
    const Q = new Float32Array(P);
    for (const i of inside) {
      let x = 0, y = 0, z = 0;
      for (const k of nb[i]) { x += P[k * 3]; y += P[k * 3 + 1]; z += P[k * 3 + 2]; }
      const m = nb[i].size, f = 0.5 * w[i];
      Q[i * 3] += f * (x / m - P[i * 3]); Q[i * 3 + 1] += f * (y / m - P[i * 3 + 1]); Q[i * 3 + 2] += f * (z / m - P[i * 3 + 2]);
    }
    P.set(Q);
  }
  pos.array.set(P);
  pos.needsUpdate = true;
  geometry.computeVertexNormals();
}

// 조건에 맞는 점이 들어간 면을 지운다 (예: 새 꼬리를 달고 남은 원래 꼬리 조각)
export function removeArea(geometry, test) {
  const pos = geometry.attributes.position, idx = geometry.index, v = new THREE.Vector3();
  const drop = new Uint8Array(pos.count);
  for (let i = 0; i < pos.count; i++) drop[i] = test(v.fromBufferAttribute(pos, i)) ? 1 : 0;
  const keep = [];
  for (let t = 0; t < idx.count; t += 3) {
    const a = idx.getX(t), b = idx.getX(t + 1), c = idx.getX(t + 2);
    if (!(drop[a] || drop[b] || drop[c])) keep.push(a, b, c);
  }
  geometry.setIndex(keep);
  return idx.count / 3 - keep.length / 3;
}

export function shortenLegs(geometry, { feetTop, legTop, k }) {
  const drop = (legTop - feetTop) * (1 - k);
  const mapY = (y) => (y <= feetTop ? y : y <= legTop ? feetTop + (y - feetTop) * k : y - drop);
  const pos = geometry.attributes.position;
  for (let i = 0; i < pos.count; i++) pos.setY(i, mapY(pos.getY(i)));
  pos.needsUpdate = true;
  geometry.computeVertexNormals();
  geometry.computeBoundingBox();
  geometry.computeBoundingSphere();
  return mapY;
}

// 뼈 이름 순서 = skinIndex 번호
const BONES = ["root", "body", "head", "tail", "wingL", "wingR", "footL", "footR", "jaw", "wingLowL", "wingLowR"];

export function buildRig(geometry, material, cfg) {
  const bb = geometry.boundingBox;
  const hw = Math.max(-bb.min.x, bb.max.x);          // 가로 절반
  const zBack = bb.min.z, zFront = bb.max.z, depth = zFront - zBack;

  // 부리: 머리 높이에서 가장 앞으로 튀어나온 점이 부리 끝
  // (부리를 잘라내고 따로 붙이는 모델은 잘라내기 전에 잰 부리 끝을 cfg.tip 으로 넘긴다)
  const pos0 = geometry.attributes.position, p0 = new THREE.Vector3(), tip = new THREE.Vector3(0, 0, -Infinity);
  if (cfg.tip) tip.copy(cfg.tip);
  else for (let i = 0; i < pos0.count; i++) {
    p0.fromBufferAttribute(pos0, i);
    if (p0.y > cfg.headStart && Math.abs(p0.x) < 0.15 && p0.z > tip.z) tip.copy(p0);
  }
  const beakLen = cfg.beakLen ?? 0.12;
  const at = {
    root: new THREE.Vector3(0, 0, 0),
    body: new THREE.Vector3(0, 0.3, 0),
    head: new THREE.Vector3(0, cfg.neckY, cfg.neckZ ?? 0),
    tail: new THREE.Vector3(0, cfg.tailY, zBack + depth * cfg.tailBase),
    wingL: new THREE.Vector3(hw * cfg.wing.pivotX, cfg.wing.pivotY, cfg.wing.pivotZ ?? 0),
    wingR: new THREE.Vector3(-hw * cfg.wing.pivotX, cfg.wing.pivotY, cfg.wing.pivotZ ?? 0),
    footL: new THREE.Vector3(hw * 0.25, cfg.feetY, 0),
    footR: new THREE.Vector3(-hw * 0.25, cfg.feetY, 0),
    jaw: new THREE.Vector3(0, tip.y, tip.z - beakLen),   // 아랫부리가 도는 곳 (부리 뿌리)
    // 날개 아랫부분: 날개 길이 방향으로 난 접는 선(foldY)을 따라 꺾여서 옆구리를 감싼다
    wingLowL: new THREE.Vector3(hw * cfg.wing.pivotX, cfg.wing.foldY ?? cfg.wing.pivotY, cfg.wing.pivotZ ?? 0),
    wingLowR: new THREE.Vector3(-hw * cfg.wing.pivotX, cfg.wing.foldY ?? cfg.wing.pivotY, cfg.wing.pivotZ ?? 0),
  };

  // 뼈: root ─ body ─ (head, tail, wingL, wingR) / root ─ (footL, footR)
  const bones = {};
  for (const name of BONES) { bones[name] = new THREE.Bone(); bones[name].name = name; }
  const parent = { body: "root", head: "body", tail: "body", wingL: "body", wingR: "body", footL: "root", footR: "root", jaw: "head",
                   wingLowL: "wingL", wingLowR: "wingR" };
  bones.root.position.copy(at.root);
  for (const name of BONES.slice(1)) {
    bones[parent[name]].add(bones[name]);
    bones[name].position.copy(at[name]).sub(at[parent[name]]);
  }

  // 가중치: 자리마다 0~1, 나머지는 몸통
  const pos = geometry.attributes.position, v = new THREE.Vector3();
  const skinIndex = new Uint16Array(pos.count * 4), skinWeight = new Float32Array(pos.count * 4);
  const w = new Array(BONES.length);
  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i);
    w.fill(0);
    // 머리: 정해진 높이 위 + 몸통 폭 안쪽만 (날개 끝이 머리를 따라가지 않게)
    const headX = cfg.headX ?? 2;
    const head = smooth(v.y, cfg.headStart, cfg.headFull) * (1 - smooth(Math.abs(v.x) / hw, headX, headX + 0.08));
    const tailZ = 1 - smooth((v.z - zBack) / depth, cfg.tailBase - 0.08, cfg.tailBase + 0.08);
    const tail = tailZ * (1 - smooth(v.y, cfg.tailY + 0.12, cfg.tailY + 0.25)) * (1 - head);
    const W = cfg.wing;
    const inWingH = smooth(v.y, W.yMin - 0.05, W.yMin + 0.03) * (1 - smooth(v.y, W.yMax - 0.03, W.yMax + 0.05));
    const inWingZ = 1 - smooth(v.z, W.zMax - 0.05, W.zMax + 0.05);
    const side = (x) => smooth(x / hw, W.x0, W.x1) * inWingH * inWingZ * (1 - head) * (1 - tail);
    const wingL = side(v.x), wingR = side(-v.x);
    const foot = 1 - smooth(v.y, cfg.feetY - 0.02, cfg.feetY + 0.03);
    // 날개를 접는 선 아래는 아랫부분 뼈로
    const fold = cfg.wing.foldY;
    const low = fold === undefined ? 0 : 1 - smooth(v.y, fold - 0.035, fold + 0.035);
    w[2] = head; w[3] = tail; w[4] = wingL * (1 - low); w[5] = wingR * (1 - low);
    w[9] = wingL * low; w[10] = wingR * low;
    w[6] = v.x > 0 ? foot : 0; w[7] = v.x <= 0 ? foot : 0;
    // 아랫부리: 부리 범위 안에서 부리 가운데 선보다 아래
    const inBeak = smooth(v.z, tip.z - beakLen, tip.z - beakLen * 0.6) * (1 - smooth(Math.abs(v.x), 0.05, 0.09))
      * (1 - smooth(Math.abs(v.y - tip.y), 0.05, 0.08));
    const lower = 1 - smooth(v.y, tip.y - 0.01, tip.y + 0.008);
    w[8] = inBeak * lower * head;
    w[2] = head * (1 - w[8]);
    const used = w.reduce((a, b) => a + b, 0);
    w[1] = Math.max(0, 1 - used);
    // 가장 큰 4개만
    const order = w.map((x, k) => [x, k]).sort((a, b) => b[0] - a[0]).slice(0, 4);
    const total = order.reduce((a, b) => a + b[0], 0) || 1;
    order.forEach(([x, k], slot) => { skinIndex[i * 4 + slot] = k; skinWeight[i * 4 + slot] = x / total; });
  }
  geometry.setAttribute("skinIndex", new THREE.Uint16BufferAttribute(skinIndex, 4));
  geometry.setAttribute("skinWeight", new THREE.Float32BufferAttribute(skinWeight, 4));

  const mesh = new THREE.SkinnedMesh(geometry, material);
  mesh.add(bones.root);
  mesh.bind(new THREE.Skeleton(BONES.map((n) => bones[n])));
  for (const b of Object.values(bones)) b.userData.rest = b.quaternion.clone();
  return { mesh, bones, at, tip };
}

// ---- 움직임 ----
const e = new THREE.Euler(), q = new THREE.Quaternion();
function turn(bone, x = 0, y = 0, z = 0, order = "YXZ") {
  e.set(x, y, z, order);
  bone.quaternion.copy(bone.userData.rest).multiply(q.setFromEuler(e));
}
const ease = (k) => (k <= 0 ? 0 : k >= 1 ? 1 : Math.sin(k * Math.PI));   // 0→1→0

export class Animator {
  constructor(rig, cfg = {}) {
    this.b = rig.bones;
    // 날개 편 모델이면 wingOpen(아래로 내리기) + wingSweep(뒤로 젖히기)으로 접힌 자세를 만든다
    // + wingRoll(날개를 긴 축으로 비틀어 깃털 면이 위를 보게 → 등 위에 얹히게)
    // + wingTuck(날개 아랫부분을 접는 선에서 꺾어 옆구리에 붙이기)
    // + wingScale(날개 크기), stubScale(원래 꼬리 덩어리를 줄여 몸 안으로 — 새 꼬리를 따로 붙일 때)
    this.cfg = { wingFlap: 0.35, wingOpen: 0, wingSweep: 0, wingRoll: 0, wingTuck: 0, wingScale: 1, stubScale: 1, eatBody: 1.1, eatHead: 0.45, ...cfg };
    this.t = 0;
    this.look = { yaw: 0, tilt: 0, target: 0, targetTilt: 0, next: 2 };
    this.tailNext = 3;
    this.acts = [];   // { name, t0, dur }
    // 걷기: 바닥 위 자리(x, z)와 바라보는 방향. 발 제자리는 걸음마다 여기서 앞뒤로 움직인다
    this.pos = { x: 0, z: 0, yaw: 0, step: 0, gait: 0 };
    this.walk = null;
    this.strollNext = 5;
    this.restYaw = 0;
    this.pet = { on: false, amt: 0, side: 1 };
    this.sit = { on: false, amt: 0, until: 0, done: null };   // 알 품기: 알 위에 폭 앉기   // 쓰다듬기: 누르는 동안 on, amt 는 0→1 로 부드럽게   // 산책을 마치고 바라볼 방향 (대개 앞, 가끔 옆)
    this.footRest = { L: this.b.footL.position.clone(), R: this.b.footR.position.clone() };
  }

  // 먹이(x, z)로 걸어가서 먹기. reach: 숙였을 때 부리 끝이 몸 가운데에서 앞으로 닿는 거리
  goEat(x, z, reach, onEat) {
    const dx = x - this.pos.x, dz = z - this.pos.z, d = Math.hypot(dx, dz);
    const stop = Math.max(0, d - reach);
    const sx = this.pos.x + (dx / d) * stop, sz = this.pos.z + (dz / d) * stop;
    this.strollNext = this.t + 8;
    this.restYaw = Math.atan2(dx, dz);   // 먹는 동안 먹이를 본다
    this.walk = { route: [{ x: sx, z: sz, pause: 2.4, onArrive: () => { this.play("eat"); onEat?.(); } }],
      speed: 0.34, hop: Math.random() < 0.5, wait: 0 };
    if (stop < 0.01) { this.walk = null; this.play("eat"); onEat?.(); }   // 이미 코앞이면 돌아서서 바로
  }

  // 알(x, z) 위로 가서 dur 초 동안 폭 앉아 품기 → 다 품으면 onDone
  sitOn(x, z, dur, onDone) {
    const startSit = () => { this.sit.on = true; this.sit.until = this.t + dur; this.sit.done = onDone; };
    this.strollNext = this.t + dur + 6;
    this.restYaw = 0;
    if (Math.hypot(x - this.pos.x, z - this.pos.z) < 0.02) { this.walk = null; startSit(); return; }
    this.walk = { route: [{ x, z, pause: dur, onArrive: startSit }], speed: 0.3, hop: false, wait: 0 };
  }

  walkTo(x, z) { this.walk = { route: [{ x, z, pause: 0 }], speed: 0.3, hop: false, wait: 0 }; }

  // 산책: 매번 다르게 — 들르는 곳 1~3군데, 걷는 빠르기, 가끔 콩콩 뛰어가기,
  // 멈출 때마다 잠깐 쉬거나 바닥을 콕콕 쪼거나 두리번. 바닥 타원(가로 0.55 · 세로 0.4) 밖으로는 안 나간다
  stroll() {
    const R = Math.random, route = [];
    let { x, z } = this.pos;
    const legs = 1 + Math.floor(R() * 3);
    for (let i = 0; i < legs; i++) {
      const a = R() * Math.PI * 2, d = 0.12 + R() * 0.28;
      x += Math.sin(a) * d; z += Math.cos(a) * d;
      const k = Math.hypot(x / 0.55, z / 0.4);
      if (k > 1) { x /= k; z /= k; }
      const r = R();
      const act = r < 0.35 ? "peck" : r < 0.55 ? "look" : r < 0.65 ? "chirp" : null;
      route.push({ x, z, pause: act === "peck" ? 2.5 : 0.3 + R() * 1.4, act });
    }
    // 멀리 와 있으면 가끔 가운데 쪽으로 돌아온다
    if (Math.hypot(x, z) > 0.35 && R() < 0.5) route.push({ x: x * 0.2, z: z * 0.2, pause: 0 });
    this.walk = { route, speed: 0.2 + R() * 0.18, hop: R() < 0.5, wait: 0 };
    this.restYaw = R() < 0.65 ? 0 : (R() - 0.5) * 1.6;
  }

  // 쓰다듬기 (side: 손이 닿은 쪽 +1 왼쪽 / -1 오른쪽 → 그쪽으로 고개를 기댄다)
  petStart(side = 1) { this.pet.on = true; this.pet.side = side; }
  petEnd() { this.pet.on = false; }

  play(name) {
    if (name === "walk") return this.stroll();
    const dur = { hop: 0.5, peck: 1.4, eat: 2.4, lay: 3.4, flap: 2.4, shake: 0.7, happy: 1.6, chirp: 0.6 }[name] ?? 1;
    this.acts.push({ name, t0: this.t, dur });
  }

  level(name) {   // 진행 중인 동작의 0~1 진행도 (없으면 -1)
    const a = this.acts.find((x) => x.name === name);
    return a ? (this.t - a.t0) / a.dur : -1;
  }

  update(dt) {
    const t = (this.t += dt);
    this.acts = this.acts.filter((a) => t - a.t0 < a.dur);
    const { b, look } = this;

    // 걷기: 먼저 갈 쪽으로 몸을 돌리고 → 종종걸음 → 도착하면 다시 앞을 본다
    if (t > this.strollNext) {
      const r = Math.random();
      if (!this.acts.length && !this.walk) {
        if (r < 0.7) this.stroll();
        else if (r < 0.9) this.play("eat");   // 제자리에서 땅 콕콕
      }                                       // 나머지는 그냥 가만히
      this.strollNext = t + 4 + Math.random() * 10;
    }
    // 쓰다듬는 동안은 가만히 (산책 중이면 멈춰서 기다린다)
    const pet = this.pet;
    pet.amt += ((pet.on ? 1 : 0) - pet.amt) * Math.min(1, dt * (pet.on ? 5 : 3));
    const pa = pet.amt;
    if (pa > 0.05 && this.walk) this.walk.wait = Math.max(this.walk.wait, 0.4);
    // 알 품기: 앉는 동안은 산책 안 함. 시간이 다 되면 일어난다
    const sit = this.sit;
    if (sit.on && t > sit.until) { sit.on = false; const d = sit.done; sit.done = null; d?.(); }
    sit.amt += ((sit.on ? 1 : 0) - sit.amt) * Math.min(1, dt * 3);
    const sa = sit.amt;
    if (sit.on) this.strollNext = Math.max(this.strollNext, t + 3);
    const P = this.pos, wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));
    let moving = 0, wantYaw = this.walk ? P.yaw : this.restYaw;
    const W = this.walk;
    if (W && this.level("flap") < 0) {
      const to = W.route[0];
      if (W.wait > 0) W.wait -= dt;                         // 들른 곳에서 쉬는 중
      else if (!to) this.walk = null;
      else {
        const dx = to.x - P.x, dz = to.z - P.z, d = Math.hypot(dx, dz);
        if (d < 0.01) {                                     // 도착: 잠깐 쉬면서 쪼기/두리번/짹
          W.route.shift();
          W.wait = to.pause;
          if (to.act === "peck") this.play("eat");   // 땅을 콕콕 (먹을 게 있나)
          to.onArrive?.();
          if (to.act === "chirp") { this.play("chirp"); this.onChirp?.(); }
          if (to.act === "look") { this.look.target = (Math.random() - 0.5) * 1.2; this.look.next = t + to.pause; }
        } else {
          wantYaw = Math.atan2(dx, dz);
          if (Math.abs(wrap(wantYaw - P.yaw)) < 0.5) {
            // 콩콩 뛰기는 뛰어 있는 동안만 나아간다
            const push = W.hop ? Math.max(0, Math.sin(P.step * 0.5)) * 2 : 1;
            const step = Math.min(d, dt * W.speed * push);
            P.x += (dx / d) * step; P.z += (dz / d) * step;
            moving = 1;
          }
        }
      }
    }
    const hopping = W?.hop && moving;
    const turnLeft = wrap(wantYaw - P.yaw);
    P.yaw += Math.sign(turnLeft) * Math.min(Math.abs(turnLeft), dt * 3.5);
    const stepping = moving || Math.abs(turnLeft) > 0.05 ? 1 : 0;   // 제자리에서 돌 때도 발을 구른다
    P.gait += (stepping - P.gait) * Math.min(1, dt * 8);
    if (P.gait > 0.01) P.step += dt * (W ? 6 + W.speed * 14 : 10); else P.step = 0;   // 빨리 걸으면 걸음도 빠르게
    const g = P.gait, hopG = hopping ? g : 0, walkG = g - hopG;
    const sL = Math.sin(P.step), sR = Math.sin(P.step + Math.PI);
    b.root.position.x = P.x;
    b.root.position.z = P.z;
    b.root.rotation.set(0, P.yaw, 0);   // y 만 바꾸면 복제한 뼈에 남은 x·z(±π) 때문에 뒤돌아 설 수 있다

    // 두리번: 가끔 고개를 돌리거나 갸웃
    if (t > look.next) {
      look.target = (Math.random() - 0.5) * 1.0;
      look.targetTilt = Math.random() < 0.4 ? (Math.random() - 0.5) * 0.6 : 0;
      look.next = t + 2 + Math.random() * 4;
    }
    look.yaw += (look.target - look.yaw) * Math.min(1, dt * 4);
    look.tilt += (look.targetTilt - look.tilt) * Math.min(1, dt * 5);

    // 콕콕 쪼기 (먹이)
    const pk = this.level("peck");
    const peck = pk >= 0 ? Math.max(0, Math.sin(pk * Math.PI * 6)) * 0.55 * (1 - pk) : 0;
    // 먹기: 바닥의 먹이까지 몸을 깊이 숙이고 콕콕 다섯 번
    const ea = this.level("eat");
    const bow = ea >= 0 ? smooth(ea, 0, 0.15) * (1 - smooth(ea, 0.85, 1)) : 0;
    const nib = ea >= 0 ? Math.max(0, Math.sin(ea * Math.PI * 10)) * 0.22 * bow : 0;
    this.bow = bow;
    const P0 = this.pos;
    // 걸을 때는 앞을 보고 걸음마다 고개를 까딱
    // 쓰다듬으면 손 쪽으로 고개를 기울여 비비고, 살짝 들어 올린다
    const rub = this.pet.amt * (0.3 + Math.sin(t * 3) * 0.08) * this.pet.side;
    turn(b.head, peck + bow * this.cfg.eatHead + nib + Math.sin(P0.step * 2) * 0.1 * P0.gait - this.pet.amt * 0.12,
      (look.yaw * (pk >= 0 ? 0.2 : 1) * (1 - P0.gait)) * (1 - this.pet.amt) * (1 - this.bow),   // 먹을 땐 먹이만 본다
      look.tilt * (1 - P0.gait) * (1 - this.pet.amt) + rub);

    // 부리: 짹짹(빠르게 두세 번) / 먹을 때 쪼는 박자에 맞춰 벌렸다 닫기 / 가끔 혼자 짹
    if (!this.chirpNext) this.chirpNext = 5;
    if (t > this.chirpNext) { this.play("chirp"); this.onChirp?.(); this.chirpNext = t + 6 + Math.random() * 8; }
    const ch = this.level("chirp");
    const gape = this.cfg.beakOpen ?? 0.28;   // 부리가 따로 떨어진 조각이면 더 크게 벌려도 된다
    const chirp = ch >= 0 ? Math.max(0, Math.sin(ch * Math.PI * 5)) * gape : 0;
    const eat = Math.max(pk >= 0 ? Math.max(0, Math.sin(pk * Math.PI * 6 + 1.2)) * gape * 0.8 * (1 - pk) : 0,
      ea >= 0 ? Math.max(0, Math.sin(ea * Math.PI * 10 + 1.2)) * gape * 0.8 * bow : 0);
    turn(b.jaw, Math.max(chirp, eat));

    // 꼬리 까딱 (까치가 자주 하는 동작)
    if (t > this.tailNext) { this.play("tailflick"); this.tailNext = t + 2.5 + Math.random() * 4; }
    const tf = this.level("tailflick");
    // 숙일 때 꼬리가 하늘로 솟지 않게 반대로 내려 준다
    turn(b.tail, (tf >= 0 ? -ease(tf) * 0.45 : Math.sin(t * 1.3) * 0.03) - (this.bow ?? 0) * 0.9 + (this.strain ?? 0) * 0.5, Math.sin(t * 9) * 0.18 * pa, 0);   // 기분 좋으면 꼬리 살랑

    // 날개: 평소엔 숨 따라 살짝, 신나면 파닥파닥
    // 파닥파닥 = 살짝 날기: 웅크림(0~0.12) → 떠오름 → 공중에서 날갯짓하며 오르락내리락 → 내려앉기(0.72~0.95)
    const fy = this.level("flap");
    const air = fy >= 0 ? smooth(fy, 0.1, 0.32) * (1 - smooth(fy, 0.72, 0.95)) : 0;
    const beat = fy >= 0 ? fy * Math.PI * 11 : 0;   // 날갯짓 11번
    const hy = this.level("happy");
    // 날 때는 날개를 활짝 편 채 수평을 가운데로 위아래 크게 친다 (위로 40°, 아래로 40°쯤)
    const spread = fy >= 0 ? smooth(fy, 0, 0.1) * (1 - smooth(fy, 0.92, 1)) : 0;
    const flap = fy >= 0 ? spread * this.cfg.wingFlap * (1 + 0.8 * Math.sin(beat))
      : hy >= 0 ? Math.abs(Math.sin(hy * Math.PI * 7)) * this.cfg.wingFlap * ease(Math.min(1, hy * 1.4)) : 0;
    const restW = this.cfg.wingOpen + Math.sin(t * 2.2) * 0.02 + pa * 0.06;   // 쓰다듬으면 날개가 살짝 풀린다
    const open = fy >= 0 ? spread : this.cfg.wingFlap ? Math.min(1, flap / this.cfg.wingFlap * 1.6) : 0;   // 펼칠수록 뒤로 젖힌 걸 푼다
    const sweep = this.cfg.wingSweep * (1 - open);
    const roll = this.cfg.wingRoll * (1 - open);
    // 왼쪽(+x) 날개 기준 각도. 오른쪽은 좌우 거울이라 Y·Z 를 뒤집는다 (X 비틀기는 그대로)
    turn(b.wingL, roll, sweep, restW + flap);      // 날개 끝이 뒤(-Z)·아래·안쪽으로, 깃털 면은 위·바깥으로
    turn(b.wingR, roll, -sweep, -(restW + flap));
    b.wingL.scale.setScalar(this.cfg.wingScale);
    b.wingR.scale.setScalar(this.cfg.wingScale);
    b.tail.scale.setScalar(this.cfg.stubScale);
    if (this.tailMesh) this.tailMesh.quaternion.copy(b.tail.quaternion);   // 새 꼬리도 까딱 (크기는 줄이지 않음)
    const tuck = this.cfg.wingTuck * (1 - open);
    if (b.wingLowL) { turn(b.wingLowL, tuck); turn(b.wingLowR, tuck); }

    // 몸: 숨쉬기 + 부르르 + 쪼을 때 앞으로 숙이기
    const sh = this.level("shake");
    // 알 낳기: 끙끙 (웅크리고 부들부들, 꼬리 번쩍) → 0.72 에서 뿅! (쭉 펴짐)
    const ly = this.level("lay");
    const strain = ly >= 0 ? smooth(ly, 0, 0.15) * (1 - smooth(ly, 0.7, 0.76)) : 0;
    const popK = ly >= 0.7 ? Math.max(0, Math.sin(Math.min(1, (ly - 0.7) / 0.12) * Math.PI)) : 0;
    this.strain = strain;
    const shake = (sh >= 0 ? Math.sin(sh * Math.PI * 14) * 0.08 * (1 - sh) : 0)
      + Math.sin(t * 55) * 0.03 * strain * (0.6 + 0.4 * Math.sin(t * 3));
    // 날 때는 앞으로 나아가듯 몸을 앞으로 숙이고, 날개를 칠 때마다 조금 흔들린다
    turn(b.body, peck * 0.35 + strain * 0.12 + this.bow * this.cfg.eatBody + air * (0.24 + Math.sin(beat) * 0.04) + g * 0.06, 0, shake + sL * 0.07 * walkG);   // 걸을 때 뒤뚱뒤뚱
    const breathe = 1 + Math.sin(t * 2.2) * 0.012;
    const fluff = 1 + pa * 0.04 + sa * 0.07 + strain * 0.04;   // 기분 좋거나 알을 품으면 깃털을 부풀린다
    b.body.scale.set(fluff / Math.sqrt(breathe), breathe * (1 + pa * 0.02), fluff / Math.sqrt(breathe));

    // 통통 뛰기 / 신나서 두 번 뛰기
    const hp = Math.max(this.level("hop"), this.level("happy") * 2 % 1 * (this.level("happy") >= 0 ? 1 : -1));
    // 날기: 높이 + 날갯짓 박자에 맞춘 오르내림 (내려칠 때 쑥 오른다)
    const flyY = air * (0.24 - Math.sin(beat) * 0.02);
    b.root.position.y = (hp >= 0 ? ease(hp) * 0.08 : 0) + flyY + Math.abs(Math.cos(P.step)) * 0.012 * walkG
      + Math.max(0, Math.sin(P.step * 0.5)) * 0.05 * hopG   // 콩콩: 두 발 모아 통통
      - sa * 0.075;                                            // 알 위에 폭 앉기
    let squash = hp >= 0 && hp < 0.15 ? 1 - (0.15 - hp) * 0.6 : 1;
    if (fy >= 0 && fy < 0.12) squash = 1 - Math.sin(fy / 0.12 * Math.PI) * 0.1;              // 뛰기 전 웅크림
    if (fy >= 0.93) squash = 1 - Math.sin(Math.min(1, (fy - 0.93) / 0.07) * Math.PI) * 0.09;   // 착지 쿵
    squash *= (1 - strain * 0.09) * (1 + popK * 0.08);   // 끙끙 웅크렸다가 뿅 하고 쭉
    b.root.scale.set(2 - squash, squash, 2 - squash);

    // 발: 뛸 때 살짝 들기
    const lift = (hp >= 0 ? ease(hp) * 0.25 : 0) + air * 0.6;   // 날 때는 발끝을 아래로 늘어뜨린다
    // 걸음: 한 발씩 들어 앞으로 내딛고, 디딘 발은 뒤로 민다
    const foot = (bone, rest, s, c) => {
      bone.position.copy(rest);
      bone.position.y += sa * 0.07;   // 앉으면 발을 몸 속으로 접는다
      bone.position.y += Math.max(0, s) * 0.035 * walkG;
      bone.position.z += c * 0.035 * walkG;
      turn(bone, lift + sa * 0.9 + Math.max(0, s) * 0.35 * walkG + Math.max(0, Math.sin(P.step * 0.5)) * 0.4 * hopG);
    };
    foot(b.footL, this.footRest.L, sL, -Math.cos(P.step));
    foot(b.footR, this.footRest.R, sR, Math.cos(P.step));
  }
}
