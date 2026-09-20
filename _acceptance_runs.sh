#!/usr/bin/env bash
# 一次性跑完 PRD §36.4 的四个验收案例。串行：它们共用同一个 sqlite 库与同一批 Provider
# 配额，并发跑既会互相抢配额，也会把日志搅在一起。
# PYTHONUNBUFFERED：日志要能边跑边看，否则重定向后要等进程退出才出现内容。
set -u
cd "D:/Documents/MyWorkSpace/travelplan" || exit 1
mkdir -p _acceptance

# 单实例锁：这个脚本被重复拉起过一次，两批 case 并行跑，Provider 限流被误当成故障。
# mkdir 是原子的，抢不到就退出，不再开第二批。
if ! mkdir _acceptance/.lock 2>/dev/null; then
  echo "another acceptance batch is already running (lock held) — abort"
  exit 1
fi
trap 'rmdir _acceptance/.lock 2>/dev/null' EXIT INT TERM

run() {
  local name="$1" message="$2"
  echo "=== $name started $(date +%H:%M:%S) ==="
  PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 D:/miniconda3/python.exe main.py \
    -m "$message" --user-id acceptance > "_acceptance/${name}.log" 2>&1
  echo "=== $name exit=$? $(date +%H:%M:%S) ==="
}

run bj_cd "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"
run sh_hz "10月2日从上海去杭州玩3天，两个人，预算3000，喜欢自然和拍照"
run cc_cd "10月1日从长春去成都玩6天，两个人，预算8000，喜欢美食和历史文化"
run gz_cq "10月3日从广州去重庆玩4天，三个人，预算7000，喜欢美食和夜景"

echo "ALL DONE"
