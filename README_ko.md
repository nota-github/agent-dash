# agent-dash

[English](README.md) | 한국어

Claude Code, Codex 세션의 진행 상황을 한 화면에서 보는 개인용 대시보드

- 웹 화면과 터미널 화면(TUI) 제공
- 여러 작업을 동시에 실행할 때 세션별 진행 상황 확인용

## 화면

> 화면의 세션, 경로, 서버 주소는 모두 예시 값

**웹 화면, 세로형**

![웹 화면, 세로형](docs/images/web-grid.png)

**웹 화면, 가로형**

![웹 화면, 가로형](docs/images/web-list.png)

**터미널 화면(TUI)**

![터미널 화면](docs/images/tui.png)

## 주요 기능

- **세션 카드**
  - 진행률, 예상 남은 시간, 계획 단계
  - 메인 에이전트, 서브 에이전트, 하위 세션(`claude -p`, `codex exec`)
  - 토큰 사용량, 컨텍스트 사용률
  - 다음 날 이어서 작업할 재개 명령
- **상태 표시**
  - 실행 중, 선택/승인 필요(객관식 질문, 승인 창), 입력 차례, 정체, 백그라운드 실행
  - 입력 차례인 카드는 바탕 움직임으로 강조
  - 입력 차례일 때 에이전트 마지막 응답의 마지막 문단을 카드에 표시. 웹은 눌러서, 터미널 화면은 `m`으로 전체 보기
  - 카드의 원은 상태만 표시. 진행률은 막대, 퍼센트, 완료 단계 수, 남은 시간으로 따로 표시
  - 진행 중 단계는 활동량(끝난 단계와 비교한 시간, 도구 호출 수)으로 채움. 최대 90%
  - 계획 단계가 없는 세션은 퍼센트 대신 활동량 표시: 이번 턴 경과 시간, 도구 호출 수, 최근 10분 호출 수
  - 팁: 에이전트 지침(CLAUDE.md, AGENTS.md)에 "여러 단계 작업은 계획 도구로 단계를 나누고 단계 이름 끝에 `[S]`, `[M]`, `[L]`을 붙인다"를 넣으면 추정이 정확해짐
  - 세션을 직접 완료로 표시 (웹: "완료로 표시" 버튼, 터미널 화면: `d`). 완료한 세션은 맨 뒤로 가고, 새 활동이 생기면 자동으로 다시 열림
- **실행 환경 표시**
  - 실행 방식: remote(앱이 SSH로 연결), cli(서버 터미널에서 실행)
  - 실행 서버 IP
  - 멀티플렉서: tmux, zellij, screen, cmux, herdr
- **보기**
  - 정렬: 최근 실행순, 이름순, 에이전트별, 직접 배치
  - 카드 끌어서 옮기기
  - Claude, Codex 필터
  - 세로형, 가로형

## 수집 원칙

- Claude, Codex가 남기는 기록 파일을 읽기만 함
- 훅, 플러그인, 스킬 설치 없음. 추가 토큰 사용 없음
- 등록한 세션만 읽음
- 사용자 입력(요청, 답변, 거부 사유)은 수집, 저장, 표시하지 않음
- 화면에는 Claude, Codex가 쓴 글만 표시: 제목, 요약, 입력 차례일 때 에이전트 마지막 응답
  - 에이전트 응답에는 사용자가 요청한 말이 인용될 수 있음. 마지막 응답을 숨기려면 `~/.agentdash/config.json`에 `"show_last_message": false`
  - 다른 파일로 복사해 저장하지 않고, 표시할 때 세션 기록에서 읽음
- 웹 서버는 `127.0.0.1`에만 열림

## 설치

```bash
curl -fsSL https://raw.githubusercontent.com/nota-github/agent-dash/main/install_ko.sh | sh
```

- 에이전트를 실행하는 서버에서 실행
- `install_ko.sh`는 안내 문구가 한국어. 영어 안내는 `install.sh`
- 고른 언어는 설정에 남아 업데이트할 때도 그대로 사용
- 필요 조건: Python 3.9 이상 (다른 패키지 불필요)
- 운영체제
  - Linux: 지원, 확인 완료
  - macOS: 동작할 구조지만 아직 확인 전. 프로세스와 멀티플렉서 정보는 표시하지 않음
  - Windows: 지원하지 않음 (WSL 확인 전)
- 설치 중 선택
  - 화면: 웹, 터미널(TUI), 둘 다
  - 웹 포트: 24000~25000에서 빈 포트 자동 선택. 없으면 직접 입력
- 설치 위치
  - 코드: `~/.local/share/agent-dash`
  - 실행 파일: `~/.local/bin/agent-dash`
  - 설정, 등록 목록: `~/.agentdash`
- 셸 설정 파일은 바꾸지 않음. `~/.local/bin`이 PATH에 없으면 추가 방법 안내
- 질문 없이 설치

  ```bash
  curl -fsSL https://raw.githubusercontent.com/nota-github/agent-dash/main/install_ko.sh | AGENT_DASH_MODE=both AGENT_DASH_PORT=24500 sh
  ```

## 지원 사용 방식

| 방식 | 지원 | 설치 위치와 접속 |
|---|---|---|
| 1. 로컬 PC에서 Claude, Codex 앱 직접 실행 | 다음 버전 (Mac) | |
| 2. 로컬 앱을 원격 서버에 SSH로 연결해 실행 | 지원 | 원격 서버에 설치. 웹은 SSH 터널로 접속 |
| 3. 원격 서버에 SSH로 접속해 CLI로 실행 | 지원 | 원격 서버에 설치. 웹은 SSH 터널, TUI는 SSH 터미널에서 |

## 세션 등록

- 설치 직후에는 표시되는 세션 없음
- 볼 세션을 직접 등록해야 하며, 등록한 세션만 읽음

| 방법 | 등록 | 해제 |
|---|---|---|
| 웹 화면 | 오른쪽 위 "설정 > 세션 관리"에서 "등록" | 카드 오른쪽 위 x, 또는 세션 관리의 "등록됨" |
| 터미널 화면 | `a`(세션 관리)에서 세션 선택 후 `Enter` | 카드 선택 후 `x`, 또는 세션 관리에서 `Enter` |
| 명령 | `agent-dash track <세션 ID 앞부분>` | `agent-dash untrack <세션 ID 앞부분>` |

```bash
agent-dash sessions                 # 최근 세션 목록과 ID (* 등록됨)
agent-dash track 3b71ac63           # 세션 ID 앞부분으로 등록
agent-dash track --dir ~/work/api   # 이 폴더 아래 세션을 앞으로 생기는 것까지 모두 등록
```

- 서브 에이전트와 하위 세션(`claude -p`, `codex exec`)은 따로 등록하지 않아도 부모 카드 안에 표시
- 해제한 세션은 세션 관리 목록 맨 위 "최근 해제한 세션"에서 다시 등록 가능
- 등록 목록은 웹 화면과 터미널 화면이 함께 사용

## 사용법

```bash
agent-dash start      # 웹 서버 켜기 (백그라운드)
agent-dash stop       # 웹 서버 끄기
agent-dash restart    # 웹 서버 다시 켜기
agent-dash status     # 설치 정보, 웹 서버 상태, 포트, 등록 세션 수
agent-dash tui        # 터미널 화면 (q 종료, ? 키 도움말)
```

- tmux, zellij 안에서 실행할 필요 없음
  - 웹 서버는 백그라운드로 실행되어 SSH 연결을 끊어도 유지
  - 서버 재부팅 후에는 `agent-dash start` 다시 실행
- 원격 서버의 웹 화면 접속
  - 로컬 PC에서 SSH 터널 연결 후 브라우저로 `http://localhost:<포트>` 접속
  - 터널 명령은 `agent-dash start` 실행 시 사용자, 주소, SSH 포트를 채워 안내

    ```bash
    ssh -f -N -L <포트>:127.0.0.1:<포트> <사용자>@<서버>
    ```

  - `-f`: 터미널을 닫아도 터널 유지
  - 끊겨도 자동으로 다시 연결하려면 `autossh -M 0 -f -N -L ...` 사용
- 자세한 내용: [MANUAL.md](MANUAL.md)

## 포트 변경

```bash
agent-dash status                                  # 지금 포트 확인
AGENT_DASH_PORT=24600 agent-dash update --force    # 24600으로 바꿔 다시 설치
```

- 같은 버전을 바꾼 포트로 다시 설치. 등록 목록과 다른 설정은 유지
- 켜져 있던 웹 서버는 새 포트로 다시 켬
- 로컬 PC의 SSH 터널도 새 포트로 다시 연결
  - 이전 터널 끄기: `pkill -f "<이전 포트>:127.0.0.1:<이전 포트>"`
  - 새 터널 명령은 `agent-dash start` 실행 시 안내
- 포트가 이미 사용 중이면 설치를 멈추고 알림. 다른 번호로 다시 실행
- 1024~65535 중 다른 프로그램이 쓰지 않는 번호 사용

## 업데이트

```bash
agent-dash update                  # 최신 버전으로 업데이트
agent-dash update --mode both      # 화면 종류를 바꿀 때
```

- 화면 종류, 포트, 등록 목록 유지
- 켜져 있던 웹 서버는 업데이트 후 다시 켬
- 설치 명령(`curl ... | sh`)을 다시 실행해도 같음
- 새 버전 안내
  - `agent-dash` 명령 실행 시, 웹 화면과 터미널 화면 위쪽에 표시
  - 1시간에 한 번 GitHub의 버전 파일만 내려받아 비교 (보내는 정보 없음)
  - 끄기: `~/.agentdash/config.json`에 `"update_check": false`
- 변경 내용: [CHANGELOG.md](CHANGELOG.md)

## 삭제

```bash
agent-dash uninstall                  # 설치 전 상태로 되돌림
agent-dash uninstall --purge-backup   # 원복용 백업까지 삭제
```

- 설치한 모든 파일을 설치 기록에 남기고, 삭제 시 기록을 거꾸로 따라 되돌림
- `~/.local/bin`처럼 다른 프로그램과 함께 쓰는 폴더는 비어 있을 때만 삭제

## 개발

```bash
python3 -m unittest discover -s tests
./install.sh          # 저장소 안에서 실행하면 이 폴더의 코드로 설치
```
