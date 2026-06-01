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
initial_ev_soc = ev_soc

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


# ============================================================
# FUZZY MEMBERSHIP FUNCTIONS
# ============================================================

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


# ============================================================
# FUZZY EMERGENCY CONTROLLER
# ============================================================

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


# ============================================================
# SOC UPDATE
# ============================================================

def update_soc(soc, ev_power):
    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0
    return max(soc - soc_drop, SOC_MIN)


# ============================================================
# SUMMARY MATRIX FUNCTIONS
# ============================================================

def count_decisions(results):
    decision_counts = {}

    for row in results:
        decision = row["decision"]

        if decision not in decision_counts:
            decision_counts[decision] = 0

        decision_counts[decision] += 1

    return decision_counts


def build_summary_matrix(results, initial_soc, final_soc):
    total_hours = len(results)

    outage_hours = sum(1 for row in results if row["grid_available"] == 0)
    grid_available_hours = sum(1 for row in results if row["grid_available"] == 1)

    ev_available_hours = sum(1 for row in results if row["ev_available"] == 1)
    ev_available_during_outage_hours = sum(
        1 for row in results
        if row["grid_available"] == 0 and row["ev_available"] == 1
    )

    relay_on_hours = sum(1 for row in results if row["relay_state"] == "ON")
    relay_off_hours = total_hours - relay_on_hours

    backup_active_hours = sum(
        1 for row in results
        if row["decision"] == "FUZZY_EMERGENCY_BACKUP_ACTIVE"
    )

    outage_but_ev_not_available_hours = sum(
        1 for row in results
        if row["decision"] == "OUTAGE_BUT_EV_NOT_AVAILABLE"
    )

    low_soc_protection_hours = sum(
        1 for row in results
        if row["decision"] == "OUTAGE_LOW_SOC_PROTECTION"
    )

    total_critical_load_kwh = sum(row["critical_load_kw"] for row in results)
    outage_critical_load_kwh = sum(
        row["critical_load_kw"] for row in results
        if row["grid_available"] == 0
    )

    ev_energy_supplied_kwh = sum(row["ev_power_kw"] for row in results)

    unsupported_outage_load_kwh = sum(
        row["critical_load_kw"] for row in results
        if row["grid_available"] == 0 and row["relay_state"] == "OFF"
    )

    supported_outage_load_kwh = sum(
        row["ev_power_kw"] for row in results
        if row["grid_available"] == 0 and row["relay_state"] == "ON"
    )

    soc_drop_percent = initial_soc - final_soc

    max_fuzzy_strength = max(row["fuzzy_backup_strength_percent"] for row in results)
    avg_fuzzy_strength = sum(row["fuzzy_backup_strength_percent"] for row in results) / total_hours

    relay_on_hours_list = [
        f"{row['hour']:02d}:00" for row in results
        if row["relay_state"] == "ON"
    ]

    outage_hours_list = [
        f"{row['hour']:02d}:00" for row in results
        if row["grid_available"] == 0
    ]

    summary_matrix = [
        {
            "category": "Simulation",
            "metric": "Total simulated hours",
            "value": total_hours,
            "unit": "hours",
            "explanation": "Full 24-hour emergency scenario"
        },
        {
            "category": "Grid",
            "metric": "Grid available hours",
            "value": grid_available_hours,
            "unit": "hours",
            "explanation": "Hours where normal grid supply was available"
        },
        {
            "category": "Grid",
            "metric": "Power outage hours",
            "value": outage_hours,
            "unit": "hours",
            "explanation": "Hours where grid supply was unavailable"
        },
        {
            "category": "EV Availability",
            "metric": "EV available hours",
            "value": ev_available_hours,
            "unit": "hours",
            "explanation": "Hours where EV/battery was available at home"
        },
        {
            "category": "EV Availability",
            "metric": "EV available during outage",
            "value": ev_available_during_outage_hours,
            "unit": "hours",
            "explanation": "Useful overlap between outage and EV availability"
        },
        {
            "category": "Relay",
            "metric": "Relay ON hours",
            "value": relay_on_hours,
            "unit": "hours",
            "explanation": "Hours where relay connected EV backup supply to critical load"
        },
        {
            "category": "Relay",
            "metric": "Relay OFF hours",
            "value": relay_off_hours,
            "unit": "hours",
            "explanation": "Hours where backup relay stayed disconnected"
        },
        {
            "category": "Backup Operation",
            "metric": "Emergency backup active hours",
            "value": backup_active_hours,
            "unit": "hours",
            "explanation": "Fuzzy controller activated EV emergency backup"
        },
        {
            "category": "Backup Operation",
            "metric": "Outage but EV unavailable",
            "value": outage_but_ev_not_available_hours,
            "unit": "hours",
            "explanation": "Outage happened but EV was not available for backup"
        },
        {
            "category": "Backup Operation",
            "metric": "Low SOC protection hours",
            "value": low_soc_protection_hours,
            "unit": "hours",
            "explanation": "Backup blocked because SOC reached reserve limit"
        },
        {
            "category": "Energy",
            "metric": "Total critical load demand",
            "value": round(total_critical_load_kwh, 3),
            "unit": "kWh",
            "explanation": "Total critical load across the full 24-hour scenario"
        },
        {
            "category": "Energy",
            "metric": "Critical load during outage",
            "value": round(outage_critical_load_kwh, 3),
            "unit": "kWh",
            "explanation": "Critical load demand only during grid outage hours"
        },
        {
            "category": "Energy",
            "metric": "EV energy supplied during backup",
            "value": round(ev_energy_supplied_kwh, 3),
            "unit": "kWh",
            "explanation": "Energy supplied by EV/battery when relay was ON"
        },
        {
            "category": "Energy",
            "metric": "Supported outage load",
            "value": round(supported_outage_load_kwh, 3),
            "unit": "kWh",
            "explanation": "Outage load successfully supplied by EV backup"
        },
        {
            "category": "Energy",
            "metric": "Unsupported outage load",
            "value": round(unsupported_outage_load_kwh, 3),
            "unit": "kWh",
            "explanation": "Outage load not supplied because relay stayed OFF"
        },
        {
            "category": "Battery SOC",
            "metric": "Initial SOC",
            "value": round(initial_soc, 2),
            "unit": "%",
            "explanation": "Battery SOC at start of simulation"
        },
        {
            "category": "Battery SOC",
            "metric": "Final SOC",
            "value": round(final_soc, 2),
            "unit": "%",
            "explanation": "Battery SOC after the 24-hour scenario"
        },
        {
            "category": "Battery SOC",
            "metric": "SOC used",
            "value": round(soc_drop_percent, 2),
            "unit": "%",
            "explanation": "SOC drop due to emergency backup discharge"
        },
        {
            "category": "Fuzzy Logic",
            "metric": "Maximum fuzzy backup strength",
            "value": round(max_fuzzy_strength, 2),
            "unit": "%",
            "explanation": "Strongest fuzzy backup command during the scenario"
        },
        {
            "category": "Fuzzy Logic",
            "metric": "Average fuzzy backup strength",
            "value": round(avg_fuzzy_strength, 2),
            "unit": "%",
            "explanation": "Average fuzzy controller strength across all hours"
        },
        {
            "category": "Timing",
            "metric": "Outage hours",
            "value": ", ".join(outage_hours_list) if outage_hours_list else "None",
            "unit": "-",
            "explanation": "Hours where grid was OFF"
        },
        {
            "category": "Timing",
            "metric": "Relay ON hours",
            "value": ", ".join(relay_on_hours_list) if relay_on_hours_list else "None",
            "unit": "-",
            "explanation": "Hours where EV backup was physically activated"
        }
    ]

    decision_counts = count_decisions(results)

    for decision, count in decision_counts.items():
        summary_matrix.append({
            "category": "Decision Count",
            "metric": decision,
            "value": count,
            "unit": "hours",
            "explanation": "Number of hours this controller decision occurred"
        })

    return summary_matrix


def print_summary_matrix(summary_matrix):
    print("\n===================================================")
    print(" SUMMARY MATRIX")
    print("===================================================")

    for row in summary_matrix:
        print(
            f"{row['category']:<18} | "
            f"{row['metric']:<38} | "
            f"{str(row['value']):<18} | "
            f"{row['unit']:<6} | "
            f"{row['explanation']}"
        )


def save_summary_matrix(summary_file, summary_matrix):
    with open(summary_file, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=[
            "category",
            "metric",
            "value",
            "unit",
            "explanation"
        ])

        writer.writeheader()
        writer.writerows(summary_matrix)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)

    log_file = "logs/scenario3_fuzzy_emergency_backup_log.csv"
    summary_file = "logs/scenario3_fuzzy_emergency_summary_matrix.csv"

    results = []

    print("===================================================")
    print(" SCENARIO 3: FUZZY EMERGENCY POWER FAILURE MODE")
    print(" Relay ON  = EV battery supplies critical load")
    print(" Relay OFF = normal mode or backup blocked")
    print(" 24 simulated hours = 5 real minutes")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = critical_load_kw[i]
            grid_status = grid_available[i]
            available = ev_available[i]

            decision, ev_power, relay_on, fuzzy_strength = fuzzy_emergency_controller(
                load,
                grid_status,
                ev_soc,
                available
            )

            if relay_on:
                relay.on()
            else:
                relay.off()

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

        summary_matrix = build_summary_matrix(
            results,
            initial_ev_soc,
            ev_soc
        )

        print_summary_matrix(summary_matrix)
        save_summary_matrix(summary_file, summary_matrix)

        print("\nRelay OFF safely.")
        print(f"Hourly log saved to: {log_file}")
        print(f"Summary matrix saved to: {summary_file}")
        print("Demo complete.")


if __name__ == "__main__":
    main()