from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SUNNY DAY TWO-PEAK V2H FUZZY RELAY DEMO
#
# Relay IN2 -> GPIO27 / physical pin 13
#
# Relay/Lamp:
# ON  = V2H discharging active
# OFF = charging / hold / no V2H
#
# Case:
# 1. Sunny day PV data
# 2. EV available all day
# 3. Noon V2H support for a few hours
# 4. PV charging during break after noon V2H
# 5. Evening V2H support during peak demand
#
# 24 simulated hours = 5 real minutes
# 1 simulated hour = 12.5 seconds
# ============================================================


# -----------------------------
# RELAY SETUP
# -----------------------------
# Use active_high=True because your relay was opposite before.
# If lamp works opposite again, change True to False.
relay = OutputDevice(27, active_high=True, initial_value=False)


# -----------------------------
# SYSTEM SETTINGS
# -----------------------------

EV_BATTERY_KWH = 60.0
EV_MAX_DISCHARGE_KW = 3.3
EV_MAX_CHARGE_KW = 3.3

SOC_MIN = 20.0
SOC_MAX = 95.0
SOC_RESERVE = 35.0

PV_REFERENCE_KW = 4.0

# 5-minute demo
HOUR_DELAY_SECONDS = 12.5

INITIAL_SOC = 72.0


# -----------------------------
# SUNNY DAY DATASET
# -----------------------------
# Basic residential load + 4 kWp sunny PV profile.
# EV is available all day.

hours = list(range(24))

home_load_kw = [
    0.45, 0.38, 0.35, 0.32, 0.35, 0.55,
    0.90, 1.20, 1.05, 0.95, 1.10, 1.80,
    2.40, 2.20, 1.30, 1.10, 1.50, 2.25,
    2.90, 3.20, 2.85, 2.20, 1.40, 0.85
]

pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.05,
    0.35, 0.95, 1.80, 2.70, 3.50, 3.90,
    4.00, 3.80, 3.60, 3.00, 1.60, 0.35,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

ev_available = [1] * 24


# -----------------------------
# MEMBERSHIP FUNCTIONS
# -----------------------------

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
    # Left shoulder
    if a == b and x <= b:
        return 1.0

    # Right shoulder
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


# -----------------------------
# TIME WINDOWS
# -----------------------------

def is_noon_v2h_window(hour):
    # Short noon V2H support window
    return 11 <= hour <= 13


def is_charge_break_window(hour):
    # Break after noon V2H where PV charges EV virtually
    return 14 <= hour <= 16


def is_evening_peak_window(hour):
    # Evening peak demand window
    return 17 <= hour <= 21


# -----------------------------
# FUZZY CONTROLLER
# -----------------------------

def fuzzy_v2h_controller(hour, load, pv, soc, available):
    net_load = load - pv
    solar_ratio = pv / PV_REFERENCE_KW

    if available == 0:
        return "EV_NOT_AVAILABLE", 0.0, False, 0.0, net_load, solar_ratio

    if soc <= SOC_RESERVE:
        return "LOW_SOC_PROTECTION", 0.0, False, 0.0, net_load, solar_ratio

    # Fuzzy input: net load
    pv_surplus = trapezoid(net_load, -5.0, -5.0, -0.8, -0.1)
    balanced = triangle(net_load, -0.4, 0.0, 0.4)
    low_deficit = triangle(net_load, 0.1, 1.2, 2.4)
    high_deficit = trapezoid(net_load, 1.8, 2.5, 5.0, 5.0)

    # Fuzzy input: SOC
    soc_low = trapezoid(soc, 0.0, 0.0, 25.0, 35.0)
    soc_medium = triangle(soc, 30.0, 55.0, 80.0)
    soc_high = trapezoid(soc, 65.0, 80.0, 100.0, 100.0)

    # Fuzzy input: solar condition
    solar_low = trapezoid(solar_ratio, 0.0, 0.0, 0.15, 0.35)
    solar_high = trapezoid(solar_ratio, 0.65, 0.85, 1.20, 1.20)

    noon_window = 1.0 if is_noon_v2h_window(hour) else 0.0
    charge_window = 1.0 if is_charge_break_window(hour) else 0.0
    evening_window = 1.0 if is_evening_peak_window(hour) else 0.0

    # -----------------------------
    # DISCHARGE FUZZY RULES
    # Score:
    # 0   = hold
    # 60  = weak discharge
    # 80  = medium discharge
    # 100 = strong discharge
    # -----------------------------

    rules = []

    # Hold rules
    rules.append((balanced, 0.0))
    rules.append((soc_low, 0.0))

    # Noon V2H rules
    # Noon discharge is smaller than evening discharge.
    rules.append((
        min(noon_window, low_deficit, max(soc_medium, soc_high)),
        65.0
    ))

    rules.append((
        min(noon_window, high_deficit, max(soc_medium, soc_high)),
        75.0
    ))

    # Evening peak V2H rules
    rules.append((
        min(evening_window, high_deficit, soc_high, solar_low),
        100.0
    ))

    rules.append((
        min(evening_window, high_deficit, soc_medium, solar_low),
        90.0
    ))

    rules.append((
        min(evening_window, low_deficit, max(soc_medium, soc_high), solar_low),
        75.0
    ))

    # Defuzzification
    numerator = 0.0
    denominator = 0.0

    for strength, score in rules:
        numerator += strength * score
        denominator += strength

    fuzzy_score = 0.0 if denominator == 0 else numerator / denominator

    # -----------------------------
    # FINAL DECISION
    # -----------------------------

    # 1. Noon V2H discharge
    if is_noon_v2h_window(hour) and fuzzy_score >= 55.0 and soc > SOC_RESERVE:
        relay_on = True
        ev_power = min(1.5, max(load - pv, 0.8))
        decision = "NOON_V2H_DISCHARGE"

    # 2. Charging break after noon V2H
    # Relay stays OFF because charging is only simulated in this demo.
    elif is_charge_break_window(hour) and pv_surplus > 0.0 and soc < SOC_MAX:
        relay_on = False
        ev_power = -min(EV_MAX_CHARGE_KW, abs(net_load))
        decision = "PV_CHARGING_BREAK_SIMULATED"

    # 3. Evening V2H discharge
    elif is_evening_peak_window(hour) and fuzzy_score >= 55.0 and net_load > 0 and soc > SOC_RESERVE:
        relay_on = True
        ev_power = min(EV_MAX_DISCHARGE_KW, net_load)

        if fuzzy_score >= 85.0:
            decision = "EVENING_FAST_V2H_DISCHARGE"
        else:
            decision = "EVENING_SLOW_V2H_DISCHARGE"

    # 4. Hold
    else:
        relay_on = False
        ev_power = 0.0
        decision = "HOLD"

    return decision, ev_power, relay_on, fuzzy_score, net_load, solar_ratio


# -----------------------------
# SOC UPDATE
# -----------------------------

def update_soc(soc, ev_power):
    """
    ev_power > 0  means discharge, SOC decreases.
    ev_power < 0  means charge, SOC increases.
    """
    soc_change = (ev_power / EV_BATTERY_KWH) * 100.0
    new_soc = soc - soc_change

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    return new_soc


# -----------------------------
# MAIN PROGRAM
# -----------------------------

def main():
    ev_soc = INITIAL_SOC

    os.makedirs("logs", exist_ok=True)
    log_file = "logs/sunny_two_peak_v2h_log.csv"

    results = []

    print("===================================================")
    print(" SUNNY DAY TWO-PEAK V2H FUZZY DEMO")
    print(" Relay ON  = V2H discharge active")
    print(" Relay OFF = charging/hold")
    print(" Noon V2H: 11:00-13:00")
    print(" PV charging break: 14:00-16:00")
    print(" Evening V2H: 17:00-21:00")
    print(" 24 simulated hours = 5 real minutes")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = home_load_kw[i]
            pv = pv_generation_kw[i]
            available = ev_available[i]

            decision, ev_power, relay_on, fuzzy_score, net_load, solar_ratio = fuzzy_v2h_controller(
                hour, load, pv, ev_soc, available
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
                f"EV={ev_power:5.2f} kW | "
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
                "solar_ratio": round(solar_ratio, 3),
                "ev_soc_percent": round(ev_soc, 2),
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
            fieldnames = [
                "timestamp",
                "hour",
                "home_load_kw",
                "pv_generation_kw",
                "net_load_kw",
                "solar_ratio",
                "ev_soc_percent",
                "fuzzy_score",
                "decision",
                "ev_power_kw",
                "managed_grid_kw",
                "relay_state"
            ]

            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

        print(f"Log saved to: {log_file}")
        print("Demo complete.")


if __name__ == "__main__":
    main()