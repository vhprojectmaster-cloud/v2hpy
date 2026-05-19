from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SCENARIO 4: GRID DEMAND CONTROL WITH PV + SUMMARY MATRIX
#
# Relay IN2 -> GPIO27 / physical pin 13
#
# Relay ON  = EV battery supports excess grid demand
# Relay OFF = grid demand is within selected limit
#
# Priority:
# 1. PV reduces household grid import first.
# 2. EV supports only if remaining grid import exceeds the limit.
#
# 24 simulated hours = 5 real minutes
# 1 simulated hour = 12.5 seconds
# ============================================================

relay = OutputDevice(27, active_high=True, initial_value=False)

# -----------------------------
# System settings
# -----------------------------

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_RESERVE = 35.0

GRID_IMPORT_LIMIT_KW = 2.0
HOUR_DELAY_SECONDS = 12.5

INITIAL_SOC = 82.0

hours = list(range(24))

home_load_kw = [
    0.50, 0.42, 0.38, 0.35, 0.40, 0.65,
    1.10, 1.35, 1.05, 0.95, 0.90, 0.85,
    0.95, 1.20, 1.55, 2.10, 2.65, 3.10,
    3.45, 3.25, 2.80, 2.20, 1.35, 0.80
]

# PV reduces grid import before EV/V2H support is considered.
pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.05,
    0.30, 0.85, 1.40, 2.00, 2.60, 2.90,
    3.00, 2.70, 2.10, 1.30, 0.60, 0.15,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

ev_available = [
    1, 1, 1, 1, 1, 1,
    0, 0, 0, 0, 0, 0,
    0, 0, 0, 1, 1, 1,
    1, 1, 1, 1, 1, 1
]


# -----------------------------
# Grid demand controller
# -----------------------------

def grid_demand_controller(load, pv, soc, available):
    grid_before_v2h = max(load - pv, 0.0)
    excess_demand = grid_before_v2h - GRID_IMPORT_LIMIT_KW

    if available == 0:
        return (
            "EV_NOT_AVAILABLE",
            0.0,
            False,
            grid_before_v2h,
            max(excess_demand, 0.0)
        )

    if soc <= SOC_RESERVE:
        return (
            "LOW_SOC_PROTECTION",
            0.0,
            False,
            grid_before_v2h,
            max(excess_demand, 0.0)
        )

    if excess_demand > 0:
        ev_power = min(EV_MAX_POWER_KW, excess_demand)
        return (
            "GRID_LIMIT_EXCEEDED_EV_SUPPORT_ACTIVE",
            ev_power,
            True,
            grid_before_v2h,
            excess_demand
        )

    return (
        "GRID_WITHIN_LIMIT_AFTER_PV",
        0.0,
        False,
        grid_before_v2h,
        0.0
    )


# -----------------------------
# SOC update
# -----------------------------

def update_soc(soc, ev_power):
    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0
    new_soc = soc - soc_drop

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    return new_soc


# -----------------------------
# Summary matrix
# -----------------------------

def create_summary_matrix(results):
    total_home_energy = sum(row["home_load_kw"] for row in results)
    total_pv_energy = sum(row["pv_generation_kw"] for row in results)

    pv_used_directly = sum(
        min(row["home_load_kw"], row["pv_generation_kw"])
        for row in results
    )

    pv_export_or_surplus = sum(
        max(row["pv_generation_kw"] - row["home_load_kw"], 0.0)
        for row in results
    )

    grid_import_before_v2h = sum(
        row["grid_import_before_v2h_kw"]
        for row in results
    )

    grid_import_after_v2h = sum(
        row["grid_import_after_v2h_kw"]
        for row in results
    )

    grid_import_reduction = grid_import_before_v2h - grid_import_after_v2h

    if grid_import_before_v2h > 0:
        grid_import_reduction_percent = (
            grid_import_reduction / grid_import_before_v2h
        ) * 100.0
    else:
        grid_import_reduction_percent = 0.0

    baseline_peak_import = max(
        row["grid_import_before_v2h_kw"]
        for row in results
    )

    managed_peak_import = max(
        row["grid_import_after_v2h_kw"]
        for row in results
    )

    peak_reduction_kw = baseline_peak_import - managed_peak_import

    if baseline_peak_import > 0:
        peak_reduction_percent = (
            peak_reduction_kw / baseline_peak_import
        ) * 100.0
    else:
        peak_reduction_percent = 0.0

    baseline_excess_energy = sum(
        max(row["grid_import_before_v2h_kw"] - row["grid_import_limit_kw"], 0.0)
        for row in results
    )

    managed_excess_energy = sum(
        max(row["grid_import_after_v2h_kw"] - row["grid_import_limit_kw"], 0.0)
        for row in results
    )

    excess_reduction = baseline_excess_energy - managed_excess_energy

    if baseline_excess_energy > 0:
        excess_reduction_percent = (
            excess_reduction / baseline_excess_energy
        ) * 100.0
    else:
        excess_reduction_percent = 0.0

    ev_discharge_energy = sum(
        max(row["ev_power_kw"], 0.0)
        for row in results
    )

    initial_soc = results[0]["soc_before_percent"]
    final_soc = results[-1]["soc_after_percent"]
    minimum_soc = min(row["soc_after_percent"] for row in results)
    soc_drop = initial_soc - final_soc

    relay_on_hours = sum(
        1 for row in results
        if row["relay_state"] == "ON"
    )

    relay_on_periods = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["relay_state"] == "ON"
    ]

    grid_limit_exceeded_before_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["grid_import_before_v2h_kw"] > row["grid_import_limit_kw"]
    ]

    grid_limit_exceeded_after_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["grid_import_after_v2h_kw"] > row["grid_import_limit_kw"]
    ]

    ev_unavailable_when_limit_exceeded = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["ev_available"] == 0
        and row["grid_import_before_v2h_kw"] > row["grid_import_limit_kw"]
    ]

    low_soc_protection_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["decision"] == "LOW_SOC_PROTECTION"
    ]

    if total_pv_energy > 0:
        pv_self_consumption_percent = (
            pv_used_directly / total_pv_energy
        ) * 100.0
    else:
        pv_self_consumption_percent = 0.0

    summary = {
        "Scenario": "Grid demand control with PV",
        "Grid import limit (kW)": GRID_IMPORT_LIMIT_KW,
        "Total home load energy (kWh)": round(total_home_energy, 2),
        "Total PV generation (kWh)": round(total_pv_energy, 2),
        "PV used directly by home (kWh)": round(pv_used_directly, 2),
        "PV surplus/export estimate (kWh)": round(pv_export_or_surplus, 2),
        "PV self-consumption estimate (%)": round(pv_self_consumption_percent, 1),
        "Baseline grid import before V2H (kWh)": round(grid_import_before_v2h, 2),
        "Managed grid import after V2H (kWh)": round(grid_import_after_v2h, 2),
        "Grid import reduction (kWh)": round(grid_import_reduction, 2),
        "Grid import reduction (%)": round(grid_import_reduction_percent, 1),
        "Baseline peak grid import (kW)": round(baseline_peak_import, 2),
        "Managed peak grid import (kW)": round(managed_peak_import, 2),
        "Peak grid import reduction (kW)": round(peak_reduction_kw, 2),
        "Peak grid import reduction (%)": round(peak_reduction_percent, 1),
        "Baseline energy above limit (kWh)": round(baseline_excess_energy, 2),
        "Managed energy above limit (kWh)": round(managed_excess_energy, 2),
        "Energy above limit reduction (kWh)": round(excess_reduction, 2),
        "Energy above limit reduction (%)": round(excess_reduction_percent, 1),
        "EV discharge energy (kWh)": round(ev_discharge_energy, 2),
        "Initial SOC (%)": round(initial_soc, 2),
        "Final SOC (%)": round(final_soc, 2),
        "Minimum SOC (%)": round(minimum_soc, 2),
        "SOC drop (%)": round(soc_drop, 2),
        "SOC reserve limit (%)": SOC_RESERVE,
        "Relay ON hours": relay_on_hours,
        "Relay ON periods": ", ".join(relay_on_periods),
        "Grid limit exceeded before V2H": ", ".join(grid_limit_exceeded_before_hours),
        "Grid limit exceeded after V2H": ", ".join(grid_limit_exceeded_after_hours),
        "EV unavailable when limit exceeded": ", ".join(ev_unavailable_when_limit_exceeded),
        "Low SOC protection hours": ", ".join(low_soc_protection_hours),
    }

    return summary


def print_summary_matrix(summary):
    print("\n================ GRID DEMAND SUMMARY MATRIX ================")

    for key, value in summary.items():
        print(f"{key:48s}: {value}")

    print("============================================================")


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

    hourly_log_file = "logs/scenario4_grid_demand_control_with_pv_log.csv"
    summary_log_file = "logs/scenario4_grid_demand_summary_matrix.csv"

    results = []

    print("===================================================")
    print(" SCENARIO 4: GRID DEMAND CONTROL WITH PV")
    print(" PV reduces grid import first; EV limits excess demand")
    print(f" Grid import limit = {GRID_IMPORT_LIMIT_KW:.1f} kW")
    print(" Relay ON = EV battery supports excess grid demand")
    print(" 24 simulated hours = 5 real minutes")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = home_load_kw[i]
            pv = pv_generation_kw[i]
            available = ev_available[i]

            soc_before = ev_soc

            decision, ev_power, relay_on, grid_before, excess_demand = grid_demand_controller(
                load,
                pv,
                ev_soc,
                available
            )

            grid_after = max(grid_before - ev_power, 0.0)

            if relay_on:
                relay.on()
            else:
                relay.off()

            ev_soc = update_soc(ev_soc, ev_power)
            soc_after = ev_soc

            after_excess_demand = max(grid_after - GRID_IMPORT_LIMIT_KW, 0.0)

            print(
                f"{hour:02d}:00 | "
                f"Load={load:4.2f} kW | "
                f"PV={pv:4.2f} kW | "
                f"Grid Before={grid_before:5.2f} kW | "
                f"Limit={GRID_IMPORT_LIMIT_KW:4.2f} kW | "
                f"Excess Before={excess_demand:4.2f} kW | "
                f"Excess After={after_excess_demand:4.2f} kW | "
                f"EV Available={available} | "
                f"SOC={soc_before:5.1f}% -> {soc_after:5.1f}% | "
                f"EV Power={ev_power:4.2f} kW | "
                f"Grid After={grid_after:5.2f} kW | "
                f"Relay={'ON ' if relay_on else 'OFF'} | "
                f"{decision}"
            )

            results.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "home_load_kw": load,
                "pv_generation_kw": pv,
                "grid_import_before_v2h_kw": round(grid_before, 3),
                "grid_import_limit_kw": GRID_IMPORT_LIMIT_KW,
                "excess_demand_before_v2h_kw": round(excess_demand, 3),
                "excess_demand_after_v2h_kw": round(after_excess_demand, 3),
                "ev_available": available,
                "soc_before_percent": round(soc_before, 2),
                "soc_after_percent": round(soc_after, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "grid_import_after_v2h_kw": round(grid_after, 3),
                "relay_state": "ON" if relay_on else "OFF"
            })

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        relay.off()
        print("Relay OFF safely.")

        if len(results) > 0:
            fieldnames = [
                "timestamp",
                "hour",
                "home_load_kw",
                "pv_generation_kw",
                "grid_import_before_v2h_kw",
                "grid_import_limit_kw",
                "excess_demand_before_v2h_kw",
                "excess_demand_after_v2h_kw",
                "ev_available",
                "soc_before_percent",
                "soc_after_percent",
                "decision",
                "ev_power_kw",
                "grid_import_after_v2h_kw",
                "relay_state"
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

        print("Scenario 4 grid demand control simulation complete.")


if __name__ == "__main__":
    main()