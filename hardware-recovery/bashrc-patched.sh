#! /bin/sh

NFS_SYSTEM=$(cat /proc/mounts | grep "/system nfs" | wc -l)

# 输出当前执行脚本名
echo -e "\033[0;33m$0\033[0m"

#mount_as_tmpfs	/etc/
mount_as_tmpfs	/var/

export LD_LIBRARY_PATH='/home/libs':'/home/sensor':$LD_LIBRARY_PATH

#echo 17 > /sys/class/gpio/export
#echo out > /sys/class/gpio/gpio17/direction
#echo 0 > /sys/class/gpio/gpio17/value

#echo ethSupport=1 > /etc/conf.d/debug_ctrl 

#echo A > /dev/watchdog
#userdata(config)
#mount -t jffs2 /dev/mtdblock4 /etc/conf.d/
# [ -d /etc/conf.d/jovision ] || mkdir /etc/conf.d/jovision
# [ -d /etc/conf.d/fixed ] || mkdir /etc/conf.d/fixed
# [ -d /etc/conf.d/recovery ] && rm -rf /etc/conf.d/recovery
#if [ ! -f /etc/conf.d/fixed/hwconfig.cfg ];then
#	if [ -f /etc/hwconfig.cfg ];then
#		cp /etc/hwconfig.cfg /etc/conf.d/fixed/
#	fi
#fi

#echo "256000" > /proc/sys/net/core/rmem_default	#500KB默认缓存
#echo "512000" > /proc/sys/net/core/rmem_max	#1000KB最大缓存
echo 163840 > /proc/sys/net/core/rmem_max
echo 4194304 > /proc/sys/net/core/wmem_max
echo 2097152 > /proc/sys/net/core/wmem_default
echo 163840 > /proc/sys/net/core/rmem_default
echo '924 1232 1848' > /proc/sys/net/ipv4/tcp_mem
echo '4096 87380 325120' > /proc/sys/net/ipv4/tcp_rmem
echo '4096 1048576 2097152' > /proc/sys/net/ipv4/tcp_wmem

#设置保留内存大小
echo 512 > /proc/sys/vm/min_free_kbytes
#设置释放内存临界值
#echo 1536 > /proc/sys/vm/extra_free_kbytes

echo 5 > /proc/sys/vm/dirty_background_ratio
echo 500 > /proc/sys/vm/dirty_expire_centisecs
echo 10 > /proc/sys/vm/dirty_ratio
echo 200 > /proc/sys/vm/dirty_writeback_centisecs
echo 500 > /proc/sys/vm/vfs_cache_pressure

#echo A > /dev/watchdog
#setMAC
# if [ $NFS_SYSTEM -eq 0 ];then
# 	if [ -f /etc/conf.d/jovision/network/mac.cfg ] ; then
# 		. /etc/conf.d/jovision/network/mac.cfg
# 	fi
# fi
#/usr/sbin/setMAC

check_return()
{
	if [ $? -ne 0 ] ;then
		echo err: $1
		# echo exit
		# exit
	fi
}
lsmod | grep "sinfo" > /dev/null
if [ $? -ne 0 ] ;then
	insmod /lib/modules/sinfo.ko
	check_return "insmod sinfo"
fi
echo 1 >/proc/jz/sinfo/info
check_return "start sinfo"

SENSOR_INFO=`cat /proc/jz/sinfo/info`
check_return "get sensor type"
echo SENSOR_INFO=${SENSOR_INFO}

#ISP_PARAM="isp_memopt=1 isp_clk=200000000 isp_clka=600000000 direct_mode=1 ivdc_mem_line=540 ivdc_threshold_line=540"
ISP_PARAM="isp_memopt=1 isp_clk=200000000 isp_clka=600000000"

SENSOR=${SENSOR_INFO#*:}

#if [ -f /etc/conf.d/jovision/sensor.sh ] ; then
#	. /etc/conf.d/jovision/sensor.sh
#else
#	echo SENSOR=$SENSOR > /etc/conf.d/jovision/sensor.sh
#fi

# lsmod | grep "tx_isp" > /dev/null
# if [ $? -ne 0 ] ;then
# 	insmod /lib/modules/tx-isp-t23.ko  ${ISP_PARAM}
# 	check_return "insmod isp drv"
# fi

# lsmod | grep "audio" > /dev/null
# if [ $? -ne 0 ] ;then
#         insmod /lib/modules/audio.ko spk_level=0
#         check_return "insmod audio"
# fi

# lsmod | grep ${SENSOR} > /dev/null
# if [ $? -ne 0 ] ;then
# 	insmod /lib/modules/sensor_${SENSOR}_t23.ko  #data_interface=1 sensor_max_fps=15
# 	# check_return "insmod sensor drv"
# fi

# echo 42 400 > /proc/jz/helix/param

# lsmod | grep motor > /dev/null
# if [ $? -ne 0 ] ;then
# 	insmod /lib/modules/sample_motor.ko
# 	check_return "insmod motor"
# fi

# #lsmod | grep exfat > /dev/null
# #if [ $? -ne 0 ] ;then
# #	insmod /lib/modules/exfat.ko
# #	check_return "insmod exfat"
# #fi
# #配置网络参数
# if [ $NFS_SYSTEM -eq 0 ];then
# 	/progs/networkcfg.sh
# fi

# #telnetd&
# #hwclock -s -u

# # cd /progs
# # #cp /progs/bin/mySystem /tmp/
# # #/tmp/mySystem &
# # ./bin/mySystem &

# ulimit -s 4096

# echo V > /dev/watchdog

# if [ -f /etc/conf.d/startup.sh ] ; then
#         /etc/conf.d/startup.sh &
#         exit
# fi


# cd /progs;
# ./startup.sh &

# MODULE_DIR=$(uname -r)
# mkdir -p /tmp/modules/${MODULE_DIR}
# mkdir -p /lib/modules
# cd /lib/modules/
# ln -s /tmp/modules/*

# echo 1 > /proc/sys/vm/overcommit_memory

#checck anticopy
SLEEPTIME=5

# if [ -f /system/bin/anticopy ]; then
# 	echo start anticopy ucamera ...
# 	SLEEPTIME=8
# 	cd /system/bin
# 	anticopy ucamera
# 	echo anticopy ucamera ok...
# fi

# Copy defaults only when absent.  The original unconditional copies
# consume JFFS2 space on every boot, while this firmware cannot reclaim it.
[ -f /etc/conf.d/uvc.attr ] || cp /system/config/uvc.attr /etc/conf.d/uvc.attr
[ -f /etc/conf.d/uvc2.attr ] || cp /system/config/uvc2.attr /etc/conf.d/uvc2.attr
[ -f /etc/conf.d/uvc.config ] || cp /system/config/uvc_dualstream.config /etc/conf.d/uvc.config
[ -f /etc/conf.d/uvc_dualstream.config ] || cp /system/config/uvc_dualstream.config /etc/conf.d/uvc_dualstream.config
[ -f /etc/conf.d/dev_config.cfg ] || cp /system/config/dev_config.cfg /etc/conf.d/dev_config.cfg

# SquashFS in-place patch padding; keep bashrc.sh length unchanged.                                                                                            
#insmod uvc drivers
# insmod /lib/modules/usbcamera.ko imd_enable=1
insmod /lib/modules/usbcamera.ko

#insmod isp drivers
insmod /lib/modules/tx-isp-t23.ko clka_name=mpll isp_clka=600000000 isp_clk=200000000 direct_mode=0 ivdc_mem_line=0 ivdc_threshold_line=0 print_level=2
insmod /lib/modules/sensor_gc1084_t23.ko shvflip=1
# insmod /lib/modules/audio.ko

# ucamera run
cd /home/progs/libs
libPath=$(pwd)
export LD_LIBRARY_PATH=$libPath:$LD_LIBRARY_PATH
echo "lib path:"${libPath}
# [ ! -f /etc/conf.d/ucamera ] && cp /home/progs/ucamera /etc/conf.d/ucamera
# [ ! -d /etc/conf.d/models ] && cp -rf /home/progs/models/ /etc/conf.d/
cd /home/progs/
# chmod 777 ucamera
env LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$libPath ./ucamera &


#hid_update run
sleep $SLEEPTIME
cd /bin
./hid_update &

cd /home/progs/
./monitor.sh ucamera &
