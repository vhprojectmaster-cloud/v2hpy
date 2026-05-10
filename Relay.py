from gpiozero import OutputDevice
from time import sleep

# Your relay IN2 is connected to physical pin 13
# Physical pin 13 = GPIO27
RELAY_GPIO = 27

# Most relay modules are active-low.
# If the LED works backwards, change this to False.
RELAY_ACTIVE_LOW = True

relay = OutputDevice(
    RELAY_GPIO,
    active_high=not RELAY_ACTIVE_LOW,
    initial_value=False
)

print("Relay LED 10-second ON/OFF test started")
print("Using IN2 -> GPIO27 -> physical pin 13")
print("Press CTRL + C to stop")

try:
    while True:
        print("Relay LED ON for 10 seconds")
        relay.on()
        sleep(10)

        print("Relay LED OFF for 10 seconds")
        relay.off()
        sleep(10)

except KeyboardInterrupt:
    print("\nStopping test...")

finally:
    relay.off()
    print("Relay LED OFF. Test ended safely.")