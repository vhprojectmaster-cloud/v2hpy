from   import OutputDevice
from time import sleep

relay = OutputDevice(27, active_high=False, initial_value=False)

while True:
    print("Relay 2 ON")
    relay.on()
    sleep(15)

    print("Relay 2 OFF")
    relay.off()
    sleep(15)