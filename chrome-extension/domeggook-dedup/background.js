/*
 * background.js — service worker
 *
 * content script 로부터 썸네일 URL 목록을 받아, 각 이미지를 cross-origin fetch(blob)로
 * 받은 뒤 dHash(9x8 그레이스케일)를 계산해 돌려준다.
 *
 * content script 가 아니라 여기(service worker)에서 fetch 하는 이유:
 *  - content script 의 fetch 는 페이지 오리진(domeggook.com) 기준이라 CDN 이미지에
 *    CORS 가 걸릴 수 있다. service worker 는 manifest host_permissions 범위 안에서
 *    cross-origin fetch 가 허용되므로 blob 을 직접 받아 픽셀을 읽을 수 있다.
 */

// dHash 알고리즘 버전. 알고리즘을 바꾸면 올려서 캐시를 무효화한다.
const HASH_VER = "dh1";

// fetch 동시 요청 수 제한 (도매꾹/CDN 차단 및 버스트 방지).
const FETCH_CONCURRENCY = 4;

// 메모리 캐시 (service worker 생존 동안). key: URL, value: hex hash.
const memCache = new Map();

// ── dHash 계산 ────────────────────────────────────────────────────────────
// 9x8 로 축소 → 그레이스케일 → 각 행에서 인접 픽셀 비교(9픽셀 → 8비트) → 8행 = 64bit.
async function computeDHash(blob) {
  const bitmap = await createImageBitmap(blob);
  const w = 9, h = 8;
  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bitmap, 0, 0, w, h);
  bitmap.close?.();
  const { data } = ctx.getImageData(0, 0, w, h);

  const gray = new Float32Array(w * h);
  for (let i = 0; i < w * h; i++) {
    const r = data[i * 4], g = data[i * 4 + 1], b = data[i * 4 + 2];
    gray[i] = 0.299 * r + 0.587 * g + 0.114 * b;
  }

  let bits = "";
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w - 1; x++) {
      const left = gray[y * w + x];
      const right = gray[y * w + x + 1];
      bits += left < right ? "1" : "0";
    }
  }
  return bitsToHex(bits); // 64bit → 16 hex chars
}

function bitsToHex(bits) {
  let hex = "";
  for (let i = 0; i < bits.length; i += 4) {
    hex += parseInt(bits.slice(i, i + 4), 2).toString(16);
  }
  return hex;
}

// ── 캐시 포함 fetch + 해시 ─────────────────────────────────────────────────
async function fetchAndHash(url) {
  if (memCache.has(url)) return memCache.get(url);

  const key = HASH_VER + ":" + url;
  try {
    const stored = await chrome.storage.local.get(key);
    if (stored && stored[key]) {
      memCache.set(url, stored[key]);
      return stored[key];
    }
  } catch (_) { /* storage 실패는 무시하고 계산 */ }

  try {
    const resp = await fetch(url, { credentials: "omit", cache: "force-cache" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    const hash = await computeDHash(blob);
    memCache.set(url, hash);
    // 실패(null)는 저장하지 않아 다음 기회에 재시도되게 한다.
    chrome.storage.local.set({ [key]: hash }).catch(() => {});
    return hash;
  } catch (_) {
    // fetch/디코딩 실패 → null. 이 상품은 dedup 대상에서 제외된다.
    return null;
  }
}

// 동시성 제한 워커 풀.
async function mapWithPool(items, limit, fn) {
  const out = {};
  let idx = 0;
  async function worker() {
    while (idx < items.length) {
      const i = idx++;
      const url = items[i];
      out[url] = await fn(url);
    }
  }
  const n = Math.min(limit, items.length);
  await Promise.all(Array.from({ length: n }, worker));
  return out;
}

async function handleGetHashes(urls) {
  const uniq = [...new Set((urls || []).filter(Boolean))];
  const hashes = await mapWithPool(uniq, FETCH_CONCURRENCY, fetchAndHash);
  return { hashes };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "getHashes") {
    handleGetHashes(msg.urls).then(sendResponse).catch(() => sendResponse({ hashes: {} }));
    return true; // 비동기 응답
  }
});
