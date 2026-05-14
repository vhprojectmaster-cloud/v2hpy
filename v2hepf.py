from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SCENARIO 3: EMERGENCY POWER FAILURE MODE
#
# Relay IN2 -> GPIO27 / physical pin 13
# Relay ON  = critical load supplied from EV battery
# Relay OFF = normal grid supply / no emergency backup
#
# 24 simulated hours compressed into 5 real minutes
# ============================================================

relay = OutputDevice(27, active_high=False, initial_value=False)

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_RESERVE = 35.0
HOUR_DELAY_SECONDS = 12.5

ev_soc = 85.0

hours = list(range(24))

# Critical household load only during outage
# Example: light, router, small essential device
critical_load_kw = [
    0.20, 0.18, 0.18, 0.18, 0.20, 0.25,
    0.35, 0.45, 0.40, 0.35, 0.35, 0.30,
    0.30, 0.35, 0.40, 0.45, 0.60, 0.75,
    0.85, 0.80, 0.70, 0.50, 0.35, 0.25
]

# 1 = grid available, 0 = outage
# Simulated outage from 18:00 to 21:00
grid_available = [
    1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1,
    0, 0, 0, 0, 1, 1
]

# EV is available at home during emergency period
ev_available = [
    1, 1, 1, 1, 1, 0,
    0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 1, 1,
    1, 1, 1, 1, 1, 1
]

def emergency_controller(hour, load, grid_status, soc, available):
    if grid_status == 1:
        return "GRID_AVAILABLE_NORMAL_MODE", 0.0, False

    if available == 0:
        return "OUTAGE_BUT_EV_NOT_AVAILABLE", 0.0, False

    if soc <= SOC_RESERVE:
        return "OUTAGE_LOW_SOC_PROTECTION", 0.0, False

    ev_power = min(EV_MAX_POWER_KW, load)
    return "EMERGENCY_BACKUP_ACTIVE", ev_power, True

def update_soc(soc, ev_power):
    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0
    new_soc = soc - soc_drop
    return max(new_soc, SOC_MIN)

def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)
    log_file = "logs/scenario4_emergency_backup_log.csv"
    results = []

    print("===================================================")
    print(" SCENARIO 4: EMERGENCY POWER FAILURE MODE")
    print(" Relay ON = critical load supplied by EV battery")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = critical_load_kw[i]
            grid_status = grid_available[i]
            available = ev_available[i]

            decision, ev_power, relay_on = emergency_controller(
                hour, load, grid_status, ev_soc, available
            )

            if relay_on:
                relay.on()
            else:
                relay.off()

            print(
                f"{hour:02d}:00 | "
                f"Critical Load={load:4.2f} kW | "
                f"Grid={'ON ' if grid_status else 'OFF'} | "
                f"EV Available={available} | "
                f"SOC={ev_soc:5.1f}% | "
                f"EV Power={ev_power:4.2f} kW | "
                f"Relay={'ON ' if relay_on else 'OFF'} | "
                f"{decision}"
            )

            results.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "critical_load_kw": load,
                "grid_available": grid_status,
                "ev_available": available,
                "ev_soc_percent": round(ev_soc, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "relay_state": "ON" if relay_on else "OFF"
            })

            ev_soc = update_soc(ev_soc, ev_power)
            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        relay.off()

        with open(log_file, "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=[
                "timestamp",
                "hour",
                "critical_load_kw",
                "grid_available",
                "ev_available",
                "ev_soc_percent",
                "decision",
                "ev_power_kw",
                "relay_state"
            ])
            writer.writeheader()
            writer.writerows(results)

        print("Relay OFF safely.")
        print(f"Log saved to: {log_file}")

if _name_ == "_main_":
    main()