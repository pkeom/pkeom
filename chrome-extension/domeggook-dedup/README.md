# 도매꾹 썸네일 중복 정리 (Chrome Extension, Manifest V3)

도매꾹 PC 웹 **검색결과 페이지**(`itemList.php`)에서 **시각적으로 같은 썸네일**을 가진 상품을
이미지 해시(dHash)로 묶어, 그룹마다 **(판매가 + 최소배송비) 합계가 가장 싼 1개만 남기고** 나머지는
숨겨 중복을 정리합니다.

- 대상 페이지: `https://domeggook.com/main/item/itemList.php*`
- 원본 페이지 최소 침범: **숨김(display:none) + 작은 배지 + 펼침 패널**만 추가합니다.

---

## 동작 방식

1. **content script**(`content.js`)가 상품 카드(`li.col2`)를 수집합니다.
   상품 판별: `a.title` 과 `div.amt` 를 **모두** 가진 `li.col2` 만.
   - 썸네일: `a.thumb > img` 의 `src`(비었으면 `data-src`/`data-original` 등 레이지 속성)
   - 상품명: `a.title`
   - 판매가: `div.amt > b` (옵션가 `div.lowP` 는 사용하지 않음)
   - 최소배송비: `div.infoDeli` 의 첫 숫자, "무료"=0
   - 링크: `a.thumb` href / 판매자: `.seller .nick a`, 등급: `.grade b`
2. **placeholder 제외**: 썸네일 파일명에 `img_notExist` 가 포함된 "이미지없음" placeholder는
   해시/중복 판별에서 제외하고 그대로 표시합니다. (placeholder끼리 잘못 묶이는 것 방지)
3. **CORS 우회**: 이미지는 content script 가 아니라 **background service worker**(`background.js`)에서
   `fetch(blob)` 으로 받습니다. manifest 의 `host_permissions` 로 cross-origin fetch 를 허용합니다.
4. service worker 에서 `blob → createImageBitmap → OffscreenCanvas` 로 그려 픽셀을 읽고
   **dHash(9×8 그레이스케일, 인접 픽셀 비교 → 64bit)** 를 계산합니다.
   fetch/디코딩 실패한 이미지는 그 상품을 그냥 건너뜁니다(다른 것과 묶지 않음).
5. 두 상품 dHash 의 **hamming distance ≤ THRESHOLD** 이면 같은 제품으로 묶습니다.
6. 각 그룹에서 (판매가 + 최소배송비) 최저 1개만 남기고 나머지 `li` 는 `display:none`.
   판매가를 못 뽑은 상품은 비교에서 제외(숨기지 않음).
7. 남긴 카드에 **"+N" 배지**(N = 숨긴 개수). 중복이 없으면 배지 없음.
8. 배지 클릭 → 남긴 카드 **바로 아래 패널**을 펼쳐 숨긴 상품들을 표시(썸네일/판매가/배송비/판매자/등급/링크).
   다시 클릭하면 접힘(토글). 원본 숨긴 `li` 위치는 건드리지 않습니다.

페이지네이션 / 무한스크롤은 `MutationObserver` 로 새 `li.col2` 를 감지해 처리하며,
이미 처리한 카드와 이미 계산한 URL 해시는 재계산하지 않습니다.

---

## 설치법 (개발자 모드 로드)

1. Chrome 주소창에 `chrome://extensions` 입력 후 이동.
2. 우측 상단 **개발자 모드(Developer mode)** 토글을 켭니다.
3. **압축해제된 확장 프로그램을 로드합니다(Load unpacked)** 클릭.
4. 이 폴더(`chrome-extension/domeggook-dedup`)를 선택합니다.
5. 도매꾹 검색결과 페이지(`https://domeggook.com/main/item/itemList.php?...`)를 열면
   자동으로 동작합니다. 코드를 수정했으면 확장 카드의 **새로고침(↻)** 을 누르세요.

---

## `host_permissions` 설명

```json
"host_permissions": [
  "https://cdn1.domeggook.com/*",
  "https://*.domeggook.com/*"
]
```

- 썸네일 이미지는 도매꾹 CDN(예: `cdn1.domeggook.com`)에서 제공됩니다.
- service worker 가 이 도메인들의 이미지를 **cross-origin fetch(blob)** 로 받아 픽셀을 읽으려면
  해당 오리진이 `host_permissions` 에 있어야 합니다.
- `https://*.domeggook.com/*` 는 `cdn1`, `cdn2` 등 여러 CDN 서브도메인을 함께 커버합니다.

---

## 튜닝: THRESHOLD

`content.js` 상단 상수입니다.

```js
const THRESHOLD = 5; // 두 dHash 의 hamming distance 가 이 값 이하이면 같은 제품
```

- **기본 5(엄격)** 에서 시작하세요.
- **흰 배경 오판**(다른 상품인데 흰 배경끼리 묶임)이 나면 값을 **낮춥니다**(예: 3).
- **중복을 너무 안 잡으면** 값을 조금 **올립니다**(예: 7~8).

`background.js` 의 `HASH_VER` / `FETCH_CONCURRENCY` 로 캐시 무효화와 동시 fetch 수(기본 4)를 조정할 수 있습니다.

---

## 한계

- **흰 배경 오판**: 배경이 흰색으로 큰 썸네일들은 dHash 가 비슷해져 다른 상품이 같이 묶일 수 있습니다.
  → `THRESHOLD` 를 낮춰 조정하세요.
- **같은 제품 · 다른 사진**: 같은 상품이라도 촬영 각도/편집이 다른 썸네일은 해시가 달라 **묶이지 않습니다**
  (이미지 유사도 기반이라 텍스트/상품코드 매칭은 하지 않음).
- **첫 로딩 지연**: CDN 이미지를 다수 fetch 하므로 처음에는 계산이 끝날 때까지 몇 초 걸릴 수 있습니다.
  (계산된 해시는 메모리 + `chrome.storage.local` 에 캐시되어 재방문 시 빨라집니다.)
- **dedup 제외 대상**: `img_notExist` placeholder, 썸네일 URL 을 못 찾은 상품, fetch/디코딩 실패 상품,
  판매가를 못 뽑은 상품은 중복 판별에서 제외되어 **숨겨지지 않고** 그대로 표시됩니다.

---

## 파일 구성

| 파일 | 역할 |
|------|------|
| `manifest.json` | MV3 매니페스트 (권한, content script, service worker 등록) |
| `content.js` | 카드 수집 · 클러스터링 · 숨김/배지/패널 렌더 (THRESHOLD 상수 포함) |
| `background.js` | service worker — cross-origin fetch + dHash 계산 + 캐시 |
| `styles.css` | 배지 · 패널 최소 스타일 |
