#! /bin/sh

echo 0 > /sys/class/rtc/rtc0/wakealarm
echo $1 > /sys/class/rtc/rtc0/wakealarm

if [ "$2" -eq 1 ]; then
#  shutdown -h now "TVHManager shutdown the system"

  # Blocking:
  pm-suspend
fi

sleep 1
exit 0

