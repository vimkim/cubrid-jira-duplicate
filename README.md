# CBRD 중복 이슈 검사기 PoC

CUBRID JIRA(`jira.cubrid.org`)의 CBRD 프로젝트에서 새 이슈를 등록하기 전과 후에, 이미 등록된 중복·관련 이슈를 찾아 주는 검사기의 PoC 저장소다. 관련 이슈는 [CBRD-27978](https://jira.cubrid.org/browse/CBRD-27978)이다.

`cubrid-jira-dup candidate-find`는 초안이나 이슈 키를 받아, 중복이거나 관련 있을 수 있는 기존 CBRD 이슈를 순위대로 제안한다. 후보가 정말 중복인지는 이 도구가 정하지 않는다. 출력을 읽는 LLM(예: Opus 5.5)이나 사람이 정한다. GPU 없이 CPU만으로 돈다.

## 문서

- [요구사항](docs/requirements.md). PoC가 만족해야 할 조건, 평가 방법, 합격 기준, 결정 기록.
- [근거 측정](docs/evidence.md). 정답 쌍, 현재 JQL 검색의 기준선, 생성 시각 차, 제목 언어 혼합, 생성량.
- [요구사항 리뷰](docs/reviews/requirements-review.md). 독립 리뷰 17건과 그 근거.
- [이슈 작성 브리프](docs/issue-brief.md). CBRD-27978을 쓸 때 쓴 측정값과 지시.

`docs/issue-brief.md`, `docs/briefs/`, `docs/reviews/`, `docs/jira/`는 날짜가 붙은 기록이라 예전 `scripts/` 경로를 그대로 인용한다.

## 후보 찾기

[uv](https://docs.astral.sh/uv/)와 [just](https://just.systems)가 필요하다. 저장소 루트에서 `just sync`를 실행하면 `cubrid-jira-dup`이 `uv tool install`로 설치된다.

### 하는 일과 하지 않는 일

`candidate-find`는 다음을 한다.

- JIRA 공개 REST API를 로그인 없이 HTTPS로 읽어 로컬 인덱스를 갱신한다.
- 후보 이슈를 기본 20개, `-k`로 1-50개까지 순위대로 낸다.
- 후보마다 키, URL, 제목, 유형, 상태, resolution, 생성 시각, 그리고 그 후보를 올린 신호를 싣는다. 신호는 채널별 순위, 질의 시각보다 며칠 먼저 생성되었는지(나중이면 음수), 질의와 함께 나오는 식별자와 오류 코드다.

`candidate-find`는 다음을 하지 않는다.

- 후보가 중복인지, 관련 이슈인지 판정하지 않는다.
- JIRA에 쓰지 않는다. 링크, 댓글, 라벨을 달지 않고 등록을 막지 않는다.
- 초안을 외부 서비스로 보내지 않는다. 임베딩 모델은 로컬에서 돈다.

### 입력 세 가지

입력은 한 번에 하나만 준다. 둘을 함께 주거나 하나도 주지 않으면 검색하기 전에 코드 2로 끝난다.

```bash
# 초안 Markdown. 첫 번째 `# ` 줄이 제목이고 나머지가 본문이다.
cubrid-jira-dup candidate-find draft.md

# 기존 이슈 키. 대소문자를 가리지 않고, 그 이슈 자신은 후보에서 뺀다.
cubrid-jira-dup candidate-find CBRD-27516

# 제목은 인자로, 본문은 표준 입력으로 준다.
cubrid-jira-dup candidate-find --title "loaddb가 느리다" < body.txt
```

`--json`을 붙이면 표준 출력에 JSON 문서 하나만 쓴다. 진행 상황과 진단은 표준 오류로 간다. 스키마 이름은 `cubrid-jira-dup/candidates/1`이다.

```bash
cubrid-jira-dup candidate-find draft.md --json > candidates.json
```

종료 코드는 두 가지다.

| 코드 | 뜻 |
|---|---|
| 0 | 동기화와 검색이 끝났다. 후보가 0개여도 0이다. |
| 2 | 입력이 잘못되었거나 인덱스가 없다. 또는 동기화가 실패하거나 불완전해서 기존 인덱스로 검색했다. 이 경우에는 후보도 출력한다. |

`--no-sync`를 붙이면 JIRA에 묻지 않고 지금 인덱스로만 검색한다.

### 첫 실행

인덱스가 없으면 `candidate-find`가 먼저 CBRD 이슈 전체를 받고, 본문과 댓글과 제목을 모두 임베딩한다. 이때 임베딩 모델 `intfloat/multilingual-e5-small`(약 470 MB)을 한 번 내려받는다. 그다음부터는 바뀐 이슈만 받고, 글이 바뀐 행만 다시 임베딩한다.

2026-11-09에 개발 서버(논리 CPU 80개 중 0-39번, 다른 작업으로 load average 약 55)에서 `cubrid-jira-dup sync --threads 16`으로 처음부터 만든 측정값이다.

- 이슈 7,975건을 받아 색인하는 데 31.7초가 걸렸다.
- 본문, 댓글, 제목 37,438개를 임베딩하는 데 1,202초가 걸렸다. 모델 다운로드 52초가 이 안에 들어 있다.
- 전체 20분 35초, 최대 메모리 3.2 GB였다.

그 뒤 `candidate-find`는 바뀐 이슈가 없을 때 동기화에 약 0.3초를 쓰고, 전체 2.8-3.1초에 끝났다. 그중 약 2초는 새 프로세스가 모델을 올리고 질의를 임베딩하는 시간이다.

인덱스를 미리 만들거나 갱신하려면 `cubrid-jira-dup sync`를 실행한다. `sync --full`은 모든 이슈를 다시 받고, JIRA에서 더 이상 보이지 않는 이슈를 지운다.

### 인덱스와 모델 위치

인덱스 디렉터리는 `--index-dir`로 정한다. 주지 않으면 `$CUBRID_JIRA_DUP_HOME`을 쓰고, 그것도 없으면 `~/.cache/cubrid-jira-dup`(`$XDG_CACHE_HOME`이 있으면 그 아래)을 쓴다. 디렉터리에는 다음이 있다.

- `index.db`. 이슈, 검색 행과 벡터, 단어별 posting 목록, 동기화 상태를 담은 SQLite 파일 하나.
- `models/`. 내려받은 임베딩 모델.
- `raw/`. 마지막 동기화들의 JIRA 응답 원문.

위 측정에서 `index.db`는 178 MB, `models/`는 465 MB, `raw/`는 97 MB였다.

### 스레드

새 이슈 글을 임베딩할 때는 기본으로 논리 CPU의 절반을 쓴다. `--threads N`으로 바꾼다. `candidate-find`와 `sync`, `eval candidates` 모두 이 옵션을 받는다. `candidate-find`가 질의 하나를 임베딩할 때는 `--threads`와 4 중 작은 값을 쓴다. 짧은 글 하나는 스레드를 늘려도 거의 빨라지지 않고 CPU 시간만 는다.

```bash
cubrid-jira-dup sync --threads 4
```

numpy의 OpenBLAS는 스레드 1개로 돈다. 그대로 두면 import할 때 CPU마다 스레드를 띄워 헛돌린다. 바꾸려면 `OPENBLAS_NUM_THREADS`를 설정한다.

2026-11-09에 개발 서버(논리 CPU 80개 중 0-39번)에서 `candidate-find CBRD-27756 --no-sync --json`을 세 번씩 잰 값이다. 바꾸기 전에는 질의 임베딩에 스레드 40개를 썼고 OpenBLAS 스레드 수도 제한하지 않았다.

| | 사용자 CPU | 경과 시간 |
|---|---|---|
| 바꾸기 전 | 42.3-48.2초 | 7.6-7.9초 |
| 바꾼 뒤 | 3.5-3.7초 | 3.5-3.6초 |

### 검색 방식

각 채널이 질의 시각 이전의 이슈와 댓글만으로 이슈 순위를 매긴다. 이슈의 점수는 그 이슈에서 가장 잘 맞는 행(본문 또는 댓글 하나)의 점수다.

| 채널 | 순위 기준 | 가중치 |
|---|---|---|
| lexical | 질의 제목과 본문 전체의 BM25 | 2 |
| semantic | 질의 전체와 각 행의 e5-small 코사인 유사도 | 1 |
| title | 질의 제목과 이슈 제목의 BM25 | 1 |
| title semantic | 질의 제목과 이슈 제목의 e5-small 코사인 유사도 | 1 |
| stack | 질의의 콜 스택 상위 함수 두 개의 BM25. 스택이 없으면 쓰지 않는다 | 4 |

채널마다 상위 200개를 가중 RRF(k = 10)로 합치고, 질의 시각 가까이에 생성된 이슈에 작은 가산점을 더한다. 점수가 같으면 키 번호가 작은 이슈가 앞선다. 이 조합은 정답 쌍의 조정용(dev) 절반에서 골랐다.

질의 시각은 초안과 `--title` 입력이면 지금이다. 이슈 키 입력이면 그 이슈의 생성 시각이고, 그보다 조금 먼저 생성된 이슈와 조금 나중에 생성된 이슈가 똑같이 가깝다. JSON의 `query.time`이 질의 시각이고, `query.time_reference`가 그 기준(`now` 또는 `issue created`)이다. 평가는 질의보다 먼저 생성된 이슈만 검색하므로 이 규칙과 무관하다.

BM25는 방식을 고를 때 쓴 bm25s의 Lucene 식(k1 1.5, b 0.75)을 그대로 계산한다. `index.db`에는 단어마다 그 단어가 나오는 행 번호와 횟수가 posting 목록으로 들어 있다. 검색은 질의 단어마다 목록 하나를 읽어 numpy로 더한다. 질의에 두 번 나온 단어는 두 번 센다. 문서 수, 문서 빈도, 평균 길이는 인덱스 전체에서 구하므로 질의 시각에 따라 바뀌지 않는다. 동기화는 바뀐 이슈의 posting만 이슈와 같은 트랜잭션에서 고친다. `sync --full`은 posting을 처음부터 다시 만든다. 고정 스냅샷의 정답 질의 160개에서 lexical, title, stack 채널의 상위 200개 이슈는 bm25s로 계산한 것과 모두 같았다.

2026-11-09에 고정한 스냅샷(이슈 7,968건, 정답 쌍 중 연결 후보가 먼저 생성된 202쌍)에서 `--mask identifiers`로 잰 값이다.

| 구간 | 쌍 | recall@10 | recall@20 |
|---|---|---|---|
| 평가용(test) 전체 | 103 | 0.825 | 0.835 |
| 평가용 중 2026년 생성 질의 | 35 | 0.800 | 0.829 |
| 조정용(dev) 전체 | 99 | 0.737 | 0.788 |
| 전체 | 202 | 0.782 | 0.812 |
| 두 제목 중 한쪽에만 한글 | 32 | 0.469 | 0.531 |

합격 기준은 평가용 recall@10이 전체와 2026년 생성 질의 모두 0.8 이상이다(요구사항 7.4). 둘 다 기준을 넘는다. 다만 2026년 생성 질의는 35쌍 중 28쌍으로 기준선에 딱 걸려 있고, Wilson 95% 구간은 0.641-0.900이다.

### 평가 다시 돌리기

```bash
cubrid-jira-dup sync --index-dir data/index
cubrid-jira-dup eval candidates --index-dir data/index --method hybrid
```

`--method bm25`는 lexical 채널만 쓰는 기준선이다. 평가 질의는 질의 이슈의 제목과 설명만 쓰고, 질의 이슈보다 먼저 생성된 이슈와 그 시각 이전에 쓰거나 고친 댓글만 검색한다(요구사항 7.2). 질의 텍스트의 `CBRD-<숫자>` 키와 `cbrd_27516`, `db25833`처럼 이슈 번호를 담은 식별자는 가린다. 초안이 원본을 이미 알면 검색할 필요가 없기 때문이다. `--mask keys`는 `CBRD-<숫자>`만 가린다. `candidate-find`는 아무것도 가리지 않는다.

## 수치 다시 계산하기

모든 명령은 공개 REST API를 로그인 없이 읽는다. 저장소 루트에서 실행한다.

```bash
just sync
cubrid-jira-dup measure corpus
cubrid-jira-dup measure gold
cubrid-jira-dup measure probe
cubrid-jira-dup measure baseline
cubrid-jira-dup measure gold-stats
cubrid-jira-dup measure volume --now "2026-11-06 17:34"
```

설치하지 않고 `uv run cubrid-jira-dup measure ...`로도 실행할 수 있다. 모든 `measure` 명령은 `--data-dir`(기본값 `./data`)을 받고, 네트워크를 쓰는 명령은 `--refresh`도 받는다.

응답 원문은 `data/raw/`에 캐시되고 git은 이 디렉터리를 무시한다. 캐시가 없으면 명령이 JIRA에서 다시 받으므로, 결과가 문서의 값과 조금 다를 수 있다. 최신 값으로 다시 재려면 `--refresh`를 붙인다. `measure corpus`의 `created >= -30d` 같은 구간은 요청한 날 기준이라, 캐시가 있으면 처음 잰 날의 값이 나온다.

## 개발

```bash
just setup   # uv sync
just check   # ruff check와 pytest. 테스트는 네트워크를 쓰지 않고 모델도 내려받지 않는다
just sync    # 설치된 cubrid-jira-dup을 현재 소스로 갱신한다(just install과 같다)
```

코드는 `src/cbrd_dup/`에 있다.

- `jira.py`는 JIRA 접근(익명 조회, 요청 간격, 재시도, 원문 캐시)을 모두 맡는다.
- `corpus.py`는 이슈 타입과 검색 행을 정의하고 동기화를 한다. `index.py`는 `index.db`를 읽고 쓴다.
- `embed.py`는 e5-small 임베딩을 맡는다. 테스트는 가짜 임베더를 넣는다.
- `search.py`는 검색 방식, `find.py`는 `candidate-find`, `evaluate.py`는 recall 평가다.
- `measure/`에는 근거 측정 명령이 있다.

## 데이터

- `data/gold-pairs.jsonl`. `resolution = Duplicate`로 닫힌 CBRD 이슈에서 뽑은 312쌍.
- `data/baseline-results.jsonl`. 현재 JQL 검색으로 정답 쌍을 찾은 결과.

두 파일의 내용은 모두 공개 JIRA 이슈에서 왔다.
