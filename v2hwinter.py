from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# WINTER DAY V2H FUZZY RELAY CONTROL
#
# Relay IN2 -> GPIO27 / physical pin 13
#
# Relay ON  = V2H discharge active
# Relay OFF = no V2H discharge
#
# 24 simulated hours = 5 real minutes
# 1 simulated hour = 12.5 seconds
# ============================================================

# Use active_high=True because your relay was working opposite before
relay = OutputDevice(27, active_high=True, initial_value=False)

# -----------------------------
# System settings
# -----------------------------

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_MAX = 95.0
SOC_RESERVE = 35.0

PV_REFERENCE_KW = 2.0
HOUR_DELAY_SECONDS = 12.5
INITIAL_SOC = 90.0

hours = list(range(24))

# Winter day home load demand in kW
# Higher daytime/evening demand compared with sunny day case
home_load_kw = [
    1.10, 1.00, 0.95, 0.90, 0.95, 1.20,
    1.80, 2.20, 2.50, 3.20, 4.00, 4.60,
    5.00, 4.80, 4.20, 4.00, 4.50, 5.20,
    5.80, 6.00, 5.50, 4.20, 3.00, 1.80
]

# Winter PV generation in kW
# Lower PV output due to weaker winter solar availability
pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    0.05, 0.15, 0.35, 0.60, 0.90, 1.20,
    1.35, 1.25, 1.00, 0.70, 0.30, 0.10,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

# EV available all day for this test case
ev_available = [1] * 24


# -----------------------------
# Fuzzy membership functions
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
    # Left shoulder case
    if a == b and x <= b:
        return 1.0

    # Right shoulder case
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
# Peak windows
# -----------------------------

def is_afternoon_peak(hour):
    return 11 <= hour <= 16


def is_evening_peak(hour):
    return 17 <= hour <= 22


# -----------------------------
# Fuzzy V2H controller
# -----------------------------

def fuzzy_v2h_controller(hour, load, pv, soc, available):
    net_load = load - pv
    solar_ratio = pv / PV_REFERENCE_KW

    # Hard safety checks first
    if available == 0:
        return "EV_NOT_AVAILABLE", 0.0, False, 0.0, net_load, solar_ratio

    if soc <= SOC_RESERVE:
        return "LOW_SOC_PROTECTION", 0.0, False, 0.0, net_load, solar_ratio

    # Net load fuzzy sets
    low_deficit = triangle(net_load, 0.2, 1.5, 3.0)
    medium_deficit = triangle(net_load, 2.0, 3.5, 5.0)
    high_deficit = trapezoid(net_load, 4.0, 5.0, 8.0, 8.0)

    # SOC fuzzy sets
    soc_medium = triangle(soc, 35.0, 60.0, 80.0)
    soc_high = trapezoid(soc, 70.0, 85.0, 100.0, 100.0)

    # Solar low means V2H is more useful
    solar_low = trapezoid(solar_ratio, 0.0, 0.0, 0.25, 0.50)

    afternoon_peak = 1.0 if is_afternoon_peak(hour) else 0.0
    evening_peak = 1.0 if is_evening_peak(hour) else 0.0

    # -----------------------------
    # Fuzzy rules
    # Score:
    # 0   = hold
    # 55  = general V2H
    # 70  = afternoon V2H
    # 90+ = evening V2H
    # -----------------------------

    rules = []

    # Afternoon peak support
    rules.append((
        min(afternoon_peak, medium_deficit, soc_medium, solar_low),
        70.0
    ))

    rules.append((
        min(afternoon_peak, high_deficit, soc_high, solar_low),
        85.0
    ))

    # Evening peak support
    rules.append((
        min(evening_peak, high_deficit, soc_high, solar_low),
        100.0
    ))

    rules.append((
        min(evening_peak, medium_deficit, soc_medium, solar_low),
        90.0
    ))

    # General low-level support if load exists and solar is low
    rules.append((
        min(low_deficit, max(soc_medium, soc_high), solar_low),
        55.0
    ))

    # Defuzzification using weighted average
    numerator = 0.0
    denominator = 0.0

    for strength, score in rules:
        numerator += strength * score
        denominator += strength

    if denominator == 0:
        fuzzy_score = 0.0
    else:
        fuzzy_score = numerator / denominator

    # Final relay decision
    if fuzzy_score >= 50.0 and net_load > 0:
        relay_on = True

        if evening_peak and fuzzy_score >= 90.0:
            ev_power = min(EV_MAX_POWER_KW, net_load)
            decision = "EVENING_MAX_V2H"

        elif afternoon_peak and fuzzy_score >= 70.0:
            ev_power = min(3.0, net_load)
            decision = "AFTERNOON_V2H"

        else:
            ev_power = min(2.0, net_load)
            decision = "GENERAL_V2H"

    else:
        relay_on = False
        ev_power = 0.0
        decision = "HOLD"

    return decision, ev_power, relay_on, fuzzy_score, net_load, solar_ratio


# -----------------------------
# SOC update
# -----------------------------

def update_soc(soc, ev_power):
    # Positive EV power means EV is discharging
    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0
    new_soc = soc - soc_drop

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    return new_soc


# -----------------------------
# Main program
# -----------------------------

def main():
    ev_soc = INITIAL_SOC

    os.makedirs("logs", exist_ok=True)
    log_file = "logs/combined_winter_v2h.csv"

    results = []

    print("===================================================")
    print(" WINTER V2H FUZZY RELAY DEMO STARTED")
    print(" Relay ON = V2H discharge active")
    print(" Relay OFF = no V2H discharge")
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
                f"Solar={solar_ratio:4.2f} | "
                f"Score={fuzzy_score:5.1f} | "
                f"EV={ev_power:4.2f} kW | "
                f"Grid={managed_grid:5.2f} kW | "
                f"Relay={'ON ' if relay_on else 'OFF'} | "
                f"{decision}"
            )

            results.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "load_kw": load,
                "pv_kw": pv,
                "net_load_kw": round(net_load, 3),
                "solar_ratio": round(solar_ratio, 3),
                "soc_percent": round(ev_soc, 2),
                "fuzzy_score": round(fuzzy_score, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "grid_kw": round(managed_grid, 3),
                "relay": "ON" if relay_on else "OFF"
            })

            ev_soc = update_soc(ev_soc, ev_power)

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user")

    finally:
        relay.off()
        print("Relay OFF safely.")

        fieldnames = [
            "timestamp",
            "hour",
            "load_kw",
            "pv_kw",
            "net_load_kw",
            "solar_ratio",
            "soc_percent",
            "fuzzy_score",
            "decision",
            "ev_power_kw",
            "grid_kw",
            "relay"
        ]

        with open(log_file, "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

        print(f"Log saved to: {log_file}")
        print("Combined winter simulation complete")


if __name__ == "__main__":
    main()