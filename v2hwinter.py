from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

relay = OutputDevice(27, active_high=False, initial_value=False)

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 5.0

SOC_MIN = 20.0
SOC_MAX = 95.0
SOC_RESERVE = 35.0

PV_REFERENCE_KW = 2.0
HOUR_DELAY_SECONDS = 12.5
INITIAL_SOC = 90.0

hours = list(range(24))

home_load_kw = [
    1.10, 1.00, 0.95, 0.90, 0.95, 1.20,
    1.80, 2.20, 2.50, 3.20, 4.00, 4.60,
    5.00, 4.80, 4.20, 4.00, 4.50, 5.20,
    5.80, 6.00, 5.50, 4.20, 3.00, 1.80
]

pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    0.05, 0.15, 0.35, 0.60, 0.90, 1.20,
    1.35, 1.25, 1.00, 0.70, 0.30, 0.10,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

ev_available = [1] * 24

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

def is_afternoon_peak(hour):
    return 11 <= hour <= 16

def is_evening_peak(hour):
    return 17 <= hour <= 22

def fuzzy_v2h_controller(hour, load, pv, soc, available):

    net_load = load - pv
    solar_ratio = pv / PV_REFERENCE_KW

    if available == 0:
        return "EV_NOT_AVAILABLE", 0.0, False, 0.0, net_load

    if soc <= SOC_RESERVE:
        return "LOW_SOC_PROTECTION", 0.0, False, 0.0, net_load

    low_deficit = triangle(net_load, 0.2, 1.5, 3.0)
    medium_deficit = triangle(net_load, 2.0, 3.5, 5.0)
    high_deficit = trapezoid(net_load, 4.0, 5.0, 8.0, 8.0)

    soc_medium = triangle(soc, 35.0, 60.0, 80.0)
    soc_high = trapezoid(soc, 70.0, 85.0, 100.0, 100.0)

    solar_low = trapezoid(solar_ratio, 0.0, 0.0, 0.25, 0.50)

    afternoon_peak = 1.0 if is_afternoon_peak(hour) else 0.0
    evening_peak = 1.0 if is_evening_peak(hour) else 0.0

    rules = []

    rules.append((
        min(afternoon_peak, medium_deficit, soc_medium, solar_low),
        70.0
    ))

    rules.append((
        min(afternoon_peak, high_deficit, soc_high, solar_low),
        85.0
    ))

    rules.append((
        min(evening_peak, high_deficit, soc_high, solar_low),
        100.0
    ))

    rules.append((
        min(evening_peak, medium_deficit, soc_medium, solar_low),
        90.0
    ))

    rules.append((
        min(low_deficit, max(soc_medium, soc_high), solar_low),
        55.0
    ))

    numerator = 0.0
    denominator = 0.0

    for strength, score in rules:
        numerator += strength * score
        denominator += strength

    if denominator == 0:
        fuzzy_score = 0.0
    else:
        fuzzy_score = numerator / denominator

    if fuzzy_score >= 50.0 and net_load > 0:

        relay_on = True

        if evening_peak and fuzzy_score >= 90:
            ev_power = min(EV_MAX_POWER_KW, net_load)
            decision = "EVENING_MAX_V2H"

        elif afternoon_peak and fuzzy_score >= 70:
            ev_power = min(4.0, net_load)
            decision = "AFTERNOON_V2H"

        else:
            ev_power = min(2.5, net_load)
            decision = "GENERAL_V2H"

    else:

        relay_on = False
        ev_power = 0.0
        decision = "HOLD"

    return (
        decision,
        ev_power,
        relay_on,
        fuzzy_score,
        net_load
    )

def update_soc(soc, ev_power):

    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0

    new_soc = soc - soc_drop

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    return new_soc

def main():

    ev_soc = INITIAL_SOC

    os.makedirs("logs", exist_ok=True)

    log_file = "logs/combined_winter_v2h.csv"

    results = []

    try:

        for i, hour in enumerate(hours):

            load = home_load_kw[i]
            pv = pv_generation_kw[i]
            available = ev_available[i]

            (
                decision,
                ev_power,
                relay_on,
                fuzzy_score,
                net_load
            ) = fuzzy_v2h_controller(
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
                f"Relay={'ON' if relay_on else 'OFF'} | "
                f"{decision}"
            )

            results.append({
                "hour": hour,
                "load_kw": load,
                "pv_kw": pv,
                "net_load_kw": round(net_load, 3),
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

        print("Stopped by user")

    finally:

        relay.off()

        with open(log_file, "w", newline="") as file:

            writer = csv.DictWriter(file, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)

        print(f"Log saved to: {log_file}")
        print("Combined winter simulation complete")

if __name__ == "_main_":
    main()
