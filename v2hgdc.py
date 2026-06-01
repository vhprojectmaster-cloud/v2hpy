from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SCENARIO 3: EMERGENCY POWER FAILURE MODE - FUZZY LOGIC
# ============================================================

relay = OutputDevice(27, active_high=True, initial_value=False)

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_RESERVE = 35.0
HOUR_DELAY_SECONDS = 12.5

ev_soc = 85.0
hours = list(range(24))

critical_load_kw = [
    0.20, 0.18, 0.18, 0.18, 0.20, 0.25,
    0.35, 0.45, 0.40, 0.35, 0.35, 0.30,
    0.30, 0.35, 0.40, 0.45, 0.60, 0.75,
    0.85, 0.80, 0.70, 0.50, 0.35, 0.25
]

grid_available = [
    1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1,
    0, 0, 0, 0, 1, 1
]

ev_available = [
    1, 1, 1, 1, 1, 0,
    0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 1, 1,
    1, 1, 1, 1, 1, 1
]


def triangle(x, a, b, c):
    if x <= a or x >= c:
        return 0.0
    elif x == b:
        return 1.0
    elif a < x < b:
        return (x - a) / (b - a)
    else:
        return (c - x) / (c - b)


def left_shoulder(x, a, b):
    if x <= a:
        return 1.0
    elif x >= b:
        return 0.0
    else:
        return (b - x) / (b - a)


def right_shoulder(x, a, b):
    if x <= a:
        return 0.0
    elif x >= b:
        return 1.0
    else:
        return (x - a) / (b - a)


def fuzzy_emergency_controller(load, grid_status, soc, available):

    # Emergency condition
    outage = 1.0 if grid_status == 0 else 0.0
    ev_home = 1.0 if available == 1 else 0.0

    # SOC fuzzy sets
    soc_low = left_shoulder(soc, 25, 40)
    soc_medium = triangle(soc, 35, 55, 75)
    soc_high = right_shoulder(soc, 65, 85)

    # Load fuzzy sets
    load_low = left_shoulder(load, 0.25, 0.50)
    load_medium = triangle(load, 0.30, 0.60, 0.90)
    load_high = right_shoulder(load, 0.70, 1.00)

    rules = []

    # Output values:
    # 0   = backup OFF
    # 30  = weak backup
    # 60  = medium backup
    # 90  = strong backup

    rules.append((1 - outage, 0))
    rules.append((outage * (1 - ev_home), 0))
    rules.append((outage * ev_home * soc_low, 20))
    rules.append((outage * ev_home * soc_medium * load_low, 60))
    rules.append((outage * ev_home * soc_medium * load_medium, 65))
    rules.append((outage * ev_home * soc_medium * load_high, 55))
    rules.append((outage * ev_home * soc_high * load_low, 80))
    rules.append((outage * ev_home * soc_high * load_medium, 90))
    rules.append((outage * ev_home * soc_high * load_high, 95))

    numerator = sum(strength * output for strength, output in rules)
    denominator = sum(strength for strength, output in rules)

    fuzzy_backup_strength = numerator / denominator if denominator != 0 else 0

    relay_on = (
        fuzzy_backup_strength >= 50
        and soc > SOC_RESERVE
        and outage == 1
        and ev_home == 1
    )

    ev_power = min(EV_MAX_POWER_KW, load) if relay_on else 0.0

    if grid_status == 1:
        decision = "GRID_AVAILABLE_NORMAL_MODE"
    elif available == 0:
        decision = "OUTAGE_BUT_EV_NOT_AVAILABLE"
    elif soc <= SOC_RESERVE:
        decision = "OUTAGE_LOW_SOC_PROTECTION"
    elif relay_on:
        decision = "FUZZY_EMERGENCY_BACKUP_ACTIVE"
    else:
        decision = "FUZZY_BACKUP_NOT_ACTIVE"

    return decision, ev_power, relay_on, fuzzy_backup_strength


def update_soc(soc, ev_power):
    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0
    return max(soc - soc_drop, SOC_MIN)


def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)
    log_file = "logs/scenario3_fuzzy_emergency_backup_log.csv"
    results = []

    print("===================================================")
    print(" SCENARIO 3: FUZZY EMERGENCY POWER FAILURE MODE")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = critical_load_kw[i]
            grid_status = grid_available[i]
            available = ev_available[i]

            decision, ev_power, relay_on, fuzzy_strength = fuzzy_emergency_controller(
                load, grid_status, ev_soc, available
            )

            relay.on() if relay_on else relay.off()

            print(
                f"{hour:02d}:00 | "
                f"Load={load:.2f} kW | "
                f"Grid={'ON' if grid_status else 'OFF'} | "
                f"EV Available={available} | "
                f"SOC={ev_soc:.1f}% | "
                f"Fuzzy Strength={fuzzy_strength:.1f}% | "
                f"EV Power={ev_power:.2f} kW | "
                f"Relay={'ON' if relay_on else 'OFF'} | "
                f"{decision}"
            )

            results.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "critical_load_kw": load,
                "grid_available": grid_status,
                "ev_available": available,
                "ev_soc_percent": round(ev_soc, 2),
                "fuzzy_backup_strength_percent": round(fuzzy_strength, 2),
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
                "fuzzy_backup_strength_percent",
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