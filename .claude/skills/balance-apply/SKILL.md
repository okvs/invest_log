---
name: balance-apply
description: 증권사 '잔고' 스크린샷을 비전으로 읽어 종목별 융자(신용대출) 금액을 invest_log 잔고에 반영한다. 호출 형태는 오직 `/balance-apply <req_id>`. 매수매도 봇이 사진을 받아 주입한다. 카톡 체결엔 증거금이 안 나와서 스샷으로 종목별 융자를 실측 반영하는 용도. 절대 다른 트리거로 자동 실행하지 말 것.
---

# balance-apply 스킬 — 잔고 스샷 → 종목별 융자 반영

## 무엇인가
사용자가 증권사 앱 **잔고 화면 스크린샷**을 invest_log 매수매도 봇(텔레그램)으로
보내면, 봇이 사진을 저장하고 이 워크스페이스의 Claude 세션에 `/balance-apply <req_id>`
를 주입한다. 나는 스샷을 **비전으로 읽어 종목별 융자(신용/대출) 금액**을 뽑아
`credit_loan` 을 실측값에 맞추고, **결과를 텔레그램으로 회신**한다.
(카톡 체결 알림엔 증거금/융자가 안 나와 자동반영이 현금매수로 잡히므로, 스샷으로 보정한다.)

## 호출 조건
- `/balance-apply <req_id>` 로 호출됐을 때만 실행. `<req_id>` 가 없거나 요청파일이
  없으면 아무것도 하지 않고 종료한다. 다른 어떤 상황에서도 자동 실행하지 않는다.

## 고정 경로 (항상 절대경로, 작업 디렉토리 무관)
```
ROOT   = /Users/seung/.openclaw/workspace/invest_log
PY     = $ROOT/.venv/bin/python
요청   = $ROOT/data/balance_shots/<req_id>.json   # {req_id,image_path,chat_id,caption,...}
헬퍼   = $ROOT/scripts/balance_apply.py           # state / apply / reply
```

## 처리 절차

1. **요청 읽기** — `Read` 로 `$ROOT/data/balance_shots/<req_id>.json`.
   `image_path`, `chat_id`, `caption` 을 확보. 파일이 없으면 즉시 종료.

2. **스샷 파싱(비전)** — `Read` 로 `image_path` 의 이미지를 읽는다. 증권사 '잔고' 화면에서
   **종목별 융자/신용/대출 금액(원)** 을 추출한다. 화면 레이아웃이 증권사마다 다르니 유연하게:
   - "융자", "신용", "대출", "신용융자", "융자금액", "융자금" 등의 라벨/컬럼을 종목과 매칭.
   - 같은 종목이 **'현금' 행 + '자기융자/유통융자' 행으로 나뉘어** 보일 수 있다. 융자금액은
     자기융자/유통융자 행의 값이고, 현금 행은 융자 0. 종목별로 **융자금액을 합산**해 한 값으로.
   - 금액은 **원 단위 그대로** 읽는다(콤마 제거). 만/억 단위로 환산하지 말 것 — 헬퍼에 원으로 넘긴다.
   - 현금매수(융자 없음)로 보이는 종목은 `0` 으로 둔다(stale 한 기존 융자를 0 으로 정정).
   - **스샷에 융자 정보가 전혀 안 보이면** 추측하지 말고, 4·5 단계를 건너뛰어 6단계에서
     "융자 컬럼이 보이는 잔고 화면을 다시 보내달라"고 회신하고 종료.
   - 가능하면 종목별 **수량·평단**도 같이 읽어둔다(3단계 기록과 대조용).
   - **⚠️ 어느 계좌인지 반드시 확인**: 화면 상단 계좌 헤더를 본다. KB증권(예: "종합위탁 277-…", "국내주식잔고")
     이면 `KB`, 신한투자증권이면 `신한`. 한 계좌만 보이는 스샷이면 그 계좌명을 5단계 `--account` 로 넘긴다.
     '전체' 합산 화면이면 `--account` 없이 합산값으로 넣는다.

3. **현재 기록 조회** — 한 번 실행:
   ```bash
   cd /Users/seung/.openclaw/workspace/invest_log && .venv/bin/python scripts/balance_apply.py state
   ```
   출력 JSON(`holdings`: name/quantity/avg_price/credit_loan/**by_account**[account/quantity/credit/funding])을
   현재 상태로 삼는다. 시스템은 **KB+신한 합산** credit_loan 을 저장하고, 계좌별 분해는 by_account 에 있다.

4. **대조** — 스샷에서 읽은 값과 현재 기록을 맞춘다(종목명은 공백/대소문자 무시 매칭).
   - **융자**: 스샷 값으로 갱신할 `{종목명: 융자원}` 맵을 만든다(현금 종목은 0 포함). 현재 보유에 없는
     종목명은 헬퍼가 unmatched 로 돌려주니 그대로 둬도 된다. **스샷에 안 나온 보유 종목은 맵에 넣지 말 것**(기존 융자 유지).
   - **🚨 한 계좌만 보이는 스샷의 함정**: 시스템 credit_loan 은 KB+신한 합산이다. 한 계좌 스샷의 융자를
     **합산 credit_loan 에 그대로 덮으면 다른 계좌(예: 신한) 융자가 사라진다.** 반드시 5단계에서
     `--account <그 계좌>` 를 쓴다 — 헬퍼가 그 계좌 분만 갱신하고 다른 계좌 융자는 보존한 채 합산을 재계산한다.
     (state.by_account 로 다른 계좌에 융자가 남아있는지 확인 가능.)
   - **수량/평단**: 차이가 있으면 **자동 반영하지 말고** 회신에 ⚠️ 로만 알린다(사용자가 봇 '매수'/'수정'으로 직접 처리).
     한 계좌 스샷이면 다른 계좌 보유분이 안 보이니 수량 대조는 생략/주의.

5. **융자 반영** — 4에서 만든 맵을 JSON 으로 넘겨 적용(+대시보드 재발행):
   ```bash
   # 한 계좌(KB/신한)만 보이는 스샷 — 그 계좌만 갱신, 다른 계좌 보존(권장):
   cd /Users/seung/.openclaw/workspace/invest_log && .venv/bin/python scripts/balance_apply.py apply <req_id> --account KB '{"삼성전자우": 39725000, "삼성전기": 27290000, "SK하이닉스": 50693000}'
   # 전체 합산 화면이면 --account 없이:
   cd /Users/seung/.openclaw/workspace/invest_log && .venv/bin/python scripts/balance_apply.py apply <req_id> '{"삼성전자": 39140000}'
   ```
   `--account` 모드는 그 계좌 by_account.credit 을 set하고 **combined = 계좌별 합**으로 재계산(증권사별 막대도 정합).
   출력 JSON 의 `changes`(name/old/new 합산원), `unmatched`, `warnings`, `total_loan` 을 회신에 쓴다.
   변경이 하나도 없으면(모두 기록과 동일) 그 사실도 회신.

6. **결과 회신(텔레그램)** — 한국어로 모바일에서 읽기 좋게 간결하게 작성해 회신:
   ```bash
   cd /Users/seung/.openclaw/workspace/invest_log && .venv/bin/python scripts/balance_apply.py reply <req_id> --text "✅ 융자 반영 완료 (스샷 기준)
   ─────
   SK하이닉스  5,069만 → 5,200만
   삼성전자    0 → 3,914만
   총 융자: 1억 2,345만원

   ⚠️ 수량 차이(확인 필요): 삼성전자 기록 10주 vs 스샷 12주 — 맞으면 봇에 '매수'/'수정'으로 반영"
   ```
   - 금액은 만/억 단위로 환산해 보여준다(읽기용). `changes` 의 old/new(원)를 만으로 나눠 표기.
   - 변경 없는 종목은 생략. unmatched(보유에 없는 종목)는 "보유에 없어 건너뜀: …" 으로 알림.
   - 수량/평단 차이가 없으면 ⚠️ 줄은 생략.
   - `reply` 는 **요청당 정확히 한 번만** 호출(중복 전송 방지).

## 하지 말 것
- req_id/요청파일 없는데 임의로 처리하기.
- 융자를 추측으로 채우기(스샷에 안 보이면 회신으로 되묻고 종료).
- 수량·평단을 사용자 확인 없이 자동 변경하기(이 스킬은 **융자만** 반영).
- 결과를 터미널에만 남기고 텔레그램 회신을 빠뜨리기(이 스킬의 존재 이유가 텔레그램 통보).
