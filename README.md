# Marvis Bot
> **A must-have Personal AI for people who keep forgetting ideas or find scheduling annoying.**  
> **My + Jarvis = Marvis**
Marvis Bot is a personal Telegram-based AI assistant running on an Oracle Cloud Ubuntu server.
---
## 🇺🇸 English
## Introduction
**Marvis Bot** is a personal Telegram-based AI assistant running on an Oracle Cloud Ubuntu server.
When the user sends a message through Telegram, Marvis generates a response using the Gemini API, converts the answer into a Korean mp3 voice file using gTTS, and sends both the text and audio response back through Telegram.
> A must-have Personal AI for people who keep forgetting ideas or find scheduling annoying.  
> **My + Jarvis = Marvis**
## Project Goal
The goal of this project is to build a lightweight personal AI assistant that can be used quickly from Telegram.
Main workflow:
```txt
Send a Telegram message
→ Generate an answer with Gemini API
→ Convert the answer into Korean speech with gTTS
→ Send both text and mp3 audio back to Telegram
→ Listen with AirPods while moving

Features

* Receive Telegram text messages
* Generate responses with Gemini API
* Convert Korean text into mp3 speech
* Send both text and audio responses
* Run continuously on an Oracle Cloud server
* Background execution with systemd
* Automatic restart after server reboot

Tech Stack

Area	Technology
Server	Oracle Cloud VM
OS	Ubuntu 22.04
Bot Framework	python-telegram-bot
AI Model	Google Gemini API
TTS	gTTS
Process Manager	systemd
Language	Python

Server Environment

Cloud Provider: Oracle Cloud Infrastructure
Instance Shape: VM.Standard.E2.1.Micro
OS: Ubuntu 22.04
User: ubuntu

The server was configured using an Oracle Always Free eligible instance.

Project Structure

marvis-bot/
├── bot.py
├── requirements.txt
├── README.md
├── .env.example
├── .gitignore
└── voices/

Environment Variables

Create a .env file in the project root.

TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here

Do not commit the .env file to GitHub because it contains sensitive credentials.

Installation

1. Connect to the server

ssh -i "/path/to/private-key.key" ubuntu@your-server-ip

2. Move to the project directory

cd ~/marvis-bot

3. Create and activate a Python virtual environment

python3 -m venv venv
source venv/bin/activate

4. Install dependencies

pip install -r requirements.txt

Or install them manually:

pip install python-telegram-bot google-generativeai gTTS python-dotenv

5. Create .env

nano .env

Add the following values:

TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here

Save with:

Ctrl + O
Enter
Ctrl + X

Manual Run

source venv/bin/activate
python bot.py

If the bot starts correctly, the terminal shows:

Marvis bot is running...

Then send a message to the Telegram bot.

systemd Service Setup

To keep the bot running after closing the SSH session, register it as a systemd service.

1. Create the service file

sudo nano /etc/systemd/system/marvis-bot.service

2. Paste the service configuration

[Unit]
Description=Marvis Telegram Gemini Bot
After=network.target
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/marvis-bot
Environment="PATH=/home/ubuntu/marvis-bot/venv/bin"
ExecStart=/home/ubuntu/marvis-bot/venv/bin/python /home/ubuntu/marvis-bot/bot.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target

3. Enable and start the service

sudo systemctl daemon-reload
sudo systemctl enable marvis-bot
sudo systemctl start marvis-bot

4. Check status

sudo systemctl status marvis-bot

Expected result:

Active: active (running)

Exit the status screen with:

q

Useful Service Commands

Check status:

sudo systemctl status marvis-bot

Restart the bot:

sudo systemctl restart marvis-bot

Stop the bot:

sudo systemctl stop marvis-bot

View logs:

journalctl -u marvis-bot -f

Troubleshooting

SSH key permission error

If SSH shows a key permission error, run:

chmod 600 "/path/to/private-key.key"

Then connect again:

ssh -i "/path/to/private-key.key" ubuntu@your-server-ip

Gemini model 404 error

The initial model name was:

model = genai.GenerativeModel("gemini-1.5-flash")

It caused the following error:

404 models/gemini-1.5-flash is not found

The issue was resolved by changing the model to:

model = genai.GenerativeModel("gemini-2.5-flash")

google.generativeai FutureWarning

The bot may show a warning that the google.generativeai package is deprecated.

This is currently only a warning. The bot still works.
The code can be migrated to the newer google.genai SDK in the future.

Cost Notes

This project was designed for free-tier usage.

Current checked status:

* Oracle instance: VM.Standard.E2.1.Micro
* Gemini API: Free tier
* Google Cloud Billing: disabled / no active billing account
* Telegram Bot API: free
* gTTS library: free to use in this setup

Important precautions:

* Do not enable Google Cloud Billing unless needed
* Do not expose the Gemini API key
* Do not expose the Telegram Bot Token
* Do not commit .env
* Do not create paid Oracle resources such as Load Balancer, NAT Gateway, paid database, or extra block volumes

Current Status

* Oracle server created
* SSH connection completed
* Python virtual environment created
* Required packages installed
* Telegram Bot Token configured
* Gemini API Key configured
* Gemini 2.5 Flash response tested successfully
* Text and mp3 audio response tested successfully
* systemd service registered
* Service status confirmed as active (running)

Future Improvements

* iPhone Shortcuts integration
* Direct voice input workflow
* Chat history memory
* Schedule management
* Idea memo feature
* More natural TTS
* Migration from google.generativeai to google.genai
* Local mini PC deployment after testing

⸻

🇰🇷 Korean

Marvis Bot 소개

Marvis Bot은 Oracle Cloud Ubuntu 서버 위에서 실행되는 개인용 Telegram AI 비서입니다.

사용자가 Telegram으로 메시지를 보내면, Marvis는 Gemini API를 통해 답변을 생성하고, gTTS를 이용해 답변을 한국어 음성 mp3 파일로 변환한 뒤, 텍스트 답변과 음성 답변을 함께 Telegram으로 전송합니다.

자꾸 아이디어를 까먹거나 스케쥴링이 귀찮은 사람에게 필수인 Personal AI,
My + Jarvis = Marvis

프로젝트 목표

이 프로젝트의 목표는 이동 중에도 빠르게 사용할 수 있는 개인 AI 비서를 만드는 것입니다.

주요 사용 흐름은 다음과 같습니다.

Telegram 메시지 입력
→ Gemini API 답변 생성
→ gTTS로 한국어 음성 변환
→ Telegram으로 텍스트 + mp3 음성 답변 전송
→ AirPods로 이동 중 청취

주요 기능

* Telegram 텍스트 메시지 수신
* Gemini API 기반 답변 생성
* 한국어 음성 mp3 변환
* 텍스트 답변 + 음성 답변 동시 전송
* Oracle Cloud 서버에서 24시간 대기
* systemd를 통한 백그라운드 실행
* 서버 재부팅 후 자동 실행

기술 스택

구분	기술
Server	Oracle Cloud VM
OS	Ubuntu 22.04
Bot Framework	python-telegram-bot
AI Model	Google Gemini API
TTS	gTTS
Process Manager	systemd
Language	Python

서버 환경

Cloud Provider: Oracle Cloud Infrastructure
Instance Shape: VM.Standard.E2.1.Micro
OS: Ubuntu 22.04
User: ubuntu

현재 서버는 Oracle Always Free 대상 인스턴스를 기준으로 구성했습니다.

프로젝트 구조

marvis-bot/
├── bot.py
├── requirements.txt
├── README.md
├── .env.example
├── .gitignore
└── voices/

환경 변수 설정

프로젝트 루트에 .env 파일을 생성합니다.

TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here

.env 파일에는 민감한 API 키가 들어가므로 GitHub에 올리면 안 됩니다.

설치 방법

1. 서버 접속

ssh -i "/path/to/private-key.key" ubuntu@your-server-ip

2. 프로젝트 폴더 이동

cd ~/marvis-bot

3. Python 가상환경 생성 및 활성화

python3 -m venv venv
source venv/bin/activate

4. 패키지 설치

pip install -r requirements.txt

또는 직접 설치합니다.

pip install python-telegram-bot google-generativeai gTTS python-dotenv

5. .env 파일 생성

nano .env

아래 내용을 입력합니다.

TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here

저장 방법:

Ctrl + O
Enter
Ctrl + X

수동 실행

source venv/bin/activate
python bot.py

정상 실행 시 아래 문구가 출력됩니다.

Marvis bot is running...

이후 Telegram 봇에게 메시지를 보내면 텍스트 답변과 음성 mp3 파일을 받을 수 있습니다.

systemd 서비스 등록

SSH 터미널을 닫아도 봇이 계속 실행되도록 systemd 서비스로 등록합니다.

1. 서비스 파일 생성

sudo nano /etc/systemd/system/marvis-bot.service

2. 서비스 설정 입력

[Unit]
Description=Marvis Telegram Gemini Bot
After=network.target
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/marvis-bot
Environment="PATH=/home/ubuntu/marvis-bot/venv/bin"
ExecStart=/home/ubuntu/marvis-bot/venv/bin/python /home/ubuntu/marvis-bot/bot.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target

3. 서비스 실행

sudo systemctl daemon-reload
sudo systemctl enable marvis-bot
sudo systemctl start marvis-bot

4. 상태 확인

sudo systemctl status marvis-bot

정상 상태:

Active: active (running)

상태 화면에서 나올 때는 q를 누릅니다.

자주 쓰는 명령어

상태 확인:

sudo systemctl status marvis-bot

재시작:

sudo systemctl restart marvis-bot

중지:

sudo systemctl stop marvis-bot

로그 확인:

journalctl -u marvis-bot -f

트러블슈팅

SSH 키 권한 오류

SSH 접속 시 키 권한 오류가 발생하면 아래 명령어를 실행합니다.

chmod 600 "/path/to/private-key.key"

이후 다시 접속합니다.

ssh -i "/path/to/private-key.key" ubuntu@your-server-ip

Gemini 모델 404 오류

초기 모델명으로 아래를 사용했을 때 오류가 발생했습니다.

model = genai.GenerativeModel("gemini-1.5-flash")

오류 내용:

404 models/gemini-1.5-flash is not found

해결 방법은 모델명을 아래처럼 변경하는 것이었습니다.

model = genai.GenerativeModel("gemini-2.5-flash")

google.generativeai FutureWarning

google.generativeai 패키지가 deprecated 예정이라는 경고가 출력될 수 있습니다.

현재는 경고일 뿐이며 봇은 정상 실행됩니다.
추후 google.genai SDK로 마이그레이션할 예정입니다.

비용 관련 주의사항

이 프로젝트는 무료 티어 사용을 기준으로 구성했습니다.

현재 확인한 상태:

* Oracle instance: VM.Standard.E2.1.Micro
* Gemini API: Free tier
* Google Cloud Billing: disabled / no active billing account
* Telegram Bot API: free
* gTTS library: free to use in this setup

주의사항:

* Google Cloud Billing을 활성화하지 않기
* Gemini API Key를 공개하지 않기
* Telegram Bot Token을 공개하지 않기
* .env 파일을 GitHub에 올리지 않기
* Oracle에서 Load Balancer, NAT Gateway, 유료 DB, 추가 Block Volume 같은 유료 리소스를 만들지 않기

현재 진행 상태

* Oracle 서버 생성 완료
* SSH 접속 완료
* Python 가상환경 생성 완료
* 필요한 패키지 설치 완료
* Telegram Bot Token 설정 완료
* Gemini API Key 설정 완료
* Gemini 2.5 Flash 모델로 정상 응답 확인
* 텍스트 답변 및 음성 mp3 전송 테스트 완료
* systemd 서비스 등록 완료
* 서비스 상태 active (running) 확인 완료

향후 개선 계획

* iPhone Shortcuts 연동
* 음성 입력 자동화
* 대화 히스토리 저장
* 일정 관리 기능 추가
* 아이디어 메모 기능 추가
* 더 자연스러운 TTS 적용
* google.generativeai에서 google.genai로 마이그레이션
* 로컬 미니 PC 환경으로 이전 테스트