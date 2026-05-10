import RPi.GPIO as GPIO
import time

relay_pin = 27   # GPIO27 = physical pin 13

GPIO.setmode(GPIO.BCM)
GPIO.setup(relay_pin, GPIO.OUT)

try:
    while True:
        print("Relay 2 ON")
        GPIO.output(relay_pin, GPIO.LOW)   # active LOW relay ON
        time.sleep(3)

        print("Relay 2 OFF")
        GPIO.output(relay_pin, GPIO.HIGH)  # relay OFF
        time.sleep(3)

except KeyboardInterrupt:
    GPIO.cleanup()