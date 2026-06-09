from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SUNNY DAY V2H FUZZY RELAY DEMO WITH SUMMARY MATRIX
#
# Relay IN2 -> Raspberry Pi GPIO27 / physical pin 13
#
# Relay/Lamp:
# ON  = V2H discharge active
# OFF = hold / no V2H discharge
#
# 24 simulated hours = 5 real minutes
# 1 simulated hour = 12.5 seconds
# ============================================================


# -----------------------------
# RELAY SETUP
# -----------------------------


relay = OutputDevice(27, active_high=True, initial_value=False)


# -----------------------------
# SYSTEM CONSTANTS
# -----------------------------

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_MAX = 95.0
SOC_RESERVE = 30.0

PV_REFERENCE_KW = 4.0

HOUR_DELAY_SECONDS = 12.5

# Starting EV SOC
ev_soc = 75.0


# -----------------------------
# SCENARIO DATA
# Sunny day PV, EV available all day
# -----------------------------

hours = list(range(24))

home_load_kw = [
    0.45, 0.38, 0.35, 0.32, 0.35, 0.55,
    0.90, 1.25, 1.10, 0.95, 0.85, 0.80,
    0.90, 0.95, 1.05, 1.25, 1.65, 2.20,
    2.75, 3.10, 2.80, 2.20, 1.40, 0.85
]

pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.05,
    0.35, 0.95, 1.80, 2.70, 3.45, 3.90,
    4.00, 3.75, 3.10, 2.10, 1.00, 0.25,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

ev_available = [1] * 24


# -----------------------------
# FUZZY MEMBERSHIP FUNCTIONS
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


def is_peak_hour(hour):
    return 17 <= hour <= 21


# -----------------------------
# FUZZY V2H CONTROLLER
# -----------------------------

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
    rules = []

    # Hold rules
    rules.append((solar_surplus, 0.0))
    rules.append((solar_high, 0.0))
    rules.append((balanced_load, 0.0))
    rules.append((soc_low, 0.0))

    # Peak V2H rules
    rules.append((min(peak, high_deficit, soc_high, solar_low), 100.0))
    rules.append((min(peak, high_deficit, soc_medium, solar_low), 80.0))
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
# SOC UPDATE
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
# SUMMARY MATRIX
# -----------------------------

def create_summary_matrix(results):
    home_energy = sum(row["home_load_kw"] for row in results)
    pv_energy = sum(row["pv_generation_kw"] for row in results)

    baseline_import_energy = sum(max(row["net_load_kw"], 0.0) for row in results)
    managed_import_energy = sum(max(row["managed_grid_kw"], 0.0) for row in results)

    baseline_export_energy = sum(max(-row["net_load_kw"], 0.0) for row in results)
    managed_export_energy = sum(max(-row["managed_grid_kw"], 0.0) for row in results)

    ev_discharge_energy = sum(max(row["ev_power_kw"], 0.0) for row in results)

    baseline_peak = max(max(row["net_load_kw"], 0.0) for row in results)
    managed_peak = max(max(row["managed_grid_kw"], 0.0) for row in results)

    peak_reduction_kw = baseline_peak - managed_peak

    if baseline_peak > 0:
        peak_reduction_percent = (peak_reduction_kw / baseline_peak) * 100.0
    else:
        peak_reduction_percent = 0.0

    final_soc = results[-1]["ev_soc_after_percent"]
    minimum_soc = min(row["ev_soc_after_percent"] for row in results)

    relay_on_hours = sum(1 for row in results if row["relay_state"] == "ON")
    v2h_active_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["relay_state"] == "ON"
    ]

    if home_energy > 0:
        pv_self_consumption_percent = (
            (pv_energy - baseline_export_energy) / pv_energy
        ) * 100.0 if pv_energy > 0 else 0.0
    else:
        pv_self_consumption_percent = 0.0

    grid_energy_reduction = baseline_import_energy - managed_import_energy

    if baseline_import_energy > 0:
        grid_energy_reduction_percent = (
            grid_energy_reduction / baseline_import_energy
        ) * 100.0
    else:
        grid_energy_reduction_percent = 0.0

    summary = {
        "Total home energy demand (kWh)": round(home_energy, 2),
        "Total PV generation (kWh)": round(pv_energy, 2),
        "Baseline grid import energy (kWh)": round(baseline_import_energy, 2),
        "Managed grid import energy (kWh)": round(managed_import_energy, 2),
        "Grid import reduction (kWh)": round(grid_energy_reduction, 2),
        "Grid import reduction (%)": round(grid_energy_reduction_percent, 1),
        "Baseline grid export energy (kWh)": round(baseline_export_energy, 2),
        "Managed grid export energy (kWh)": round(managed_export_energy, 2),
        "EV discharge energy (kWh)": round(ev_discharge_energy, 2),
        "Baseline peak demand (kW)": round(baseline_peak, 2),
        "Managed peak demand (kW)": round(managed_peak, 2),
        "Peak demand reduction (kW)": round(peak_reduction_kw, 2),
        "Peak demand reduction (%)": round(peak_reduction_percent, 1),
        "Initial SOC (%)": round(results[0]["ev_soc_before_percent"], 2),
        "Final SOC (%)": round(final_soc, 2),
        "Minimum SOC (%)": round(minimum_soc, 2),
        "Relay ON hours": relay_on_hours,
        "V2H active hours": ", ".join(v2h_active_hours),
        "PV self-consumption estimate (%)": round(pv_self_consumption_percent, 1),
    }

    return summary


def print_summary_matrix(summary):
    print("\n================ SUMMARY MATRIX ================")

    for key, value in summary.items():
        print(f"{key:38s}: {value}")

    print("================================================")


def save_summary_matrix(summary, file_path):
    with open(file_path, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["metric", "value"])

        for key, value in summary.items():
            writer.writerow([key, value])


# -----------------------------
# MAIN LOOP
# -----------------------------

def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)

    hourly_log_file = "logs/sunny_v2h_fuzzy_relay_log.csv"
    summary_log_file = "logs/sunny_v2h_summary_matrix.csv"

    results = []

    print("===================================================")
    print(" SUNNY DAY V2H FUZZY RELAY DEMO STARTED")
    print(" Relay ON  = V2H discharge active")
    print(" Relay OFF = hold / no V2H")
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
                "ev_soc_before_percent": round(soc_before, 2),
                "ev_soc_after_percent": round(soc_after, 2),
                "solar_ratio": round(solar_ratio, 3),
                "fuzzy_score": round(fuzzy_score, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "managed_grid_kw": round(managed_grid, 3),
                "relay_state": "ON" if relay_on else "OFF"
            })

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        relay.off()
        print("Relay OFF safely.")

        if len(results) > 0:
            fieldnames = list(results[0].keys())

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
            print("No results were recorded, so no CSV files were saved.")

        print("Demo complete.")


if __name__ == "__main__":
    main()