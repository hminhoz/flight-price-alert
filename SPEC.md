# ✈️ 항공권 최저가 알림

일본·제주 노선 항공권 가격을 2시간마다 자동 감시하고, 특가가 뜨면 텔레그램으로
알려주는 시스템. GitHub Actions에서 무료로 돌아가며 서버·PC 상시 가동이 필요 없다.

설계 문서: [SPEC.md](SPEC.md) · 검색 조건 변경: [config.yaml](config.yaml)

## 설치 (약 15분, 최초 1회)

### 1. 텔레그램 봇 만들기 (5분)

1. 텔레그램에서 `@BotFather` 검색 → 대화 시작
2. `/newbot` 입력 → 봇 이름과 아이디(끝이 `bot`) 지정
3. 발급된 **토큰**을 복사해 둔다 (`123456:ABC-DEF...` 형태)
4. 방금 만든 봇을 검색해 들어가서 **아무 메시지나 하나 보낸다** (중요!)
5. 브라우저에서 아래 주소를 연다 (토큰 부분 교체):
   ```
   https://api.telegram.org/bot<토큰>/getUpdates
   ```
   응답 JSON에서 `"chat":{"id": 숫자}` 의 **숫자가 chat_id**.

### 2. GitHub 저장소 만들기

1. GitHub에서 새 **공개(Public)** 저장소 생성 (공개여야 Actions 무제한 무료)
2. 이 프로젝트의 모든 파일을 업로드 (또는 git push)

### 3. Secrets 등록

저장소 → Settings → Secrets and variables → **Actions** → New repository secret:

| 이름 | 값 |
|------|-----|
| `TELEGRAM_BOT_TOKEN` | 1번에서 받은 토큰 |
| `TELEGRAM_CHAT_ID` | 1번에서 확인한 chat_id |

### 4. 첫 실행 테스트

1. 저장소 → **Actions** 탭 → 워크플로우 활성화
2. `flight-price-check` → **Run workflow** → dry_run에 `1` 입력 후 실행
   (텔레그램 전송 없이 로그로만 확인)
3. 로그에 `검색 완료` 가 보이면 정상. dry_run 없이 한 번 더 실행하면
   이후 2시간마다 자동으로 돈다.

## 운영 노트

- **첫 3일은 관측 기간**: 알림 없이 가격만 수집해 노선×월별 기준가를 만든다.
  4일째부터 특가 알림이 시작된다.
- **조건 변경**: `config.yaml`만 수정해 커밋하면 다음 실행부터 반영.
- **가격 기록**: `data/` 폴더에 자동 커밋으로 쌓인다 (수동 편집 금지).
- **알림이 한동안 없을 때**: 정상일 수 있음(특가가 없는 것). 시스템 자체가
  죽으면 "가격 조회 실패 중" 경고가 온다. Actions 탭에서 최근 실행 로그로도 확인 가능.
- **가격 주의**: 알림 가격은 Google Flights 기준이라 실구매가(특히 제주 국내선의
  네이버/직판 특가)와 다를 수 있다. 알림은 "지금 확인해볼 타이밍" 신호로 쓰고,
  구매는 병기된 링크에서 실가격 확인 후 진행할 것.

## 구조

```
main.py                     실행 진입점
app/settings.py             config.yaml 로더
app/search.py               Google Flights 편도 검색 (fast-flights)
app/engine.py               샤딩·콤보 계산·기준가·알림 판정 (핵심 로직)
app/state.py                data/*.json 상태 관리
app/notify.py               텔레그램 전송·메시지 포맷
app/links.py                Google/네이버 딥링크 생성
tests/test_pipeline.py      네트워크 없는 통합 테스트 (python tests/test_pipeline.py)
.github/workflows/          2시간 간격 스케줄 + 상태 커밋
```

## 알려진 한계

- fast-flights는 비공식 스크래핑: Google 개편 시 일시 중단될 수 있음 (경고 알림 옴)
- 편도 합산 방식이라 대한항공/아시아나 왕복 전용 특가는 일부 놓칠 수 있음
- GitHub Actions cron은 수 분~수십 분 지연될 수 있음

## 노선 추가/제거

`config.yaml`의 `routes` 목록에 한 줄 추가하고 커밋하면 끝 (코드 수정 불필요):

```yaml
  - { origin: ICN, destination: DAD, label: "인천-다낭" }          # 국제선
  - { origin: GMP, destination: PUS, label: "김포-부산", domestic: true }  # 국내선은 domestic 표시
```

- 공항 코드는 IATA 3글자 (도쿄처럼 도시 통합 코드 TYO도 가능)
- 새 노선은 해당 노선만 3일 관측을 새로 거친 뒤 알림 시작 (기존 노선은 영향 없음)
- 비용: 노선 1개당 실행 시간 약 +5분 (실측 8초/검색 기준).
  현재 평시 ~30분 / 타임아웃 75분이라 **7~8개 노선 추가까지는 조정 불필요.**
  그 이상이면 `imminent_days`를 줄이거나 여행 기간을 좁혀 시간을 되찾을 것.
