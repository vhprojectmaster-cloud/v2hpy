from gpiozero import OutputDevice
from time import sleep
from datetime import datetime

# ============================================================
# SIMPLE 5-MINUTE CHARGING DEMO
# ============================================================
# Relay ON  = charger connected, battery charging
# Relay OFF = charger disconnected
#
# Relay IN2 -> GPIO27 / physical pin 13
# VCC       -> 5V
# GND       -> GND
#
# If your relay works opposite, change active_high=True to False.
# ============================================================

RELAY_GPIO = 27
CHARGE_TIME_SECONDS = 5 * 60   # 5 minutes

relay = OutputDevice(RELAY_GPIO, active_high=True, initial_value=False)

try:
    print("===================================")
    print(" SIMPLE 5-MINUTE CHARGING DEMO")
    print("===================================")
    print("Relay ON  = charger connected")
    print("Relay OFF = charger disconnected")
    print(f"Charging time: {CHARGE_TIME_SECONDS} seconds")
    print("===================================")

    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay ON | Charging started")
    relay.on()

    sleep(CHARGE_TIME_SECONDS)

    relay.off()
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay OFF | Charging stopped")

except KeyboardInterrupt:
    print("\nStopped by user.")

finally:
    relay.off()
    print("Relay OFF safely.")