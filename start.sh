#!/bin/bash
# CHECKS Terminal - One-Click Starter

# 1. 포트 8000번을 쓰고 있는 이전 서버가 있다면 강제 종료
echo "🧹 기존 프로세스 정리 중..."
lsof -i :8000 -t | xargs kill -9 2>/dev/null

# 2. 가상환경 확인 및 실행
echo "🚀 서비스를 시작합니다..."
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# 3. 서버 실행
python3 server.py
