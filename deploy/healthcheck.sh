#!/usr/bin/env bash
# Перевірка, що трекер справді працює, а не просто «запущений».
#
#   sudo bash deploy/healthcheck.sh
#
# Свідомо без set -e: мета — пройти всі перевірки й показати повну картину,
# а не спинитись на першій проблемі.

SERVICE=spread-tracker
APP_DIR=/opt/spread-tracker
CSV="$APP_DIR/spread_log.csv"

G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; D=$'\e[2m'; B=$'\e[1m'; O=$'\e[0m'
bad=0

ok()   { echo "  ${G}ok${O}   $1 ${D}$2${O}"; }
warn() { echo "  ${Y}увага${O} $1 ${D}$2${O}"; }
err()  { echo "  ${R}ПРОБЛЕМА${O} $1 ${D}$2${O}"; bad=$((bad + 1)); }

echo "${B}Сервіс${O}"

state=$(systemctl is-active "$SERVICE" 2>/dev/null)
[[ $state == active ]] && ok "запущений" "($state)" || err "не запущений" "($state)"

since=$(systemctl show "$SERVICE" -p ActiveEnterTimestamp --value 2>/dev/null)
echo "  ${D}працює з $since${O}"

restarts=$(systemctl show "$SERVICE" -p NRestarts --value 2>/dev/null)
if [[ ${restarts:-0} -eq 0 ]]; then
  ok "перезапусків не було" ""
else
  warn "перезапусків: $restarts" "(мережа або збій — глянь лог нижче)"
fi

enabled=$(systemctl is-enabled "$SERVICE" 2>/dev/null)
[[ $enabled == enabled ]] && ok "автостарт увімкнено" "" \
  || err "автостарт вимкнено" "(після ребуту не підніметься)"

n=$(pgrep -cf "$APP_DIR/spread_tracker.py")
case "$n" in
  1) ok "процес один" "" ;;
  0) err "процесу немає" "" ;;
  *) err "процесів $n" "(дублікати шлють подвійні алерти — прибери зайві)" ;;
esac

mem=$(systemctl show "$SERVICE" -p MemoryCurrent --value 2>/dev/null)
if [[ $mem =~ ^[0-9]+$ ]]; then
  ok "пам'ять" "$((mem / 1024 / 1024)) МБ з 256 дозволених"
fi

echo
echo "${B}Заміри${O}"

ticks=$(journalctl -u "$SERVICE" --since "-3 min" -o cat --no-pager 2>/dev/null \
        | grep -cE '  (SOL|XRP)  spot ')
if   [[ $ticks -ge 8 ]]; then ok "тіків за 3 хв: $ticks" "(норма для 2 активів кожні 30 с)"
elif [[ $ticks -ge 2 ]]; then warn "тіків за 3 хв: $ticks" "(менше очікуваного ~12)"
else                          err "тіків за 3 хв: $ticks" "(заміри не йдуть)"
fi

for a in SOL XRP; do
  line=$(journalctl -u "$SERVICE" --since "-5 min" -o cat --no-pager 2>/dev/null \
         | grep -E "  $a  spot " | tail -1)
  [[ -n $line ]] && echo "  ${D}$a: ${line#*"$a"}${O}" \
    || err "$a не міряється за 5 хв" ""
done

echo
echo "${B}Помилки за останню годину${O}"

tb=$(journalctl -u "$SERVICE" --since "-1 hour" -o cat --no-pager 2>/dev/null | grep -c 'Traceback')
[[ $tb -eq 0 ]] && ok "виключень немає" "" || err "Traceback: $tb" "(див. journalctl нижче)"

neterr=$(journalctl -u "$SERVICE" --since "-1 hour" -o cat --no-pager 2>/dev/null \
         | grep -cE 'kyber:|binance|HTTP Error|URLError|немає маршруту')
if   [[ $neterr -eq 0 ]];  then ok "мережевих збоїв немає" ""
elif [[ $neterr -lt 10 ]]; then ok "мережевих збоїв: $neterr" "(поодинокі — це норма, є ретраї)"
else                            warn "мережевих збоїв: $neterr" "(багато — перевір зв'язок або ліміти)"
fi

tgerr=$(journalctl -u "$SERVICE" --since "-1 hour" -o cat --no-pager 2>/dev/null | grep -c 'telegram ')
[[ $tgerr -eq 0 ]] && ok "Telegram без помилок" "" \
  || err "помилок Telegram: $tgerr" "(перевір токен і chat id)"

echo
echo "${B}Історія${O}"

if [[ -f $CSV ]]; then
  rows=$(($(wc -l < "$CSV") - 1))
  ok "рядків у логу: $rows" "($CSV)"
  age=$(( $(date +%s) - $(stat -c %Y "$CSV") ))
  [[ $age -lt 120 ]] && ok "останній запис" "$age с тому" \
    || err "останній запис" "$age с тому — записи стали"
  alerts=$(awk -F, 'NR>1 && $NF==1' "$CSV" 2>/dev/null | wc -l)
  echo "  ${D}спрацювань порогу: $alerts${O}"
  echo "  ${D}останні заміри:${O}"
  tail -3 "$CSV" | awk -F, '{printf "    %s  %-3s  arb %+.2f%%  поріг %s%%\n", $1, $2, $9, $12}'
else
  err "немає $CSV" "(жоден замір не записався)"
fi

echo
echo "${B}Оточення${O}"

py=$(python3 --version 2>&1)
[[ $py =~ 3\.(9|1[0-9]) ]] && ok "$py" "" || warn "$py" "(треба 3.9+)"

perm=$(stat -c '%U:%G %a' /etc/spread-tracker.env 2>/dev/null)
[[ $perm == "root:tracker 640" ]] && ok "права на файл ключів" "$perm" \
  || warn "права на файл ключів" "$perm (очікувалось root:tracker 640)"

sync=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)
[[ $sync == yes ]] && ok "час синхронізовано" "" \
  || warn "час не синхронізовано" "(мітки в логу поїдуть)"

echo
if [[ $bad -eq 0 ]]; then
  echo "${G}${B}Усе гаразд${O}"
else
  echo "${R}${B}Проблем: $bad${O}  —  подробиці: journalctl -u $SERVICE -n 50 --no-pager"
  exit 1
fi
