from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SCENARIO 3: EMERGENCY POWER FAILURE MODE WITH SUMMARY MATRIX
#
# Relay IN2 -> GPIO27 / physical pin 13
#
# Relay ON  = critical load supplied from EV battery
# Relay OFF = normal grid supply / no emergency backup
#
# 24 simulated hours compressed into 5 real minutes
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

HOUR_DELAY_SECONDS = 12.5
INITIAL_SOC = 85.0

hours = list(range(24))

# Critical household load only during outage
# Example: light, router, small essential device
critical_load_kw = [
    0.20, 0.18, 0.18, 0.18, 0.20, 0.25,
    0.35, 0.45, 0.40, 0.35, 0.35, 0.30,
    0.30, 0.35, 0.40, 0.45, 0.60, 0.75,
    0.85, 0.80, 0.70, 0.50, 0.35, 0.25
]

# 1 = grid available
# 0 = outage
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


# -----------------------------
# Emergency controller
# -----------------------------

def emergency_controller(hour, load, grid_status, soc, available):
    if grid_status == 1:
        return "GRID_AVAILABLE_NORMAL_MODE", 0.0, False

    if available == 0:
        return "OUTAGE_BUT_EV_NOT_AVAILABLE", 0.0, False

    if soc <= SOC_RESERVE:
        return "OUTAGE_LOW_SOC_PROTECTION", 0.0, False

    ev_power = min(EV_MAX_POWER_KW, load)
    return "EMERGENCY_BACKUP_ACTIVE", ev_power, True


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
    total_critical_load_energy = sum(row["critical_load_kw"] for row in results)

    outage_hours = sum(1 for row in results if row["grid_available"] == 0)
    grid_available_hours = sum(1 for row in results if row["grid_available"] == 1)

    outage_critical_load_energy = sum(
        row["critical_load_kw"]
        for row in results
        if row["grid_available"] == 0
    )

    ev_backup_energy = sum(
        row["ev_power_kw"]
        for row in results
        if row["relay_state"] == "ON"
    )

    # Base case for emergency mode:
    # If there is no EV backup, all critical load during outage is unserved.
    baseline_unserved_energy = outage_critical_load_energy

    # Managed case:
    # During outage, any critical load not supplied by EV is unserved.
    managed_unserved_energy = sum(
        max(row["critical_load_kw"] - row["ev_power_kw"], 0.0)
        for row in results
        if row["grid_available"] == 0
    )

    critical_load_served_energy = outage_critical_load_energy - managed_unserved_energy

    if outage_critical_load_energy > 0:
        backup_coverage_percent = (
            critical_load_served_energy / outage_critical_load_energy
        ) * 100.0
    else:
        backup_coverage_percent = 0.0

    unserved_energy_reduction = baseline_unserved_energy - managed_unserved_energy

    if baseline_unserved_energy > 0:
        unserved_energy_reduction_percent = (
            unserved_energy_reduction / baseline_unserved_energy
        ) * 100.0
    else:
        unserved_energy_reduction_percent = 0.0

    baseline_peak_critical_load = max(row["critical_load_kw"] for row in results)

    outage_peak_critical_load = max(
        row["critical_load_kw"]
        for row in results
        if row["grid_available"] == 0
    )

    max_ev_backup_power = max(row["ev_power_kw"] for row in results)

    initial_soc = results[0]["soc_before_percent"]
    final_soc = results[-1]["soc_after_percent"]
    minimum_soc = min(row["soc_after_percent"] for row in results)
    soc_drop = initial_soc - final_soc

    relay_on_hours = sum(1 for row in results if row["relay_state"] == "ON")

    backup_active_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["relay_state"] == "ON"
    ]

    outage_hours_list = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["grid_available"] == 0
    ]

    ev_unavailable_during_outage_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["grid_available"] == 0 and row["ev_available"] == 0
    ]

    low_soc_protection_hours = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["decision"] == "OUTAGE_LOW_SOC_PROTECTION"
    ]

    summary = {
        "Scenario": "Emergency power failure mode",
        "Total critical load energy over day (kWh)": round(total_critical_load_energy, 2),
        "Grid available hours": grid_available_hours,
        "Outage hours": outage_hours,
        "Outage period": ", ".join(outage_hours_list),
        "Critical load energy during outage (kWh)": round(outage_critical_load_energy, 2),
        "Base case unserved outage energy (kWh)": round(baseline_unserved_energy, 2),
        "Managed unserved outage energy (kWh)": round(managed_unserved_energy, 2),
        "Critical load served by EV (kWh)": round(critical_load_served_energy, 2),
        "EV backup discharge energy (kWh)": round(ev_backup_energy, 2),
        "Backup coverage (%)": round(backup_coverage_percent, 1),
        "Unserved energy reduction (kWh)": round(unserved_energy_reduction, 2),
        "Unserved energy reduction (%)": round(unserved_energy_reduction_percent, 1),
        "Peak critical load over day (kW)": round(baseline_peak_critical_load, 2),
        "Peak critical load during outage (kW)": round(outage_peak_critical_load, 2),
        "Maximum EV backup power (kW)": round(max_ev_backup_power, 2),
        "Initial SOC (%)": round(initial_soc, 2),
        "Final SOC (%)": round(final_soc, 2),
        "Minimum SOC (%)": round(minimum_soc, 2),
        "SOC drop (%)": round(soc_drop, 2),
        "SOC reserve limit (%)": SOC_RESERVE,
        "Relay ON hours": relay_on_hours,
        "Backup active hours": ", ".join(backup_active_hours),
        "EV unavailable during outage hours": ", ".join(ev_unavailable_during_outage_hours),
        "Low SOC protection hours": ", ".join(low_soc_protection_hours),
    }

    return summary


def print_summary_matrix(summary):
    print("\n================ EMERGENCY SUMMARY MATRIX ================")

    for key, value in summary.items():
        print(f"{key:48s}: {value}")

    print("==========================================================")


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

    hourly_log_file = "logs/scenario3_emergency_backup_log.csv"
    summary_log_file = "logs/scenario3_emergency_summary_matrix.csv"

    results = []

    print("===================================================")
    print(" SCENARIO 3: EMERGENCY POWER FAILURE MODE")
    print(" Relay ON = critical load supplied by EV battery")
    print(" Relay OFF = grid supply / no emergency backup")
    print(" 24 simulated hours = 5 real minutes")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = critical_load_kw[i]
            grid_status = grid_available[i]
            available = ev_available[i]

            soc_before = ev_soc

            decision, ev_power, relay_on = emergency_controller(
                hour,
                load,
                grid_status,
                ev_soc,
                available
            )

            if relay_on:
                relay.on()
            else:
                relay.off()

            ev_soc = update_soc(ev_soc, ev_power)
            soc_after = ev_soc

            if grid_status == 0:
                unserved_load = max(load - ev_power, 0.0)
            else:
                unserved_load = 0.0

            print(
                f"{hour:02d}:00 | "
                f"Critical Load={load:4.2f} kW | "
                f"Grid={'ON ' if grid_status else 'OFF'} | "
                f"EV Available={available} | "
                f"SOC={soc_before:5.1f}% -> {soc_after:5.1f}% | "
                f"EV Power={ev_power:4.2f} kW | "
                f"Unserved={unserved_load:4.2f} kW | "
                f"Relay={'ON ' if relay_on else 'OFF'} | "
                f"{decision}"
            )

            results.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "critical_load_kw": load,
                "grid_available": grid_status,
                "ev_available": available,
                "soc_before_percent": round(soc_before, 2),
                "soc_after_percent": round(soc_after, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "unserved_load_kw": round(unserved_load, 3),
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
                "critical_load_kw",
                "grid_available",
                "ev_available",
                "soc_before_percent",
                "soc_after_percent",
                "decision",
                "ev_power_kw",
                "unserved_load_kw",
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

        print("Emergency backup simulation complete.")


if __name__ == "__main__":
    main()