#!/usr/bin/env -S python3 -u
#-u to unbuffer output. Otherwise when calling with nohup or redirecting output things are printed very lately or would even mixup

print("---------------------------------------------")
print("MiTemperature2 / ATC Thermometer version 5.0")
print("---------------------------------------------")

readme="""

Please read README.md in this folder. Latest version is available at https://github.com/JsBergbau/MiTemperature2#readme
This file explains very detailed about the usage and covers everything you need to know as user.

"""

print(readme)


import argparse
import os
import re
from dataclasses import dataclass
import threading
import time
import signal
import math
import logging
import json
import ssl


@dataclass
class Measurement:
	temperature: float
	humidity: int
	voltage: float
	calibratedHumidity: int = 0
	battery: int = 0
	timestamp: int = 0
	sensorname: str	= ""
	rssi: int = 0 

	def __eq__(self, other): #rssi may be different, so exclude it from comparison
		if self.temperature == other.temperature and self.humidity == other.humidity and self.calibratedHumidity == other.calibratedHumidity and self.battery == other.battery and self.sensorname == other.sensorname:
			#in passive mode also exclude voltage as it changes often due to frequent measurements
			return True if args.passive else (self.voltage == other.voltage)
		else:
			return False

previousMeasurements={}
previousCallbacks={}
identicalCounters={}
MQTTClient=None
MQTTTopic=None
receiver=None
subtopics=None
mqttJSONDisabled=False

def myMQTTPublish(topic,jsonMessage):
	global subtopics
	if len(subtopics) > 0:
		messageDict = json.loads(jsonMessage)
		for subtopic in subtopics:
			logging.debug("Topic:" + subtopic)
			MQTTClient.publish(topic + "/" + subtopic,messageDict[subtopic],0)
	if not mqttJSONDisabled:
		MQTTClient.publish(topic,jsonMessage,1)


def signal_handler(sig, frame):
	if args.passive:
		disable_le_scan(sock)	
	os._exit(0)
		
def watchDog_Thread():
	global unconnectedTime
	global connected
	global pid
	while True:
		logging.debug("watchdog_Thread")
		logging.debug("unconnectedTime : " + str(unconnectedTime))
		logging.debug("connected : " + str(connected))
		logging.debug("pid : " + str(pid))
		now = int(time.time())
		if (unconnectedTime is not None) and ((now - unconnectedTime) > 60): #could also check connected is False, but this is more fault proof
			pstree=os.popen("pstree -p " + str(pid)).read() #we want to kill only bluepy from our own process tree, because other python scripts have there own bluepy-helper process
			logging.debug("PSTree: " + pstree)
			try:
				bluepypid=re.findall(r'bluepy-helper\((.*)\)',pstree)[0] #Store the bluepypid, to kill it later
			except IndexError: #Should not happen since we're now connected
				logging.debug("Couldn't find pid of bluepy-helper")
			os.system("kill " + bluepypid)
			logging.debug("Killed bluepy with pid: " + str(bluepypid))
			unconnectedTime = now #reset unconnectedTime to prevent multiple killings in a row
		time.sleep(5)
	
sock = None #from ATC 
lastBLEPacketReceived = 0
BLERestartCounter = 1
def keepingLEScanRunning(): #LE-Scanning gets disabled sometimes, especially if you have a lot of BLE connections, this thread periodically enables BLE scanning again
	global BLERestartCounter
	while True:
		time.sleep(1)
		now = time.time()
		if now - lastBLEPacketReceived > args.watchdogtimer:
			print("Watchdog: Did not receive any BLE packet within", int(now - lastBLEPacketReceived), "s. Restarting BLE scan. Count:", BLERestartCounter)
			disable_le_scan(sock)
			enable_le_scan(sock, filter_duplicates=False)
			BLERestartCounter += 1
			print("")
			time.sleep(5) #give some time to take effect


def calibrateHumidity2Points(humidity, offset1, offset2, calpoint1, calpoint2):
	#offset1=args.offset1
	#offset2=args.offset2
	#p1y=args.calpoint1
	#p2y=args.calpoint2
	p1y=calpoint1
	p2y=calpoint2
	p1x=p1y - offset1
	p2x=p2y - offset2
	m = (p1y - p2y) * 1.0 / (p1x - p2x) # y=mx+b
	#b = (p1x * p2y - p2x * p1y) * 1.0 / (p1y - p2y)
	b = p2y - m * p2x #would be more efficient to do these calculations only once
	humidityCalibrated=m*humidity + b
	if (humidityCalibrated > 100 ): #with correct calibration this should not happen
		humidityCalibrated = 100
	elif (humidityCalibrated < 0):
		humidityCalibrated = 0
	humidityCalibrated=int(round(humidityCalibrated,0))
	return humidityCalibrated


mode="round"
# Initialisation  -------

def buildJSONString(measurement):
	jsonstr = '{"temperature": ' + str(measurement.temperature) + ', "humidity": ' + str(measurement.humidity) + ', "voltage": ' + str(measurement.voltage) \
		+ ', "calibratedHumidity": ' + str(measurement.calibratedHumidity) + ', "battery": ' + str(measurement.battery) \
		+ ', "timestamp": '+ str(measurement.timestamp) +', "sensor": "' + measurement.sensorname + '", "rssi": ' + str(measurement.rssi) \
		+ ', "receiver": "' + receiver  + '"}'
	return jsonstr

def MQTTOnConnect(client, userdata, flags, rc):
    logging.info("MQTT connected with result code "+str(rc))

def MQTTOnPublish(client,userdata,mid):
	logging.debug("MQTT published client: %s, userdata: %s, mid: %s")

def MQTTOnDisconnect(client, userdata,rc):
	logging.info("MQTT disconnected client: %s, userdata: %s, rc: %s",client, userdata, rc)	

# Main loop --------
parser=argparse.ArgumentParser(allow_abbrev=False,epilog=readme)
parser.add_argument("--battery","-b", help="Get estimated battery level, in passive mode: Get battery level from device", metavar='', type=int, nargs='?', const=1)
parser.add_argument("--mqttconfigfile","-mcf", help="specify a configurationfile for MQTT-Broker")
parser.add_argument("--log","-l", help="specify loglevel")

passivegroup = parser.add_argument_group("Passive mode related arguments")
passivegroup.add_argument("--passive","-p","--atc","-a", help="Read the data of devices based on BLE advertisements, use --battery to get battery level additionaly in percent",action='store_true')
passivegroup.add_argument("--watchdogtimer","-wdt",metavar='X', type=int, help="Re-enable scanning after not receiving any BLE packet after X seconds")
passivegroup.add_argument("--devicelistfile","-df",help="Specify a device list file giving further details to devices")
passivegroup.add_argument("--onlydevicelist","-odl", help="Only read devices which are in the device list file",action='store_true')
passivegroup.add_argument("--rssi","-rs", help="Report RSSI via callback",action='store_true')

args=parser.parse_args()

if args.log:
	numeric_level = int(args.log)
	if not isinstance(numeric_level, int):
 	   raise ValueError('Invalid log level: %s' % args.log)
	logging.basicConfig(level=numeric_level)

if args.devicelistfile or args.mqttconfigfile:
	import configparser

if args.mqttconfigfile:
	try:
		import paho.mqtt.client as mqtt
	except:
		logging.critical("Please install MQTT-Library via 'pip/pip3 install paho-mqtt'")
		exit(1)
	if not os.path.exists(args.mqttconfigfile):
		logging.critical("Error MQTT config file '"+args.mqttconfigfile+"' not found")
		os._exit(1)
	mqttConfig = configparser.ConfigParser()
	# print(mqttConfig.sections())
	mqttConfig.read(args.mqttconfigfile)
	broker = mqttConfig["MQTT"]["broker"]
	port = int(mqttConfig["MQTT"]["port"])

	# MQTTS parameters
	tls = int(mqttConfig["MQTT"]["tls"]) if "tls" in mqttConfig["MQTT"] else 0
	if tls != 0:
		cacerts = mqttConfig["MQTT"]["cacerts"] if mqttConfig["MQTT"]["cacerts"] else None
		certificate = mqttConfig["MQTT"]["certificate"] if mqttConfig["MQTT"]["certificate"] else None
		certificate_key = mqttConfig["MQTT"]["certificate_key"] if mqttConfig["MQTT"]["certificate_key"] else None
		insecure = int(mqttConfig["MQTT"]["insecure"])
	username = mqttConfig["MQTT"]["username"]
	password = mqttConfig["MQTT"]["password"]
	MQTTTopic = mqttConfig["MQTT"]["topic"]
	lastwill = mqttConfig["MQTT"]["lastwill"]
	lwt = mqttConfig["MQTT"]["lwt"]
	clientid=mqttConfig["MQTT"]["clientid"]
	receiver=mqttConfig["MQTT"]["receivername"]
	subtopics=mqttConfig["MQTT"]["subtopics"]
	if len(subtopics) > 0:
		subtopics=subtopics.split(",")
		if "nojson" in subtopics:
			subtopics.remove("nojson")
			mqttJSONDisabled=True

	if len(receiver) == 0:
		import socket
		receiver=socket.gethostname()

	client = mqtt.Client(clientid)
	client.on_connect = MQTTOnConnect
	client.on_publish = MQTTOnPublish
	client.on_disconnect = MQTTOnDisconnect
	client.reconnect_delay_set(min_delay=1,max_delay=60)
	client.loop_start()
	client.username_pw_set(username,password)
	if len(lwt) > 0:
		logging.debug("Using lastwill with topic:"+lwt+"and message:"+lastwill)
		client.will_set(lwt,lastwill,qos=1)
	# MQTTS parameters
	if tls:
		client.tls_set(cacerts, certificate, certificate_key, cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS, ciphers=None)
		client.tls_insecure_set(insecure)
	
	client.connect_async(broker,port)
	MQTTClient=client
	
if not args.passive:
	parser.print_help()
	os._exit(1)

signal.signal(signal.SIGINT, signal_handler)	

if args.passive:
	logging.info("""Script started in passive mode
	------------------------------
	In this mode all devices within reach are read out, unless a devicelistfile and --onlydevicelist is specified.
	Also --name Argument is ignored, if you require names, please use --devicelistfile.
	In this mode debouncing is not available. Rounding option will round humidity and temperature to one decimal place.
	Passive mode usually requires root rights. If you want to use it with normal user rights,
	please execute \"sudo setcap cap_net_raw,cap_net_admin+eip $(eval readlink -f `which python3`)\"
	You have to redo this step if you upgrade your python version.
	----------------------------""")

	import sys
	import bluetooth._bluetooth as bluez

	from bluetooth_utils import (toggle_device,
								enable_le_scan, parse_le_advertising_events,
								disable_le_scan, raw_packet_to_str)

	advCounter=dict()
	#encryptedPacketStore=dict()
	sensors = dict()
	if args.devicelistfile:
		#import configparser
		if not os.path.exists(args.devicelistfile):
			logging.error("Error: specified device list file '"+args.devicelistfile+"' not found")
			os._exit(1)
		sensors = configparser.ConfigParser()
		sensors.read(args.devicelistfile)
		#Convert macs in devicelist file to Uppercase
		sensorsnew={}
		for key in sensors:
			sensorsnew[key.upper()] = sensors[key]
		sensors = sensorsnew

		#loop through sensors to generate key
		sensorsnew=sensors
		for sensor in sensors:
			if "decryption" in sensors[sensor]:
				if sensors[sensor]["decryption"][0] == "k":
					sensorsnew[sensor]["key"] = sensors[sensor]["decryption"][1:]
					#print(sensorsnew[sensor]["key"])
		sensors = sensorsnew

	dev_id = 0 # the bluetooth device is hci0
	toggle_device(dev_id, True)
	
	try:
		sock = bluez.hci_open_dev(dev_id)
	except:
		logging.critical("Error: cannot open bluetooth device %i", dev_id)
		raise

	enable_le_scan(sock, filter_duplicates=False)

	try:
		prev_data = None

		def decode_data_atc(mac, adv_type, data_str, rssi, measurement):
			preeamble = "161a18"
			packetStart = data_str.find(preeamble)
			if (packetStart == -1):
				return
			offset = packetStart + len(preeamble)
			strippedData_str = data_str[offset:offset+26] #if shorter will just be shorter then 13 Bytes
			strippedData_str = data_str[offset:] #if shorter will just be shorter then 13 Bytes
			macStr = mac.replace(":","").upper()
			dataIdentifier = data_str[(offset-4):offset].upper()

			batteryVoltage=None

			if(dataIdentifier == "1A18") and not args.onlydevicelist or (dataIdentifier == "1A18" and mac in sensors) and (len(strippedData_str) in (16, 22, 26, 30)): #only Data from ATC devices
				if len(strippedData_str) == 30: #custom format, next-to-last ist adv number
					advNumber = strippedData_str[-4:-2]
				else:
					advNumber = strippedData_str[-2:] #last data in packet is adv number
				if macStr in advCounter:
					lastAdvNumber = advCounter[macStr]
				else:
					lastAdvNumber = None
				if lastAdvNumber == None or lastAdvNumber != advNumber:

					if len(strippedData_str) == 26: #ATC1441 Format
						logging.debug("BLE packet - ATC1441: %s %02x %s %d", mac, adv_type, data_str, rssi)
						advCounter[macStr] = advNumber
						#temperature = int(data_str[12:16],16) / 10.    # this method fails for negative temperatures
						temperature = int.from_bytes(bytearray.fromhex(strippedData_str[12:16]),byteorder='big',signed=True) / 10.
						humidity = int(strippedData_str[16:18], 16)
						batteryVoltage = int(strippedData_str[20:24], 16) / 1000
						batteryPercent = int(strippedData_str[18:20], 16)

					else: #no fitting packet
						return

				else: #Packet is just repeated
					return

				measurement.battery = batteryPercent
				measurement.humidity = humidity
				measurement.temperature = temperature
				measurement.voltage = batteryVoltage if batteryVoltage != None else 0
				measurement.rssi = rssi
				return measurement

		def le_advertise_packet_handler(mac, adv_type, data, rssi):
			global lastBLEPacketReceived
			if args.watchdogtimer:
				lastBLEPacketReceived = time.time()
			lastBLEPacketReceived = time.time()
			data_str = raw_packet_to_str(data)

			measurement = Measurement(0,0,0,0,0,0,0,0)
			measurement = decode_data_atc(mac, adv_type, data_str, rssi, measurement)

			if measurement:
				measurement.timestamp = int(time.time())

				if mac in sensors and "sensorname" in sensors[mac]:
					measurement.sensorname = sensors[mac]["sensorname"]

				logging.info("Measurement %s %s %fC %f%%", mac, measurement.sensorname, measurement.temperature, measurement.humidity)
				
				currentMQTTTopic = MQTTTopic
				if mac in sensors:
					try:
						measurement.sensorname = sensors[mac]["sensorname"]
					except:
						measurement.sensorname = mac
					if "offset1" in sensors[mac] and "offset2" in sensors[mac] and "calpoint1" in sensors[mac] and "calpoint2" in sensors[mac]:
						measurement.humidity = calibrateHumidity2Points(measurement.humidity,int(sensors[mac]["offset1"]),int(sensors[mac]["offset2"]),int(sensors[mac]["calpoint1"]),int(sensors[mac]["calpoint2"]))
						logging.debug ("Humidity calibrated (2 points calibration): "+ measurement.humidity)
					elif "humidityOffset" in sensors[mac]:
						measurement.humidity = measurement.humidity + int(sensors[mac]["humidityOffset"])
						logging.debug ("Humidity calibrated (offset calibration): "+ measurement.humidity)
					if "topic" in sensors[mac]:
						currentMQTTTopic=sensors[mac]["topic"]
				else:
					measurement.sensorname = mac
				
				if measurement.calibratedHumidity == 0:
					measurement.calibratedHumidity = measurement.humidity

				if args.mqttconfigfile:
					jsonString=buildJSONString(measurement)
					myMQTTPublish(currentMQTTTopic, jsonString)


		if  args.watchdogtimer:
			keepingLEScanRunningThread = threading.Thread(target=keepingLEScanRunning)
			keepingLEScanRunningThread.start()
			logging.debug("keepingLEScanRunningThread started")



		# Blocking call (the given handler will be called each time a new LE
		# advertisement packet is detected)
		parse_le_advertising_events(sock,
									handler=le_advertise_packet_handler,
									debug=False)
	except KeyboardInterrupt:
		disable_le_scan(sock)
