# -*- coding: utf-8 -*-
import sys, os, stat, subprocess
import xbmc, xbmcaddon, xbmcgui, xbmcvfs
import time, datetime, random
import requests
import threading
from xml.dom import minidom
import smtplib
from email.message import Message
import resources.lib.tools as tools

release = tools.release()

__addon__ = xbmcaddon.Addon()
__version__ = __addon__.getAddonInfo('version')
__path__ = __addon__.getAddonInfo('path')
__LS__ = __addon__.getLocalizedString

# File for power off event
POWER_OFF_FILE = xbmcvfs.translatePath('special://temp/.pbc_poweroff')

# Script to be executed on resume (from suspend/hibernate)
RESUME_SCRIPT = xbmcvfs.translatePath('special://userdata/resume.py')

# Resume margin used (in seconds)
RESUME_MARGIN = 15

# (On)-Off-On margin to skip shutdown for upcoming scheduled recordings
# within OFF_ON_MARGIN minutes
OFF_ON_MARGIN = 5

# Amount of minutes idle after which we'll (auto) shutdown
IDLE_SHUTDOWN = 30

# Countdown time in seconds when idle timer expires and automatically shutting down
IDLE_COUNTDOWN_TIME = 10

SHUTDOWN_CMD = xbmcvfs.translatePath(os.path.join(__path__, 'resources', 'lib', 'shutdown.sh'))
EXTGRABBER = xbmcvfs.translatePath(os.path.join(__path__, 'resources', 'lib', 'epggrab_ext.sh'))

# set permissions for these files, this is required after installation or update
_sts = os.stat(SHUTDOWN_CMD)
if not (_sts.st_mode & stat.S_IEXEC):
    os.chmod(SHUTDOWN_CMD, _sts.st_mode | stat.S_IEXEC)

_stg = os.stat(EXTGRABBER)
if not (_stg.st_mode & stat.S_IEXEC):
    os.chmod(EXTGRABBER, _stg.st_mode | stat.S_IEXEC)

tools.writeLog('OS ID is %s' % (release.osid))

if ('libreelec' or 'openelec') in release.osid and tools.getAddonSetting('sudo', sType=tools.BOOL):
    __addon__.setSetting('sudo', 'false')
    tools.writeLog('OS is LibreELEC or OpenELEC, reset wrong setting \'sudo\' in options')

# binary Flags

isRES = 0b10000     # TVH PM has started by Resume on record/EPG
isNET = 0b01000     # Network is active
isPRG = 0b00100     # Programs/Processes are active
isREC = 0b00010     # Recording is or becomes active
isEPG = 0b00001     # EPG grabbing is active
isUSR = 0b00000     # User is active

class UserIdleThread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self._user_activity = False
        self._stop_event = threading.Event()

    def run(self):
        _idle_last = 0
        while not self._stop_event.isSet():
            self._stop_event.wait(0.01) # sleep 10 ms

            # User activity detected (=idle timer reset)?
            _idle = xbmc.getGlobalIdleTime()
            if _idle < _idle_last:
                self._user_activity = True
            _idle_last = _idle

    def stop(self, timeout=None):
        self._stop_event.set()
        threading.Thread.join(self, timeout)

    def IsUserActive(self, reset=True):
        if self._user_activity:
            if reset:
                self._user_activity = False
            return True
        return False


class Manager(object):

    def __init__(self):

        self.__xml = None
        self.__recTitles = []
        self.__wakeUp = None
        self.__wakeUpUT = None
        self.__wakeUpUTRec = None
        self.__wakeUpUTEpg = None
        self.__monitored_ports = ''
        self.__flags = isUSR
        self.__auto_mode_set = 0
        self.__auto_mode_counter = 0
        self.__dialog_pb = None
        self.rndProcNum = random.randint(1, 1024)
        self.hasPVR = None

        ### read addon settings

        self.__prerun = tools.getAddonSetting('margin_start', sType=tools.NUM)
        self.__postrun = tools.getAddonSetting('margin_stop', sType=tools.NUM)
        self.__shutdown = tools.getAddonSetting('shutdown_method', sType=tools.NUM)
        self.__sudo = 'sudo ' if tools.getAddonSetting('sudo', sType=tools.BOOL) else ''
        self.__counter = tools.getAddonSetting('notification_counter', sType=tools.NUM)
        self.__nextsched = tools.getAddonSetting('next_schedule', sType=tools.BOOL)

        # TVHeadend server
        self.__maxattempts = tools.getAddonSetting('conn_attempts', sType=tools.NUM)

        self.hasPVR = True
        try:
            __addonTVH__ = xbmcaddon.Addon('pvr.hts')
            self.__server = 'http://' + __addonTVH__.getSetting('host')
            self.__port = __addonTVH__.getSetting('http_port')
            self.__user = __addonTVH__.getSetting('user')
            self.__pass = __addonTVH__.getSetting('pass')
        except RuntimeError:
            tools.writeLog('Addon \'pvr.hts\' not installed or inactive', level=xbmc.LOGERROR)
            self.hasPVR = False

        # check if network activity has to observed
        self.__network = tools.getAddonSetting('network', sType=tools.BOOL)
        self.__monitored_ports = self.createwellformedlist('monitored_ports')

        # check if processes has to observed
        self.__pp_enabled = tools.getAddonSetting('postprocessor_enable', sType=tools.BOOL)
        self.__pp_list = self.createwellformedlist('processor_list')

        # mail settings
        self.__notification = tools.getAddonSetting('smtp_sendmail', sType=tools.BOOL)
        self.__smtpserver = tools.getAddonSetting('smtp_server')
        self.__smtpuser = tools.getAddonSetting('smtp_user')
        self.__smtppass = self.crypt('smtp_passwd', 'smtp_key', 'smtp_token')
        self.__smtpenc = tools.getAddonSetting('smtp_encryption')
        self.__smtpfrom = tools.getAddonSetting('smtp_from')
        self.__smtpto = tools.getAddonSetting('smtp_to')
        self.__charset = tools.getAddonSetting('charset')

        # EPG-Wakeup settings
        self.__epg_interval = tools.getAddonSetting('epgtimer_interval', sType=tools.NUM)
        self.__epg_time = tools.getAddonSetting('epgtimer_time', sType=tools.NUM)
        self.__epg_duration = tools.getAddonSetting('epgtimer_duration', sType=tools.NUM)
        self.__epg_grab_ext = tools.getAddonSetting('epg_grab_ext', sType=tools.BOOL)
        self.__epg_socket = xbmcvfs.translatePath(tools.getAddonSetting('epg_socket_path'))
        self.__epg_store = tools.getAddonSetting('store_epg', sType=tools.BOOL)
        self.__epg_path = xbmcvfs.translatePath(os.path.join(tools.getAddonSetting('epg_path'), 'epg.xml'))

        tools.writeLog('Settings loaded')

    @classmethod
    def createwellformedlist(cls, setting):

        ''' transform possible ugly userinput (e.g. 'p1, p2,,   p3 p4  ') to a shapely list '''
        return ' '.join(tools.getAddonSetting(setting).replace(',', ' ').split()).split()

    @classmethod
    def crypt(cls, pw, key, token):
        _pw = __addon__.getSetting(pw)
        if _pw == '' or _pw == '*':
            _key = __addon__.getSetting(key)
            _token = __addon__.getSetting(token)
            if len(_key) > 2:
                return "".join([chr(ord(_token[i]) ^ ord(_key[i])) for i in range(int(_key[-2:]))])
            return ''
        else:
            _key = ''
            for i in range((len(pw) / 16) + 1):
                _key += ('%016d' % int(random.random() * 10 ** 16))
            _key = _key[:-2] + ('%02d' % len(_pw))
            _tpw = _pw.ljust(len(_key), 'a')
            _token = "".join([chr(ord(_tpw[i]) ^ ord(_key[i])) for i in range(len(_key))])

            __addon__.setSetting(key, _key)
            __addon__.setSetting(token, _token)
            __addon__.setSetting(pw, '*')

            return _pw

        # send email to user to inform about a successful completition


    def deliverMail(self, message):
        if self.__notification:
            try:
                __port = {'None': 25, 'SSL/TLS': 465, 'STARTTLS': 587}
                __s_msg = Message()
                __s_msg.set_charset(self.__charset)
                __s_msg.set_payload(message, charset=self.__charset)
                __s_msg["Subject"] = __LS__(30046) % (release.hostname)
                __s_msg["From"] = self.__smtpfrom
                __s_msg["To"] = self.__smtpto

                if self.__smtpenc == 'STARTTLS':
                    __s_conn = smtplib.SMTP(self.__smtpserver, __port[self.__smtpenc])
                    __s_conn.ehlo()
                    __s_conn.starttls()
                elif self.__smtpenc == 'SSL/TLS':
                    __s_conn = smtplib.SMTP_SSL(self.__smtpserver, __port[self.__smtpenc])
                    __s_conn.ehlo()
                else:
                    __s_conn = smtplib.SMTP(self.__smtpserver, __port[self.__smtpenc])
                __s_conn.login(self.__smtpuser, self.__smtppass)
                __s_conn.sendmail(self.__smtpfrom, self.__smtpto, __s_msg.as_string())
                __s_conn.close()
                tools.writeLog('Mail delivered to %s.' % (self.__smtpto), level=xbmc.LOGINFO)
                return True
            except Exception as e:
                tools.writeLog('Mail could not be delivered. Check your settings.', level=xbmc.LOGERROR)
                tools.writeLog(e)
                return False
        else:
            tools.writeLog('"%s" completed, no Mail delivered.' % (message))
            return True

    # Connect to TVHeadend and establish connection (log in))

    def __getPvrStatusXML(self):
        _attempts = self.__maxattempts

        if not self.hasPVR:
            tools.writeLog('No HTS PVR client installed or inactive', level=xbmc.LOGERROR)
            tools.Notify().notify(__LS__(30030), __LS__(30032), icon=xbmcgui.NOTIFICATION_ERROR)
            self.__xml = None
            return False
        else:
            while self.hasPVR and _attempts > 0:
                # try DigestAuth as first, as this is the default auth on TVH > 3.9
                try:
                    conn = requests.get('%s:%s/status.xml' % (self.__server, self.__port), auth=requests.auth.HTTPDigestAuth(self.__user, self.__pass))
                    conn.close()
                    if conn.status_code == 200:
#                        tools.writeLog('Getting status.xml (Digest Auth)')
                        self.__xml = conn.content
                        return True
                    else:
                        # try BasicAuth as older method
                        conn = requests.get('%s:%s/status.xml' % (self.__server, self.__port), auth=requests.auth.HTTPBasicAuth(self.__user, self.__pass))
                        conn.close()
                        if conn.status_code == 200:
#                            tools.writeLog('Getting status.xml (Basic Auth)')
                            self.__xml = conn.content
                            return True

                    if conn.status_code == 401:
                        tools.writeLog('Unauthorized access (401)')
                        break
                except requests.ConnectionError:
                    _attempts -= 1
                    tools.writeLog('%s unreachable, remaining attempts: %s' % (self.__server, _attempts))
                    xbmc.sleep(5000)
                    continue

        tools.Notify().notify(__LS__(30030), __LS__(30031), icon=xbmcgui.NOTIFICATION_ERROR)
        self.__xml = None
        return False

    def readStatusXML(self, xmlnode):
        nodedata = []
        try:
            _xml = minidom.parseString(self.__xml)
            nodes = _xml.getElementsByTagName(xmlnode)
            for node in nodes:
                if node:
                    nodedata.append(node.childNodes[0].data)
            return nodedata
        except TypeError:
            tools.writeLog("Could not read XML tree from %s" % self.__server, level=xbmc.LOGERROR)
        return nodedata

    def __calcNextSched(self):
        self.__wakeUpUTRec = 0
        self.__wakeUpUTEpg = 0
        self.__wakeUpUT = 0
        self.__wakeUp = None

        __curTime = datetime.datetime.now()

        nodedata = self.readStatusXML('next')
        if nodedata:
            self.__wakeUp = (__curTime + datetime.timedelta(minutes=int(nodedata[0]) - self.__prerun)).replace(second=0)
            self.__wakeUpUTRec = int(time.mktime(self.__wakeUp.timetuple()))

        __wakeEPG = None
        if self.__epg_interval > 0:
            __dayDelta = self.__epg_interval
            if int(__curTime.strftime('%j')) % __dayDelta == 0:
                __dayDelta = 0
            __wakeEPG = (__curTime + datetime.timedelta(days=__dayDelta) -
                         datetime.timedelta(days=int(__curTime.strftime('%j')) % self.__epg_interval)).replace(hour=self.__epg_time, minute=0, second=0)
            if __curTime > __wakeEPG:
                __wakeEPG = __wakeEPG + datetime.timedelta(days=self.__epg_interval)
            self.__wakeUpUTEpg = int(time.mktime(__wakeEPG.timetuple()))

        # Calculate wakeup times
        if self.__wakeUpUTRec <= self.__wakeUpUTEpg:
            if self.__wakeUpUTRec > 0:
                self.__wakeUpUT = self.__wakeUpUTRec
            elif self.__wakeUpUTEpg > 0:
                self.__wakeUpUT = self.__wakeUpUTEpg
                self.__wakeUp = __wakeEPG
        else:
            if self.__wakeUpUTEpg > 0:
                self.__wakeUpUT = self.__wakeUpUTEpg
                self.__wakeUp = __wakeEPG
            elif self.__wakeUpUTRec > 0:
                self.__wakeUpUT = self.__wakeUpUTRec

    def updateSysState(self, Net=True, verbose=False):
        # Update status xml from tvh
        if not self.__getPvrStatusXML():
            return False

        # Reset flags
        self.__flags = isUSR

        # Check for current recordings. If there a 'status' tag,
        # and content is "Recording" current recording is in progress
        nodedata = self.readStatusXML('status')
        if nodedata and 'Recording' in nodedata:
            self.__flags |= isREC

        # Check for (future) recordings. If there is a 'next' tag a future recording comes up
        nodedata = self.readStatusXML('next')
        if nodedata:
            if int(nodedata[0]) <= (self.__prerun + self.__postrun + OFF_ON_MARGIN):
                # immediate
                self.__flags |= isREC

        __curTime = datetime.datetime.now()
        if self.__wakeUp and self.__wakeUp <= __curTime:
                self.__flags |= isRES # Resumed automatically

        # Check if actualizing EPG-Data
        if self.__epg_interval > 0:
            __dayDelta = self.__epg_interval
            if int(__curTime.strftime('%j')) % __dayDelta == 0:
                __dayDelta = 0

            __epgTime = (__curTime + datetime.timedelta(days=__dayDelta) -
                         datetime.timedelta(days=int(__curTime.strftime('%j')) % self.__epg_interval)).replace(hour=self.__epg_time, minute=0, second=0)
            if __epgTime <= __curTime <= __epgTime + datetime.timedelta(minutes=self.__epg_duration):
                self.__flags |= isEPG

        # Check if any watched process is running
        if self.__pp_enabled:
            for _proc in self.__pp_list:
                _pid = subprocess.Popen(['pidof', _proc], stdout=subprocess.PIPE)
                if _pid.stdout.read().strip():
                    self.__flags |= isPRG

        # Check for active network connection(s)
        if self.__network and Net:
            _port = ''
            for port in self.__monitored_ports:
                nwc = subprocess.Popen('netstat -an | grep -iE "(established|verbunden)" | grep -v "127.0.0.1" | grep ":%s "' % port, stdout=subprocess.PIPE, shell=True).communicate()
                nwc = nwc[0].strip()
                if nwc and len(nwc.split(b'\n')) > 0:
                    self.__flags |= isNET
                    _port += '%s, ' % (port)
            if _port:
                tools.writeLog('Network on port %s established and active' % (_port[:-2]))
        if verbose:
            tools.writeLog('Status flags: {0:05b} (RES/NET/PRG/REC/EPG)'.format(self.__flags))

        # Calculate new schedule value
        self.__calcNextSched()

        return True

    def enableAutoMode(self):
        if self.__dialog_pb is None:
            self.__dialog_pb = xbmcgui.DialogProgressBG()
            self.__dialog_pb.create(__LS__(30010), "")
        self.__auto_mode_set = IDLE_COUNTDOWN_TIME
        self.__auto_mode_counter = 0
        xbmc.executebuiltin('InhibitScreensaver(true)')

    def disableAutoMode(self):
        if not self.__dialog_pb is None:
            self.__dialog_pb.close()
            self.__dialog_pb = None
        self.__auto_mode_set = 0
        self.__auto_mode_counter = 0
        xbmc.executebuiltin('InhibitScreensaver(false)')

    def updateAutoModeDialog(self):
        if not self.__dialog_pb is None:
            if self.__auto_mode_counter == 0:
                self.disableScreensaver()
                tools.writeLog('Display countdown dialog for %s secs' % (self.__auto_mode_set))
                if xbmc.getCondVisibility('VideoPlayer.isFullscreen'):
                    tools.writeLog('Countdown possibly invisible (fullscreen mode)')
                    tools.writeLog('Showing additional notification')
                    tools.Notify().notify(__LS__(30010), __LS__(30011) % (self.__auto_mode_set))

            # actualize progressbar
            if self.__auto_mode_counter < self.__auto_mode_set:
                __percent = int(self.__auto_mode_counter * 100 / self.__auto_mode_set)
                self.__dialog_pb.update(__percent, __LS__(30010), __LS__(30011) % (self.__auto_mode_set - self.__auto_mode_counter))

                self.__auto_mode_counter += 1
            if self.__auto_mode_counter == self.__auto_mode_set:
                return True
        return False

    @staticmethod
    def disableScreensaver():
        # deactivate screensaver (send key select)
        if xbmc.getCondVisibility('System.ScreenSaverActive'):
            query = {
                "method": "Input.Select"
            }
            tools.jsonrpc(query)

    @staticmethod
    def setPowerOffEvent():
        # Create notification file
        try:
            open(POWER_OFF_FILE, 'w').close()
            return True
        except IOError:
            tools.writeLog('Unable to create power off file %s' % POWER_OFF_FILE, level=xbmc.LOGERROR)
            return False

    @staticmethod
    def getPowerOffEvent(remove=True):
        if os.path.isfile(POWER_OFF_FILE):
            if remove:
                try:
                    os.remove(POWER_OFF_FILE)
                except OSError:
                    tools.writeLog('Unable to remove power off file %s' % POWER_OFF_FILE, level=xbmc.LOGERROR)
                    return False
            return True

        return False # No event

    def setWakeup(self, shutdown=True):
        if not self.__wakeUpUT:
            tools.writeLog('No recordings or EPG update to schedule')
        elif self.__wakeUpUT == self.__wakeUpUTRec:
            tools.writeLog('Recording wake-up time: %s' % (self.__wakeUp.strftime('%d.%m.%y %H:%M')))
        elif self.__wakeUpUT == self.__wakeUpUTEpg:
            tools.writeLog('EPG update wake-up time: %s' % (self.__wakeUp.strftime('%d.%m.%y %H:%M')))

        tools.writeLog('Wake-up Unix time: %s' % (self.__wakeUpUT), xbmc.LOGINFO)
#        tools.writeLog('Flags before shutdown are: {0:05b}'.format(self.__flags))

        if shutdown:
            # Show notifications
            if self.__nextsched:
                if self.__wakeUpUT == self.__wakeUpUTRec:
                    tools.Notify().notify(__LS__(30017), __LS__(30018) % (self.__wakeUp.strftime('%d.%m.%Y %H:%M')))
                elif self.__wakeUpUT == self.__wakeUpUTEpg:
                    tools.Notify().notify(__LS__(30017), __LS__(30019) % (self.__wakeUp.strftime('%d.%m.%Y %H:%M')))
                else:
                    tools.Notify().notify(__LS__(30010), __LS__(30014))

            if xbmc.getCondVisibility('Player.Playing') or xbmc.getCondVisibility('Player.Paused'):
                tools.writeLog('Stopping Player')
                xbmc.Player().stop()

            tools.writeLog('Instruct the system to shut down using %s' % ('Application' if self.__shutdown == 0 else 'OS'), xbmc.LOGINFO)
            os.system('%s%s %s %s' % (self.__sudo, SHUTDOWN_CMD, self.__wakeUpUT, self.__shutdown))
            if self.__shutdown == 0:
                xbmc.shutdown()
            xbmc.sleep(1000)
            return True
        else:
            os.system('%s%s %s %s' % (self.__sudo, SHUTDOWN_CMD, self.__wakeUpUT, 0))

        return False

    def checkOutdatedRecordings(self, mode):
        nodedata = self.readStatusXML('title')
        for item in nodedata:
            if not item in self.__recTitles:
                self.__recTitles.append(item)
                tools.writeLog('Recording of "%s" is active' % (item))
        for item in self.__recTitles:
            if not item in nodedata:
                self.__recTitles.remove(item)
                tools.writeLog('Recording of "%s" has finished' % (item))
                if mode is None:
                    self.deliverMail(__LS__(30047) % (release.hostname, item))

    ####################################### START MAIN SERVICE #####################################

    def start(self, mode=None):
        tools.writeLog('Starting with id:%s@mode:%s' % (self.rndProcNum, mode))
        # reset RTC
        #os.system('%s%s %s %s' % (self.__sudo, SHUTDOWN_CMD, 0, 0))
        #tools.writeLog('Reset RTC')

        if mode == 'CHECKMAILSETTINGS':
            if self.deliverMail(__LS__(30065) % (release.hostname)):
                tools.dialogOK(__LS__(30066), __LS__(30068) % (self.__smtpto))
            else:
                tools.dialogOK(__LS__(30067), __LS__(30069) % (self.__smtpto))
            return
        elif mode == 'POWEROFF':
            tools.Notify().notify(__LS__(30010), __LS__(30013))
            tools.writeLog('Poweroff command received', level=xbmc.LOGINFO)

            # Notify service loop of power off event
            self.setPowerOffEvent()
            return
        elif not mode == None:
            tools.writeLog('Unknown parameter %s' % (mode), level=xbmc.LOGFATAL)
            return

        ### START SERVICE LOOP ###
        tools.writeLog('Starting service', level=xbmc.LOGINFO)

        idle_timer = 0
        wake_up_last = 0
        resume_last = 0
        resumed = False
        first_start = True
        power_off = False

        mon = xbmc.Monitor()
        uit = UserIdleThread()
        uit.start()

        while (1):
            if resumed or first_start:
                # Get updated system state (and store new status)
                self.updateSysState(verbose=True)

                # Check if we resumed automatically
                if self.__flags:
                    self.enableAutoMode()
                    tools.writeLog('Wakeup in automode', level=xbmc.LOGINFO)

                    if (self.__flags & isEPG) and self.__epg_grab_ext and os.path.isfile(EXTGRABBER):
                        tools.writeLog('Starting script for grabbing external EPG')
                        #
                        # ToDo: implement startup of external script (epg grabbing)
                        #
                        _epgpath = self.__epg_path
                        if self.__epg_store and _epgpath == '':
                            _epgpath = '/dev/null'
                        _start = datetime.datetime.now()
                        try:
                            _comm = subprocess.Popen('%s %s %s' % (EXTGRABBER, _epgpath, self.__epg_socket),
                                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True, universal_newlines=True)
                            while _comm.poll() is None:
                                tools.writeLog(_comm.stdout.readline().decode('utf-8', 'ignore').strip())

                            tools.writeLog('external EPG grabber script took %s seconds' % ((datetime.datetime.now() - _start).seconds))
                        except Exception:
                            tools.writeLog('Could not start external EPG grabber script', level=xbmc.LOGERROR)

                if resumed and os.path.isfile(RESUME_SCRIPT):
                    _user_idle = not uit.IsUserActive(False)
                    xbmc.executebuiltin("RunScript(%s, %s, %s)" % (RESUME_SCRIPT, int(self.__auto_mode_set), int(_user_idle)))

                # Reset flags
                #############
                first_start = False
                resumed = False
                wake_up_last = 0    # Force update of wakeup time, just in case

            # Update wake time, in case a new value is set
            # NOTE: We keep doing this (instead of only on powerdown),
            #       just in case Kodi crashes/freezes
            if not self.__wakeUpUT == wake_up_last:
                wake_up_last = self.__wakeUpUT
                self.setWakeup(shutdown=False)

            # Check outdated recordings
            self.checkOutdatedRecordings(mode)

            # 1 Minute wait loop
            wait_count = 0
            SLOW_CYCLE = 60
            while wait_count < SLOW_CYCLE:
                wait_count += 1

                if mon.waitForAbort(1):
                    tools.writeLog('Service with id %s aborted' % (self.rndProcNum), level=xbmc.LOGINFO)
                    return

                # User activity detected?
                if uit.IsUserActive():
                    idle_timer = 0
                    if self.__auto_mode_set:
                        tools.writeLog('User interaction detected, disabling automode')
                        self.disableAutoMode()

                # Update countdown dialog (if any)
                if not self.__flags & (isREC | isEPG | isPRG | isNET):
                    if self.updateAutoModeDialog():
                        power_off = True  # Countdown reached 0
                        break             # Break loop so we can power off

                # Check if power off event was set
                if self.getPowerOffEvent():
                    tools.writeLog('Poweroff request detected by service')
                    if int(time.time()) < resume_last + RESUME_MARGIN:
                        tools.writeLog('Not enough time passed since last power up, skipping poweroff')
                    else:
                        if xbmc.getCondVisibility('Player.Playing') or xbmc.getCondVisibility('Player.Paused'):
                            tools.writeLog('Stopping Player')
                            xbmc.Player().stop()

                        # Make sure system state is updated
                        self.updateSysState(verbose=True)

                        if (self.__flags & isREC):
                            tools.Notify().notify(__LS__(30015), __LS__(30020), icon=xbmcgui.NOTIFICATION_WARNING)  # Notify 'Recording in progress'
                            tools.writeLog('Recording in progress: Postponing poweroff with automode', level=xbmc.LOGINFO)
                            self.enableAutoMode()
                        elif (self.__flags & isEPG):
                            tools.Notify().notify(__LS__(30015), __LS__(30021), icon=xbmcgui.NOTIFICATION_WARNING)  # Notify 'EPG-Update'
                            tools.writeLog('EPG-update in progress: Postponing poweroff with automode', level=xbmc.LOGINFO)
                            self.enableAutoMode()
                        elif (self.__flags & isPRG):
                            tools.Notify().notify(__LS__(30015), __LS__(30022), icon=xbmcgui.NOTIFICATION_WARNING)  # Notify 'Postprocessing'
                            tools.writeLog('Postprocessing in progress: Postponing poweroff with automode', level=xbmc.LOGINFO)
                            self.enableAutoMode()
                        elif (self.__flags & isNET):
                            tools.Notify().notify(__LS__(30015), __LS__(30023), icon=xbmcgui.NOTIFICATION_WARNING)  # Notify 'Network active'
                            tools.writeLog('Network active: Postponing poweroff with automode', level=xbmc.LOGINFO)
                            self.enableAutoMode()
                        else:
                            power_off = True
                            break # Break wait loop so we can perform power off
            # Wait loop ends

            if not power_off:
                # Get updated system state (and store new status)
                self.updateSysState(verbose=True)

                # When flags are set, make sure we don't automatically shutdown
                # and prevent the screensaver from starting
                if self.__flags & (isREC | isEPG | isPRG | isNET):
                    xbmc.executebuiltin('InhibitIdleShutdown(true)')
                    #self.disableScreensaver() # Doesn't work as intended

                    # (Re)set idle timer
                    idle_timer = 0
                else:
                    xbmc.executebuiltin('InhibitIdleShutdown(false)')

                    # Auto shutdown handling
                    if xbmc.getCondVisibility('Player.Playing') or self.__auto_mode_set:
                        tools.writeLog('Player is playing or automode set, resetting idle timer')
                        idle_timer = 0
                    else:
                        idle_timer += 1
                        tools.writeLog('No user activity for %s minutes' % idle_timer)
                        if idle_timer > IDLE_SHUTDOWN:
                            tools.writeLog('No user activity detected for %s minutes. Powering down' % idle_timer)
                            idle_timer = 0    # In case powerdown is aborted by user
                            self.enableAutoMode()  # Enable auto-mode (also for countdown dialog)

            if power_off:
                # Set power off event. This is in case suspend in the shutdown script fails,
                # as a fallback it will then reboot and immediately power off
                self.setPowerOffEvent()

                # Always disable automode (+close dialog)
                self.disableAutoMode()

                # Set RTC wakeup + suspend system:
                # NOTE: setWakeup() will block when the system suspends
                #       and continue as soon as it resumes again
                if self.setWakeup():
                    resumed = True                    # Notify next iteration we have resumed from suspend
                    uit.IsUserActive()                # Reset user active event
                    resume_last = int(time.time())    # Save resume time for later use
                    self.getPowerOffEvent()           # Reset power off event
                    tools.writeLog('Resume point passed', level=xbmc.LOGINFO)

                power_off = False       # Reset power off flag

        ### END MAIN LOOP ###
        uit.stop() # Stop user idle thread

        ##################################### END OF MAIN SERVICE #####################################

if __name__ == '__main__':
    mode = None

    try:
        mode = sys.argv[1].upper()
    except IndexError:
        # Start without arguments (i.e. login|startup|restart)
        pass

    TVHMan = Manager()
    TVHMan.start(mode)
    tools.writeLog('Service with id %s (V.%s on %s) kicks off' % (TVHMan.rndProcNum, __version__, release.hostname), level=xbmc.LOGINFO)
    del TVHMan
