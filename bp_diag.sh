#!/usr/bin/env bash
set -euo pipefail
echo "== 路径检查 =="
echo "- python3: $(which python3 || echo 'missing')"
echo "- script :"; ls -l /root/backpack_scalper.py || true
echo
echo "== ENV 检查 =="
for s in BTC_USDC_PERP ETH_USDC_PERP SOL_USDC_PERP; do
  echo "--- $s ---"
  if [ -f "/root/env/backpack-$s.env" ]; then
    echo "[OK] /root/env/backpack-$s.env"
    grep -E '^(SECRET_KEY|SYMBOL)=' "/root/env/backpack-$s.env" || echo "[WARN] 缺少 SECRET_KEY/SYMBOL"
  else
    echo "[MISS] /root/env/backpack-$s.env 不存在"
  fi
done
echo
echo "== 实例状态 =="
for s in BTC_USDC_PERP ETH_USDC_PERP SOL_USDC_PERP; do
  printf "%-18s : %s\n" "$s" "$(systemctl is-active backpack@$s 2>/dev/null || true)"
done
echo
echo "== 最近报错 (各 40 行) =="
journalctl -u backpack@BTC_USDC_PERP -n 40 --no-pager | tail -n +1
journalctl -u backpack@ETH_USDC_PERP -n 40 --no-pager | tail -n +1
journalctl -u backpack@SOL_USDC_PERP -n 40 --no-pager | tail -n +1
