from gpiozero import OutputDevice
from time import sleep
from datetime import datetime

# ============================================================
# V2H DISCHARGE DEMO
# ============================================================
# Relay ON  = discharge path connected, battery supplying load
# Relay OFF = discharge path disconnected
#
# Relay IN2 -> GPIO27 / physical pin 13
# VCC       -> 5V
# GND       -> GND
#
# If your relay works opposite, change active_high=True to False.
# ============================================================

RELAY_GPIO = 27

DISCHARGE_TIME_SECONDS = int(7.5 * 60)   # 7.5 minutes = 450 seconds
REST_TIME_SECONDS = 30                   # 30 seconds OFF time

relay = OutputDevice(RELAY_GPIO, active_high=True, initial_value=False)

try:
    print("===================================")
    print(" SIMPLE V2H DISCHARGE DEMO")
    print("===================================")
    print("Relay ON  = discharge path connected")
    print("Relay OFF = discharge path disconnected")
    print(f"First discharge time:  {DISCHARGE_TIME_SECONDS} seconds")
    print(f"Relay OFF pause time:  {REST_TIME_SECONDS} seconds")
    print(f"Second discharge time: {DISCHARGE_TIME_SECONDS} seconds")
    print("===================================")

    # First discharge period
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay ON | First discharge started")
    relay.on()
    sleep(DISCHARGE_TIME_SECONDS)

    # Pause period
    relay.off()
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay OFF | 30-second pause started")
    sleep(REST_TIME_SECONDS)

    # Second discharge period
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay ON | Second discharge started")
    relay.on()
    sleep(DISCHARGE_TIME_SECONDS)

    # Final stop
    relay.off()
    print(f"{datetime.now().strftime('%H:%M:%S')} | Relay OFF | Discharge stopped")

except KeyboardInterrupt:
    print("\nStopped by user.")

finally:
    relay.off()
    print("Relay OFF safely.")