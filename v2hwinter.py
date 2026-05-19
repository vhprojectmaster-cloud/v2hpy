from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# WINTER DAY V2H FUZZY RELAY CONTROL WITH SUMMARY MATRIX
#
# Relay IN2 -> GPIO27 / physical pin 13
#
# Relay ON  = V2H discharge active
# Relay OFF = no V2H discharge
#
# 24 simulated hours = 5 real minutes
# 1 simulated hour = 12.5 seconds
# ============================================================

# Use active_high=True because your relay was working opposite before.
# If relay works opposite again, change True to False.
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
# Summary matrix
# -----------------------------

def create_summary_matrix(results):
    total_home_energy = sum(row["load_kw"] for row in results)
    total_pv_energy = sum(row["pv_kw"] for row in results)

    baseline_import_energy = sum(max(row["net_load_kw"], 0.0) for row in results)
    managed_import_energy = sum(max(row["grid_kw"], 0.0) for row in results)

    baseline_export_energy = sum(max(-row["net_load_kw"], 0.0) for row in results)
    managed_export_energy = sum(max(-row["grid_kw"], 0.0) for row in results)

    ev_discharge_energy = sum(max(row["ev_power_kw"], 0.0) for row in results)

    baseline_peak = max(max(row["net_load_kw"], 0.0) for row in results)
    managed_peak = max(max(row["grid_kw"], 0.0) for row in results)

    peak_reduction_kw = baseline_peak - managed_peak

    if baseline_peak > 0:
        peak_reduction_percent = (peak_reduction_kw / baseline_peak) * 100.0
    else:
        peak_reduction_percent = 0.0

    grid_import_reduction = baseline_import_energy - managed_import_energy

    if baseline_import_energy > 0:
        grid_import_reduction_percent = (
            grid_import_reduction / baseline_import_energy
        ) * 100.0
    else:
        grid_import_reduction_percent = 0.0

    initial_soc = results[0]["soc_before_percent"]
    final_soc = results[-1]["soc_after_percent"]
    minimum_soc = min(row["soc_after_percent"] for row in results)

    relay_on_hours = sum(1 for row in results if row["relay"] == "ON")

    v2h_active_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["relay"] == "ON"
    ]

    afternoon_v2h_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["decision"] == "AFTERNOON_V2H"
    ]

    evening_v2h_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["decision"] == "EVENING_MAX_V2H"
    ]

    general_v2h_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["decision"] == "GENERAL_V2H"
    ]

    if total_pv_energy > 0:
        baseline_pv_self_consumption = (
            (total_pv_energy - baseline_export_energy) / total_pv_energy
        ) * 100.0

        managed_pv_self_consumption = (
            (total_pv_energy - managed_export_energy) / total_pv_energy
        ) * 100.0
    else:
        baseline_pv_self_consumption = 0.0
        managed_pv_self_consumption = 0.0

    summary = {
        "Scenario": "Winter V2H fuzzy control",
        "Total home load energy (kWh)": round(total_home_energy, 2),
        "Total PV generation (kWh)": round(total_pv_energy, 2),
        "Baseline grid import energy (kWh)": round(baseline_import_energy, 2),
        "Managed grid import energy (kWh)": round(managed_import_energy, 2),
        "Grid import reduction (kWh)": round(grid_import_reduction, 2),
        "Grid import reduction (%)": round(grid_import_reduction_percent, 1),
        "Baseline grid export energy (kWh)": round(baseline_export_energy, 2),
        "Managed grid export energy (kWh)": round(managed_export_energy, 2),
        "EV discharge energy (kWh)": round(ev_discharge_energy, 2),
        "Baseline peak demand (kW)": round(baseline_peak, 2),
        "Managed peak demand (kW)": round(managed_peak, 2),
        "Peak demand reduction (kW)": round(peak_reduction_kw, 2),
        "Peak demand reduction (%)": round(peak_reduction_percent, 1),
        "Initial SOC (%)": round(initial_soc, 2),
        "Final SOC (%)": round(final_soc, 2),
        "Minimum SOC (%)": round(minimum_soc, 2),
        "Relay ON hours": relay_on_hours,
        "V2H active hours": ", ".join(v2h_active_hours),
        "Afternoon V2H hours": ", ".join(afternoon_v2h_hours),
        "Evening V2H hours": ", ".join(evening_v2h_hours),
        "General V2H hours": ", ".join(general_v2h_hours),
        "Baseline PV self-consumption (%)": round(baseline_pv_self_consumption, 1),
        "Managed PV self-consumption (%)": round(managed_pv_self_consumption, 1),
    }

    return summary


def print_summary_matrix(summary):
    print("\n================ WINTER SUMMARY MATRIX ================")

    for key, value in summary.items():
        print(f"{key:42s}: {value}")

    print("=======================================================")


def save_summary_matrix(summary, file_path):
    with open(file_path, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["metric", "value"])

        for key, value in summary.items():
            writer.writerow([key, value])


# -----------------------------
# Main program
# -----------------------------

def main():
    ev_soc = INITIAL_SOC

    os.makedirs("logs", exist_ok=True)

    hourly_log_file = "logs/combined_winter_v2h.csv"
    summary_log_file = "logs/winter_v2h_summary_matrix.csv"

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

            soc_before = ev_soc

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

            ev_soc = update_soc(ev_soc, ev_power)
            soc_after = ev_soc

            print(
                f"{hour:02d}:00 | "
                f"Load={load:4.2f} kW | "
                f"PV={pv:4.2f} kW | "
                f"Net={net_load:5.2f} kW | "
                f"SOC={soc_before:5.1f}% -> {soc_after:5.1f}% | "
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
                "soc_before_percent": round(soc_before, 2),
                "soc_after_percent": round(soc_after, 2),
                "fuzzy_score": round(fuzzy_score, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "grid_kw": round(managed_grid, 3),
                "relay": "ON" if relay_on else "OFF"
            })

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user")

    finally:
        relay.off()
        print("Relay OFF safely.")

        if len(results) > 0:
            fieldnames = [
                "timestamp",
                "hour",
                "load_kw",
                "pv_kw",
                "net_load_kw",
                "solar_ratio",
                "soc_before_percent",
                "soc_after_percent",
                "fuzzy_score",
                "decision",
                "ev_power_kw",
                "grid_kw",
                "relay"
            ]

            with open(hourly_log_file, "w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)

            summary = create_summary_matrix(results)
            print_summary_matrix(summary)
            save_summary_matrix(summary, summary_log_file)

            print(f"\nHourly log saved to: {hourly_log_file}")
            print(f"Summary matrix saved to: {summary_log_file}")

        else:
            print("No results recorded, so no CSV files were saved.")

        print("Combined winter simulation complete")


if __name__ == "__main__":
    main()