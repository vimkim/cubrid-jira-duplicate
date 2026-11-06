# CBRD 중복 이슈 검사기 PoC

CUBRID JIRA(`jira.cubrid.org`)의 CBRD 프로젝트에서 새 이슈를 등록하기 전과 후에, 이미 등록된 중복·관련 이슈를 찾아 주는 검사기의 PoC 저장소다. 관련 이슈는 [CBRD-27978](http://jira.cubrid.org/browse/CBRD-27978)이다.

지금은 요구사항과 근거 측정 단계다. 검사기 코드는 아직 없다.

## 문서

- [요구사항](docs/requirements.md). PoC가 만족해야 할 조건, 평가 방법, 합격 기준, 결정 기록.
- [근거 측정](docs/evidence.md). 정답 쌍, 현재 JQL 검색의 기준선, 생성 시각 차, 제목 언어 혼합, 생성량.
- [요구사항 리뷰](docs/reviews/requirements-review.md). 독립 리뷰 17건과 그 근거.
- [이슈 작성 브리프](docs/issue-brief.md). CBRD-27978을 쓸 때 쓴 측정값과 지시.

## 수치 다시 계산하기

모든 스크립트는 공개 REST API를 로그인 없이 읽는다. 저장소 루트에서 실행한다.

```bash
python3 -I scripts/measure_cbrd.py
python3 -I scripts/build_gold.py
python3 -I scripts/measure_baseline.py
python3 -I scripts/measure_gold_stats.py
python3 -I scripts/measure_volume.py --now "2026-11-06 17:34"
```

응답 원문은 `data/raw/`에 캐시되고 git은 이 디렉터리를 무시한다. 캐시가 없으면 스크립트가 JIRA에서 다시 받으므로, 결과가 문서의 값과 조금 다를 수 있다. 최신 값으로 다시 재려면 `--refresh`를 붙인다.

## 데이터

- `data/gold-pairs.jsonl`. `resolution = Duplicate`로 닫힌 CBRD 이슈에서 뽑은 312쌍.
- `data/baseline-results.jsonl`. 현재 JQL 검색으로 정답 쌍을 찾은 결과.

두 파일의 내용은 모두 공개 JIRA 이슈에서 왔다.
