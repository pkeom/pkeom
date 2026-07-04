/*
 * content.js
 *
 * 도매꾹 검색결과(itemList.php) 페이지에서 상품 카드(li.col2)를 수집하고,
 * 썸네일 dHash(background 계산)를 이용해 "시각적으로 같은" 상품을 묶어
 * (판매가 + 최소배송비) 합계가 가장 싼 1개만 남기고 나머지는 숨긴다.
 */
(() => {
  "use strict";

  // ── 튜닝 상수 ────────────────────────────────────────────────────────────
  // 두 dHash 의 hamming distance 가 이 값 이하이면 같은 제품으로 묶는다.
  //  · 기본 5(엄격). 흰 배경끼리 잘못 묶이면(오판) 낮추고,
  //    같은 상품인데 안 묶이면 조금 올린다. (0~64 범위, 실용값은 대략 2~10)
  const THRESHOLD = 5;

  // MutationObserver 처리 디바운스(ms).
  const DEBOUNCE_MS = 300;

  // ── 상태 ────────────────────────────────────────────────────────────────
  const allCards = [];               // 지금까지 수집한 모든 상품 카드
  const hashCache = new Map();       // URL -> hex hash (background 응답 캐시)
  const processedEls = new WeakSet(); // 이미 처리한 li
  let decorations = [];              // {badge, panel} — render 때마다 정리
  let hiddenEls = new Set();         // 숨긴 li 들 — render 때마다 복원

  // ── 파싱 유틸 ────────────────────────────────────────────────────────────
  function firstNumber(text) {
    if (!text) return null;
    const m = text.replace(/,/g, "").match(/\d+/);
    return m ? parseInt(m[0], 10) : null;
  }

  // 판매가: div.amt > b (직계 b). div.lowP(옵션가)는 사용하지 않는다.
  function parsePrice(li) {
    const b = li.querySelector(".amt > b");
    return b ? firstNumber(b.textContent) : null;
  }

  // 최소배송비: div.infoDeli 의 첫 숫자. "무료" 포함이면 0.
  // 못 뽑으면 0 으로 두되 unknown=true (카드에 "배송비확인" 표시).
  function parseFee(li) {
    const el = li.querySelector(".infoDeli");
    if (!el) return { fee: 0, unknown: true };
    const txt = el.textContent || "";
    if (txt.includes("무료")) return { fee: 0, unknown: false };
    const n = firstNumber(txt);
    if (n == null) return { fee: 0, unknown: true };
    return { fee: n, unknown: false };
  }

  // 썸네일 실제 URL (레이지로딩 대비).
  function getThumbUrl(li) {
    const img = li.querySelector("a.thumb img");
    if (!img) return null;
    const cand = [
      img.getAttribute("src"),
      img.getAttribute("data-src"),
      img.getAttribute("data-original"),
      img.getAttribute("data-lazy"),
      img.getAttribute("data-lazy-src"),
      img.getAttribute("data-echo"),
    ];
    for (const c of cand) {
      if (!c) continue;
      const v = c.trim();
      if (!v || v.startsWith("data:")) continue; // 빈 값/인라인 placeholder 스킵
      try { return new URL(v, location.href).href; } catch (_) { return v; }
    }
    return null;
  }

  // 도매꾹 "이미지없음" placeholder (예: img_notExist330.gif) → dedup 제외.
  function isPlaceholder(url) {
    return !!url && url.includes("img_notExist");
  }

  function textOf(li, sel) {
    const el = li.querySelector(sel);
    return el ? el.textContent.trim() : "";
  }

  // li.col2 → 상품 카드 객체. 상품 조건 미충족 시 null.
  function extractCard(li) {
    // 상품 판별: a.title 과 div.amt 를 모두 가진 것만.
    if (!li.querySelector("a.title") || !li.querySelector(".amt")) return null;

    const thumbUrl = getThumbUrl(li);
    const price = parsePrice(li);
    const { fee, unknown } = parseFee(li);
    const linkEl = li.querySelector("a.thumb");

    return {
      el: li,
      thumbUrl,
      title: textOf(li, "a.title"),
      price,
      fee,
      feeUnknown: unknown,
      total: price != null ? price + fee : null,
      link: linkEl ? linkEl.href : "",
      nick: textOf(li, ".seller .nick a") || textOf(li, ".nick a"),
      grade: textOf(li, ".grade b"),
      excluded: isPlaceholder(thumbUrl) || !thumbUrl, // placeholder/썸네일없음 → dedup 제외
      hash: null,
    };
  }

  // ── 해밍 거리 (16 hex = 64bit) ─────────────────────────────────────────────
  function hamming(hexA, hexB) {
    let x = BigInt("0x" + hexA) ^ BigInt("0x" + hexB);
    let c = 0;
    while (x) { c += Number(x & 1n); x >>= 1n; }
    return c;
  }

  // ── 클러스터링 (union-find) ────────────────────────────────────────────────
  function cluster(cards) {
    const parent = cards.map((_, i) => i);
    const find = (i) => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
    const union = (a, b) => { parent[find(a)] = find(b); };

    for (let i = 0; i < cards.length; i++) {
      for (let j = i + 1; j < cards.length; j++) {
        if (hamming(cards[i].hash, cards[j].hash) <= THRESHOLD) union(i, j);
      }
    }
    const groups = {};
    cards.forEach((c, i) => {
      const r = find(i);
      (groups[r] = groups[r] || []).push(c);
    });
    return Object.values(groups);
  }

  // ── 배지 / 패널 ────────────────────────────────────────────────────────────
  function buildPanel(hidden) {
    const panel = document.createElement("div");
    panel.className = "ddedup-panel";
    panel.style.display = "none";

    hidden.forEach((c) => {
      const row = document.createElement("div");
      row.className = "ddedup-row";

      const img = document.createElement("img");
      img.className = "ddedup-thumb";
      img.loading = "lazy";
      if (c.thumbUrl) img.src = c.thumbUrl;

      const price = document.createElement("span");
      price.className = "ddedup-price";
      price.textContent = c.price != null ? c.price.toLocaleString() + "원" : "-";

      const fee = document.createElement("span");
      fee.className = "ddedup-fee";
      fee.textContent = c.feeUnknown
        ? "배송비확인"
        : (c.fee > 0 ? "배송비 " + c.fee.toLocaleString() + "원" : "무료배송");

      const seller = document.createElement("span");
      seller.className = "ddedup-seller";
      seller.textContent = (c.nick || "-") + (c.grade ? " [" + c.grade + "]" : "");

      const link = document.createElement("a");
      link.className = "ddedup-link";
      link.href = c.link || "#";
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "상품보기";

      row.append(img, price, fee, seller, link);
      panel.appendChild(row);
    });
    return panel;
  }

  function addBadge(keep, hidden) {
    const badge = document.createElement("span");
    badge.className = "ddedup-badge";
    badge.textContent = "+" + hidden.length;
    badge.title = "숨긴 중복 " + hidden.length + "건 — 클릭하여 펼치기";

    const cs = getComputedStyle(keep.el);
    if (cs.position === "static") keep.el.style.position = "relative";
    keep.el.appendChild(badge);

    const panel = buildPanel(hidden);
    keep.el.after(panel);

    badge.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      panel.style.display = panel.style.display === "none" ? "block" : "none";
    });

    decorations.push({ badge, panel });
  }

  // 배송비 못 뽑은 카드에 작은 "배송비확인" 표시(그룹과 무관, 1회만).
  function addFeeWarn(card) {
    if (card.el.querySelector(".ddedup-feewarn")) return;
    const w = document.createElement("span");
    w.className = "ddedup-feewarn";
    w.textContent = "배송비확인";
    const cs = getComputedStyle(card.el);
    if (cs.position === "static") card.el.style.position = "relative";
    card.el.appendChild(w);
  }

  // ── 렌더 (전체 재구성) ─────────────────────────────────────────────────────
  function clearDecorations() {
    for (const d of decorations) { d.badge.remove(); d.panel.remove(); }
    decorations = [];
    for (const el of hiddenEls) { el.style.display = ""; }
    hiddenEls = new Set();
  }

  function render() {
    clearDecorations();

    // dedup 대상: 제외 안 됨 + 해시 성공 + 판매가 있음.
    const eligible = allCards.filter((c) => !c.excluded && c.hash && c.price != null);
    const groups = cluster(eligible);

    for (const g of groups) {
      if (g.length < 2) continue;
      // (판매가 + 최소배송비) 합계 최저 1개만 남긴다.
      g.sort((a, b) => a.total - b.total);
      const keep = g[0];
      const hidden = g.slice(1);
      hidden.forEach((c) => { c.el.style.display = "none"; hiddenEls.add(c.el); });
      addBadge(keep, hidden);
    }
  }

  // ── 신규 카드 처리 ─────────────────────────────────────────────────────────
  function getHashes(urls) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: "getHashes", urls }, (resp) => {
          if (chrome.runtime.lastError || !resp) { resolve({}); return; }
          resolve(resp.hashes || {});
        });
      } catch (_) { resolve({}); }
    });
  }

  let processing = false;
  async function processNewCards() {
    const lis = document.querySelectorAll("li.col2");
    const newCards = [];

    lis.forEach((li) => {
      if (processedEls.has(li)) return;
      processedEls.add(li);
      const obj = extractCard(li);
      if (!obj) return; // 상품 카드 아님 (광고 등) — 재처리만 방지
      li.dataset.ddedup = "1";
      if (obj.feeUnknown && !obj.excluded && obj.price != null) addFeeWarn(obj);
      allCards.push(obj);
      newCards.push(obj);
    });

    if (newCards.length === 0) return;

    // 해시 필요한 URL만 background 로 요청.
    const need = newCards
      .filter((c) => !c.excluded && c.thumbUrl && c.price != null && !hashCache.has(c.thumbUrl))
      .map((c) => c.thumbUrl);
    const uniq = [...new Set(need)];

    if (uniq.length) {
      const res = await getHashes(uniq);
      for (const [u, h] of Object.entries(res)) if (h) hashCache.set(u, h);
    }

    // 캐시에서 해시 배정 (이전에 실패한 것도 재시도로 채워질 수 있음).
    for (const c of allCards) {
      if (!c.hash && c.thumbUrl && hashCache.has(c.thumbUrl)) c.hash = hashCache.get(c.thumbUrl);
    }

    render();
  }

  function scheduleProcess() {
    if (processing) return;
    processing = true;
    setTimeout(async () => {
      processing = false;
      try { await processNewCards(); } catch (e) { /* 조용히 무시 */ }
    }, DEBOUNCE_MS);
  }

  // ── 시작 ────────────────────────────────────────────────────────────────
  const observer = new MutationObserver(scheduleProcess);
  observer.observe(document.body, { childList: true, subtree: true });

  // 최초 1회.
  processNewCards();
})();
