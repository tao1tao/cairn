#!/bin/bash
# 用法: bash push_to_github.sh <你的GitHub Token>
# 例如: bash push_to_github.sh ghp_xxxxxxxxxxxx

set -e

TOKEN="$1"
if [ -z "$TOKEN" ]; then
    echo "Usage: $0 <github-token>"
    echo "Token needs 'repo' scope (classic) or Contents:write (fine-grained)"
    exit 1
fi

# 初始化并推送
REPO_URL="https://tao1tao@github.com/tao1tao/cairn.git"

git init
git config user.email "user@cairn.local"
git config user.name "cairn-user"
git add -A
git commit -m "Initial commit: Cairn deploy with performance & bug fixes"
git remote add origin "https://tao1tao:$TOKEN@github.com/tao1tao/cairn.git"
git branch -M main
git push -u origin main

echo ""
echo "✅ 推送完成！"
echo "https://github.com/tao1tao/cairn"
