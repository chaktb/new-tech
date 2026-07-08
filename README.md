# new-tech · Daily Photonics Digest

매일 아침 08:00 (KST) 에 arXiv + 학술 RSS 에서 **Si Photonics / PIC / Quantum Computing**
관련 새 논문·기사를 수집하고, 로컬 **Ollama (oss-120b)** 로 한국어 요약을 붙여
`public/posts/YYYY-MM-DD.html` 를 생성하고 GitHub 에 push 한다.
Cloudflare Workers 가 push 를 감지해 자동 배포한다.

사이트: https://new-tech.chaktb.workers.dev

---

## 구조

```
new-tech/
├─ wrangler.toml              # [assets] directory = "./public"
├─ public/
│  ├─ index.html             # 랜딩 (AUTO_CARDS 마커에 Digest 카드 자동 삽입)
│  └─ posts/YYYY-MM-DD.html  # 매일 생성되는 Digest
└─ scripts/
   └─ daily_photonics_digest.py
```

## DGX Spark 셋업 (최초 1회)

```bash
# 1) 리포 클론 (홈 디렉토리에)
cd ~
git clone https://github.com/chaktb/new-tech.git
cd new-tech

# 2) git 인증 설정 (둘 중 하나)
#   a) gh CLI:   gh auth login
#   b) 토큰 리모트:
#      git remote set-url origin https://chaktb:<PAT>@github.com/chaktb/new-tech.git
git config user.name  "chaktb"
git config user.email "chaktb@gmail.com"

# 3) Ollama 모델 확인 (정확한 태그를 CONFIG 에 반영)
ollama list          # 예: gpt-oss:120b

# 4) 수동 테스트 1회
python3 scripts/daily_photonics_digest.py
```

`scripts/daily_photonics_digest.py` 상단 **CONFIG 블록**에서
`OLLAMA_URL`, `OLLAMA_MODEL`, `REPO_DIR` 만 환경에 맞게 확인/수정.

## crontab 등록 (매일 08:00 KST)

```bash
crontab -e
```
아래 한 줄 추가 (DGX Spark 시스템 시간대가 KST 라고 가정):

```cron
0 8 * * *  /usr/bin/python3 $HOME/new-tech/scripts/daily_photonics_digest.py >> $HOME/new-tech/digest.log 2>&1
```

> 시스템이 UTC 로 돌면 `0 23 * * *` (23:00 UTC = 익일 08:00 KST) 로 설정.
> 확인: `timedatectl` 로 Time zone 점검.

## 로그 확인

```bash
tail -f ~/new-tech/digest.log
```

## 튜닝 포인트 (CONFIG)

| 변수 | 의미 |
|---|---|
| MAX_ITEMS | 하루 최대 게시 항목 수 (기본 8) |
| LOOKBACK_HOURS | 최근 N시간 내 항목만 (기본 30) |
| ARXIV_QUERIES | arXiv 검색 카테고리·키워드 |
| RSS_FEEDS | 추가 학술/뉴스 RSS |
| KEYWORDS | 제목·초록 필터 정규식 |
| OLLAMA_ENABLE | False 시 요약 없이 제목+링크만 |

## 문제 해결

- **push 실패 (403/401):** 토큰 만료/권한 부족 → PAT 재발급 (Contents: Read and write)
- **요약 비어 있음:** Ollama 미기동 또는 모델 태그 불일치 → `ollama list` 확인, OLLAMA_URL 점검
- **빌드 실패:** wrangler.toml 의 [assets] directory 가 ./public 인지 확인
- **arXiv 403:** 방화벽/프록시에서 export.arxiv.org 차단 여부 확인
