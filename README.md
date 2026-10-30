# agent-dash

Claude Code와 Codex로 여러 작업을 동시에 돌릴 때, 세션마다 메인 에이전트와 서브에이전트의 진행 상황을 한 화면에서 보여주는 개인용 대시보드입니다. 웹 화면과 터미널 화면(TUI)을 제공합니다.

- 세션별 카드: 진행률, 예상 남은 시간, 계획 단계, 메인 에이전트와 서브에이전트, 하위 세션(`claude -p`, `codex exec`)
- 상태: 실행 중, 사용자 대기, 사용자 입력(승인) 차례, 정체, 백그라운드 실행
- 세션을 실행 중인 서버 IP와 멀티플렉서(tmux, zellij, screen, cmux, herdr) 표시
- 토큰 사용량, 컨텍스트 사용률, 다음 날 이어서 작업할 재개 명령
- 정렬(최근 실행순, 이름순, 에이전트별, 직접 배치), 끌어서 위치 바꾸기, 세로형과 가로형 보기

Claude와 Codex가 원래 남기는 기록 파일을 읽기만 합니다. 훅, 플러그인, 스킬을 설치하지 않고, 추가 토큰도 쓰지 않습니다. 사용자가 입력한 내용(요청, 답변, 거부 사유)은 수집하지도, 저장하지도, 표시하지도 않습니다. 화면에는 Claude나 Codex가 만든 제목과 요약만 나옵니다.

## 설치

에이전트를 실행하는 서버에서 아래 한 줄을 실행합니다. Python 3.9 이상이 필요하고, 다른 패키지는 필요 없습니다.

```bash
curl -fsSL https://raw.githubusercontent.com/nota-github/agent-dash/main/install.sh | sh
```

설치 중에 묻는 것:

1. 설치할 화면: 웹, 터미널(TUI), 둘 다
2. 웹 포트: 24000~25000 대역에서 비어 있는 포트를 자동으로 고릅니다. 비어 있는 포트가 없으면 직접 입력합니다.

설치 위치는 `~/.local/share/agent-dash`(코드), `~/.local/bin/agent-dash`(실행 파일), `~/.agentdash`(설정, 등록 목록)입니다. 셸 설정 파일은 바꾸지 않습니다. `~/.local/bin`이 PATH에 없으면 설치를 마칠 때 추가 방법을 안내합니다.

질문 없이 설치하려면 환경 변수를 줍니다.

```bash
curl -fsSL https://raw.githubusercontent.com/nota-github/agent-dash/main/install.sh | AGENT_DASH_MODE=both AGENT_DASH_PORT=24500 sh
```

## 사용 방식

| 방식 | 지원 | 설치 위치와 접속 |
|---|---|---|
| 1. 로컬 PC에서 Claude, Codex 앱을 직접 실행 | 다음 버전 (Mac) | |
| 2. 로컬 앱을 원격 서버에 SSH로 연결해 실행 | 지원 | 원격 서버에 설치. 웹은 SSH 터널로 접속 |
| 3. 원격 서버에 SSH로 접속해 CLI로 실행 | 지원 | 원격 서버에 설치. 웹은 SSH 터널, TUI는 그 SSH 터미널에서 |

## 사용법

```bash
agent-dash sessions                 # 최근 세션 목록 (* 등록됨)
agent-dash track <세션 ID 앞부분>    # 세션 등록
agent-dash track --dir <폴더>       # 폴더 아래에서 실행하는 세션을 모두 등록

agent-dash start                    # 웹 서버 켜기 (백그라운드)
agent-dash stop                     # 웹 서버 끄기
agent-dash restart                  # 웹 서버 다시 켜기
agent-dash status                   # 설치 정보, 웹 서버 상태, 포트, 등록 세션 수
agent-dash tui                      # 터미널 화면 (q로 종료)

agent-dash update                   # 최신 버전으로 업데이트
agent-dash uninstall                # 삭제
```

tmux나 zellij 안에서 실행할 필요는 없습니다. 웹 서버는 백그라운드로 따로 실행되므로 SSH 연결을 끊어도 계속 돕니다. 서버를 재부팅하면 꺼지므로 그때는 `agent-dash start`를 다시 실행합니다. 터미널 화면은 SSH 터미널에서 필요할 때 엽니다.

웹 서버는 `127.0.0.1`에만 열립니다. 원격 서버라면 로컬 PC에서 터널을 연 뒤 브라우저로 `http://localhost:<포트>`에 접속합니다.

```bash
ssh -f -N -L <포트>:127.0.0.1:<포트> <사용자>@<서버>
```

`-f`를 붙이면 터미널을 닫아도 터널이 유지됩니다. 끊겨도 자동으로 다시 연결하려면 `autossh -M 0 -f -N -L ...`을 씁니다. 자세한 내용은 [MANUAL.md](MANUAL.md)를 봅니다.

## 업데이트

```bash
agent-dash update                     # 화면 종류, 포트, 등록 목록은 그대로 두고 최신 버전으로 바꿈
agent-dash update --mode both         # 화면 종류를 바꿀 때
```

설치 명령(`curl ... | sh`)을 다시 실행해도 같습니다. 웹 서버가 켜져 있었다면 업데이트 뒤 다시 켭니다.

새 버전이 나오면 `agent-dash` 명령을 실행할 때, 그리고 웹 화면과 터미널 화면 위쪽에 안내가 나옵니다. 확인은 하루에 한 번 GitHub의 버전 파일을 내려받는 것뿐입니다. 끄려면 `~/.agentdash/config.json`에 `"update_check": false`를 넣습니다. 바뀐 내용은 [CHANGELOG.md](CHANGELOG.md)에 있습니다.

## 삭제

```bash
agent-dash uninstall                  # 설치 전 상태로 되돌림
agent-dash uninstall --purge-backup   # 원복용 백업까지 삭제
```

설치한 모든 파일은 설치 기록에 남고, 삭제하면 기록을 거꾸로 따라가 설치 전 상태로 되돌립니다. `~/.local/bin`처럼 다른 프로그램과 함께 쓰는 폴더는 비어 있을 때만 지웁니다.

## 개발

```bash
python3 -m unittest discover -s tests
./install.sh          # 저장소 안에서 실행하면 내려받지 않고 이 폴더의 코드를 설치
```
