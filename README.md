# market-data

공개 시장 지표 스냅숏. 평일 06:35·07:05 KST에 GitHub Actions가 `fetch_market.py`를 돌려 `latest.json`을 갱신한다.

- 담는 것: 나스닥100·S&P500·VIX·미10년·달러인덱스·MU, CNN 공포탐욕·풋콜 단계, CAPE·FactSet 선행 PER, 미국 기준금리·다음 FOMC·선물 내재 변화, 코스피·코스피200·외국인 순매수, 신용거래융자·반대매매(금융투자협회).
- 담지 않는 것: 개인 보유 종목·수량 등 개인 정보(공개 저장소).
- 값마다 `asof`(실제 기준일)·`source`(출처)를 단다. 읽는 쪽이 날짜를 검사한다.
- 읽는 주소: `https://raw.githubusercontent.com/hoonyr-alt/market-data/main/latest.json`
