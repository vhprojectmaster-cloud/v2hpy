from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os


# This relay setup is the one that worked for your hardware.
relay = OutputDevice(27, active_high=False, initial_value=False)



EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_MAX = 95.0
SOC_RESERVE = 30.0

PV_REFERENCE_KW = 4.0

HOUR_DELAY_SECONDS = 12.5


ev_soc = 75.0


# Scenario 1 dataset
# Sunny day PV, EV available all day


hours = list(range(24))

# Basic residential home load demand in kW
home_load_kw = [
    0.45, 0.38, 0.35, 0.32, 0.35, 0.55,
    0.90, 1.25, 1.10, 0.95, 0.85, 0.80,
    0.90, 0.95, 1.05, 1.25, 1.65, 2.20,
    2.75, 3.10, 2.80, 2.20, 1.40, 0.85
]

# Sunny day PV data
pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.05,
    0.35, 0.95, 1.80, 2.70, 3.45, 3.90,
    4.00, 3.75, 3.10, 2.10, 1.00, 0.25,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

# EV is available all day for Scenario 1
ev_available = [1] * 24



# Fuzzy Membership functions


def triangle(x, a, b, c):
    if x <= a or x >= c:
        return 0.0
    if x == b:
        return 1.0
    if a < x < b:
        return (x - a) / (b - a)
    if b < x < c:
        return (c - x) / (c - b)
    return 0.0


def trapezoid(x, a, b, c, d):
    # Handles left-shoulder and right-shoulder cases
    if a == b and x <= b:
        return 1.0
    if c == d and x >= c:
        return 1.0

    if x <= a or x >= d:
        return 0.0
    if a < x < b:
        return (x - a) / (b - a)
    if b <= x <= c:
        return 1.0
    if c < x < d:
        return (d - x) / (d - c)

    return 0.0


def is_peak_hour(hour):
    # Evening peak-shaving window
    return 17 <= hour <= 21



# Fuzzy V2H controller


def fuzzy_v2h_controller(hour, load, pv, soc, available):
    net_load = load - pv
    solar_ratio = pv / PV_REFERENCE_KW

    # Hard protection rules
    if available == 0:
        return "EV_NOT_AVAILABLE", 0.0, False, 0.0, net_load, solar_ratio

    if soc <= SOC_RESERVE:
        return "LOW_SOC_PROTECTION", 0.0, False, 0.0, net_load, solar_ratio

    # Fuzzy input 1: net load
    solar_surplus = trapezoid(net_load, -5.0, -5.0, -0.8, -0.1)
    balanced_load = triangle(net_load, -0.4, 0.0, 0.4)
    low_deficit = triangle(net_load, 0.1, 1.2, 2.4)
    high_deficit = trapezoid(net_load, 1.8, 2.5, 5.0, 5.0)

    # Fuzzy input 2: EV SOC
    soc_low = trapezoid(soc, 0.0, 0.0, 25.0, 35.0)
    soc_medium = triangle(soc, 30.0, 55.0, 80.0)
    soc_high = trapezoid(soc, 65.0, 80.0, 100.0, 100.0)

    # Fuzzy input 3: solar availability
    solar_low = trapezoid(solar_ratio, 0.0, 0.0, 0.15, 0.35)
    solar_high = trapezoid(solar_ratio, 0.65, 0.85, 1.20, 1.20)

    peak = 1.0 if is_peak_hour(hour) else 0.0

    # Fuzzy rules
    # Score meaning:
    # 0   = hold
    # 60  = slow discharge
    # 80  = medium discharge
    # 100 = fast discharge

    rules = []

    # If PV surplus or high solar, do not discharge
    rules.append((solar_surplus, 0.0))
    rules.append((solar_high, 0.0))

    # If load is balanced, hold
    rules.append((balanced_load, 0.0))

    # If SOC is low, hold
    rules.append((soc_low, 0.0))

    # Peak + high deficit + high SOC + low solar = fast V2H
    rules.append((min(peak, high_deficit, soc_high, solar_low), 100.0))

    # Peak + high deficit + medium SOC + low solar = medium V2H
    rules.append((min(peak, high_deficit, soc_medium, solar_low), 80.0))

    # Peak + low deficit + medium/high SOC + low solar = slow V2H
    rules.append((min(peak, low_deficit, max(soc_medium, soc_high), solar_low), 60.0))

    numerator = 0.0
    denominator = 0.0

    for strength, score in rules:
        numerator += strength * score
        denominator += strength

    if denominator == 0:
        fuzzy_score = 0.0
    else:
        fuzzy_score = numerator / denominator

    # Final decision
    if fuzzy_score >= 55.0 and net_load > 0:
        relay_on = True
        ev_power = min(EV_MAX_POWER_KW, net_load)

        if fuzzy_score >= 85.0:
            decision = "FAST_V2H_DISCHARGE"
        else:
            decision = "SLOW_V2H_DISCHARGE"
    else:
        relay_on = False
        ev_power = 0.0
        decision = "HOLD"

    return decision, ev_power, relay_on, fuzzy_score, net_load, solar_ratio


# -----------------------------
# SOC update
# -----------------------------

def update_soc(soc, ev_power):
    # Positive EV power means discharge
    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0
    new_soc = soc - soc_drop

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    return new_soc


# -----------------------------
# Main loop
# -----------------------------

def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)
    log_file = "logs/sunny_v2h_fuzzy_relay_log.csv"

    results = []

    print("===================================================")
    print(" SUNNY DAY V2H FUZZY RELAY DEMO STARTED")
    print(" Relay ON = V2H discharge active")
    print(" 24 simulated hours = 5 real minutes")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = home_load_kw[i]
            pv = pv_generation_kw[i]
            available = ev_available[i]

            decision, ev_power, relay_on, fuzzy_score, net_load, solar_ratio = fuzzy_v2h_controller(
                hour,
                load,
                pv,
                ev_soc,
                available
            )

            managed_grid = net_load - ev_power

            if relay_on:
                relay.on()
            else:
                relay.off()

            print(
                f"{hour:02d}:00 | "
                f"Load={load:4.2f} kW | "
                f"PV={pv:4.2f} kW | "
                f"Net={net_load:5.2f} kW | "
                f"SOC={ev_soc:5.1f}% | "
                f"Score={fuzzy_score:5.1f} | "
                f"EV={ev_power:4.2f} kW | "
                f"Grid={managed_grid:5.2f} kW | "
                f"Relay={'ON ' if relay_on else 'OFF'} | "
                f"{decision}"
            )

            results.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "home_load_kw": load,
                "pv_generation_kw": pv,
                "net_load_kw": round(net_load, 3),
                "ev_soc_percent": round(ev_soc, 2),
                "solar_ratio": round(solar_ratio, 3),
                "fuzzy_score": round(fuzzy_score, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "managed_grid_kw": round(managed_grid, 3),
                "relay_state": "ON" if relay_on else "OFF"
            })

            ev_soc = update_soc(ev_soc, ev_power)

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        relay.off()
        print("Relay OFF safely.")

        with open(log_file, "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=[
                "timestamp",
                "hour",
                "home_load_kw",
                "pv_generation_kw",
                "net_load_kw",
                "ev_soc_percent",
                "solar_ratio",
                "fuzzy_score",
                "decision",
                "ev_power_kw",
                "managed_grid_kw",
                "relay_state"
            ])

            writer.writeheader()
            writer.writerows(results)

        print(f"Log saved to: {log_file}")
        print("Demo complete.")


if __name__ == "__main__":
    main()