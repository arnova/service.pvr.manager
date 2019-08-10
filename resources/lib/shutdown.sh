#! /bin/sh

echo 0 > /sys/class/rtc/rtc0/wakealarm
echo $1 > /sys/class/rtc/rtc0/wakealarm

if [ "$2" -eq 1 ]; then
#  shutdown -h now "TVHManager shutdown the system"

  # Note: pm-suspend is a blocking call
  pm-suspend
  if [ $? -ne 0 ]; then
    # Hack in case pm-suspend fails: reboot and try again
    reboot
  fi
fi

sleep 1

exit 0
