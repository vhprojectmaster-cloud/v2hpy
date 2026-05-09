import RPi.GPIO as GPIO
import time

relay = 17

GPIO.setmode(GPIO.BCM)
GPIO.setup(relay, GPIO.OUT)

print("Relay ON")
GPIO.output(relay, GPIO.LOW)
time.sleep(3)

print("Relay OFF")
GPIO.output(relay, GPIO.HIGH)

GPIO.cleanup()
