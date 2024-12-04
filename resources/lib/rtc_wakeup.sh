#! /bin/sh

RTC_FILE="/home/kodi/.kodi/temp/.rtc_wakeup"

# Override RTC file?
if [ -n "$1" ]; then
  RTC_FILE="$1"
fi

echo 0 > /sys/class/rtc/rtc0/wakealarm

WAKE_TIME="$(cat "$RTC_FILE")"
if [ $? -eq 0 -a -n "$WAKE_TIME" ]; then
  echo $WAKE_TIME > /sys/class/rtc/rtc0/wakealarm
  exit 0
fi

echo "ERROR: RTC wakeup set failed for \"$WAKE_TIME\"" >&2
exit 1