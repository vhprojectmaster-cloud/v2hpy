from gpiozero import OutputDevice
from time import sleep
from datetime import datetime

# ============================================================
# SIMPLE CHARGING DEMO
# ============================================================
# Relay ON  = charger connected, battery charging
# Relay OFF = charger disconnected
#
# Relay IN2 -> GPIO27 / physical pin 13
# VCC       -> 5V
# GND       -> GND

RELAY_GPIO = 27

CHARGE_TIME_SECONDS = int(7.5 * 60)   # 7.5 minutes = 450 seconds
REST_TIME_SECONDS = 30                # 30 seconds OFF time

relay = OutputDevice(RELAY_GPIO, active_high=True, initial_value=False)

try:
    print("===================================")
    print(" SIMPLE CHARGING DEMO")
    print("===================================")
    print("Relay ON  = charger connected")
    print("Relay OFF = charger disconnected")
    print(f"First charging time:  {CHARGE_TIME_SECONDS} seconds")
    print(f"Relay OFF pause time: {REST_TIME_SECONDS} seconds")
    print(f"Second charging time: {CHARGE_TIME_SECONDS} seconds")
    print("===================================")

    # First charging period
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay ON | First charging started")
    relay.on()
    sleep(CHARGE_TIME_SECONDS)

    # Pause period
    relay.off()
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay OFF | 30-second pause started")
    sleep(REST_TIME_SECONDS)

    # Second charging period
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay ON | Second charging started")
    relay.on()
    sleep(CHARGE_TIME_SECONDS)

    # Final stop
    relay.off()
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay OFF | Charging stopped")

except KeyboardInterrupt:
    print("\nStopped by user.")

finally:
    relay.off()
    print("Relay OFF safely.")