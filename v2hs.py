from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SCENARIO 4: TOU-AWARE PEAK DEMAND MITIGATION WITH PV + V2H
#
# Relay IN2 -> GPIO27 / physical pin 13
# Relay ON  = EV battery supports household load
# Relay OFF = no V2H discharge
#
# Logic:
# 1. PV supplies home load first.
# 2. Grid import is calculated after PV.
# 3. During evening peak/stress window, V2H is enabled if
#    grid import exceeds the dynamic target limit.
# 4. EV SOC reserve is protected.
# 5. Summary matrix shows benefit and trade-off.
# ============================================================

relay = OutputDevice(27, active_high=True, initial_value=False)

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_RESERVE = 35.0

HOUR_DELAY_SECONDS = 12.5

# Grid import targets
NORMAL_GRID_LIMIT_KW = 3.0
PEAK_GRID_LIMIT_KW = 1.5

# Estimated TOU energy prices
OFF_PEAK_RATE = 0.22
SHOULDER_RATE = 0.30
PEAK_RATE = 0.45

ev_soc = 82.0
initial_soc = ev_soc

hours = list(range(24))

# Synthetic residential demand profile in kW
# Higher values during evening peak due to cooking, heating/cooling,
# lighting and appliance use.
home_load_kw = [
    0.55, 0.45, 0.40, 0.38, 0.42, 0.70,
    1.20, 1.55, 1.30, 1.05, 0.95, 0.90,
    1.00, 1.25, 1.60, 2.20, 3.00, 4.10,
    4.60, 4.30, 3.70, 2.80, 1.60, 0.90
]

# PV generation in kW
# PV is strong during the day and drops in late afternoon.
pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.05,
    0.35, 1.10, 2.00, 2.80, 3.40, 3.80,
    4.00, 3.60, 2.70, 1.60, 0.70, 0.15,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

# EV availability profile
# EV assumed unavailable during work hours and available at home later.
ev_available = [
    1, 1, 1, 1, 1, 1,
    0, 0, 0, 0, 0, 0,
    0, 0, 0, 1, 1, 1,
    1, 1, 1, 1, 1, 1
]


def is_peak_period(hour):
    return 16 <= hour <= 21


def get_tariff_rate(hour):
    if 16 <= hour <= 21:
        return PEAK_RATE
    elif 7 <= hour <= 15 or 22 <= hour <= 23:
        return SHOULDER_RATE
    else:
        return OFF_PEAK_RATE


def get_grid_limit(hour):
    if is_peak_period(hour):
        return PEAK_GRID_LIMIT_KW
    return NORMAL_GRID_LIMIT_KW


def peak_mitigation_controller(hour, load, pv, soc, available):
    pv_used_by_home = min(load, pv)
    pv_surplus = max(pv - load, 0.0)

    grid_before_v2h = max(load - pv, 0.0)
    grid_limit = get_grid_limit(hour)

    excess_above_limit = max(grid_before_v2h - grid_limit, 0.0)

    if available == 0:
        return (
            "EV_NOT_AVAILABLE",
            0.0,
            False,
            grid_before_v2h,
            grid_before_v2h,
            grid_limit,
            excess_above_limit,
            pv_used_by_home,
            pv_surplus
        )

    if soc <= SOC_RESERVE:
        return (
            "LOW_SOC_PROTECTION",
            0.0,
            False,
            grid_before_v2h,
            grid_before_v2h,
            grid_limit,
            excess_above_limit,
            pv_used_by_home,
            pv_surplus
        )

    # Strongest action: evening peak demand mitigation
    if is_peak_period(hour) and excess_above_limit > 0:
        ev_power = min(EV_MAX_POWER_KW, excess_above_limit)
        grid_after_v2h = max(grid_before_v2h - ev_power, 0.0)

        return (
            "PEAK_DEMAND_MITIGATION_ACTIVE",
            ev_power,
            True,
            grid_before_v2h,
            grid_after_v2h,
            grid_limit,
            excess_above_limit,
            pv_used_by_home,
            pv_surplus
        )

    # Optional mild support outside peak only if demand is very high
    if not is_peak_period(hour) and grid_before_v2h > NORMAL_GRID_LIMIT_KW and soc > 60:
        ev_power = min(EV_MAX_POWER_KW, grid_before_v2h - NORMAL_GRID_LIMIT_KW)
        grid_after_v2h = max(grid_before_v2h - ev_power, 0.0)

        return (
            "NON_PEAK_HIGH_DEMAND_SUPPORT",
            ev_power,
            True,
            grid_before_v2h,
            grid_after_v2h,
            grid_limit,
            excess_above_limit,
            pv_used_by_home,
            pv_surplus
        )

    return (
        "HOLD",
        0.0,
        False,
        grid_before_v2h,
        grid_before_v2h,
        grid_limit,
        excess_above_limit,
        pv_used_by_home,
        pv_surplus
    )


def update_soc(soc, ev_power):
    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0
    new_soc = soc - soc_drop
    return max(new_soc, SOC_MIN)


def fmt_hours(hour_list):
    if not hour_list:
        return "None"
    return ", ".join([f"{h:02d}:00" for h in hour_list])


def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)

    hourly_log_file = "logs/scenario4_peak_demand_mitigation_with_pv_log.csv"
    summary_file = "logs/scenario4_peak_demand_mitigation_summary_matrix.csv"

    results = []

    print("===================================================")
    print(" SCENARIO 4: TOU-AWARE PEAK DEMAND MITIGATION")
    print(" PV reduces grid import first; EV limits evening peak")
    print(f" Normal grid limit = {NORMAL_GRID_LIMIT_KW:.1f} kW")
    print(f" Peak grid limit   = {PEAK_GRID_LIMIT_KW:.1f} kW")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = home_load_kw[i]
            pv = pv_generation_kw[i]
            available = ev_available[i]

            soc_before = ev_soc

            (
                decision,
                ev_power,
                relay_on,
                grid_before,
                grid_after,
                grid_limit,
                excess_demand,
                pv_used_by_home,
                pv_surplus
            ) = peak_mitigation_controller(hour, load, pv, ev_soc, available)

            tariff = get_tariff_rate(hour)

            baseline_cost = grid_before * tariff
            managed_cost = grid_after * tariff

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
                f"PV Used={pv_used_by_home:4.2f} kW | "
                f"Grid Before={grid_before:5.2f} kW | "
                f"Limit={grid_limit:4.2f} kW | "
                f"Grid After={grid_after:5.2f} kW | "
                f"SOC={soc_before:5.1f}% -> {soc_after:5.1f}% | "
                f"EV={ev_power:4.2f} kW | "
                f"Cost Save=${baseline_cost - managed_cost:4.2f} | "
                f"Relay={'ON ' if relay_on else 'OFF'} | "
                f"{decision}"
            )

            results.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "home_load_kw": load,
                "pv_generation_kw": pv,
                "pv_used_by_home_kw": round(pv_used_by_home, 3),
                "pv_surplus_kw": round(pv_surplus, 3),
                "grid_import_before_v2h_kw": round(grid_before, 3),
                "grid_limit_kw": grid_limit,
                "excess_demand_kw": round(excess_demand, 3),
                "ev_available": available,
                "soc_before_percent": round(soc_before, 2),
                "soc_after_percent": round(soc_after, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "grid_import_after_v2h_kw": round(grid_after, 3),
                "tariff_rate_aud_per_kwh": tariff,
                "baseline_cost_aud": round(baseline_cost, 3),
                "managed_cost_aud": round(managed_cost, 3),
                "cost_saving_aud": round(baseline_cost - managed_cost, 3),
                "relay_state": "ON" if relay_on else "OFF"
            })

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        relay.off()
        print("Relay OFF safely.")

        with open(hourly_log_file, "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=[
                "timestamp",
                "hour",
                "home_load_kw",
                "pv_generation_kw",
                "pv_used_by_home_kw",
                "pv_surplus_kw",
                "grid_import_before_v2h_kw",
                "grid_limit_kw",
                "excess_demand_kw",
                "ev_available",
                "soc_before_percent",
                "soc_after_percent",
                "decision",
                "ev_power_kw",
                "grid_import_after_v2h_kw",
                "tariff_rate_aud_per_kwh",
                "baseline_cost_aud",
                "managed_cost_aud",
                "cost_saving_aud",
                "relay_state"
            ])

            writer.writeheader()
            writer.writerows(results)

        # -----------------------------
        # Summary matrix calculations
        # -----------------------------

        total_home_load = sum(r["home_load_kw"] for r in results)
        total_pv = sum(r["pv_generation_kw"] for r in results)
        total_pv_used = sum(r["pv_used_by_home_kw"] for r in results)
        total_pv_surplus = sum(r["pv_surplus_kw"] for r in results)

        baseline_grid_import = sum(r["grid_import_before_v2h_kw"] for r in results)
        managed_grid_import = sum(r["grid_import_after_v2h_kw"] for r in results)
        grid_import_reduction = baseline_grid_import - managed_grid_import

        baseline_peak_grid = max(r["grid_import_before_v2h_kw"] for r in results)
        managed_peak_grid = max(r["grid_import_after_v2h_kw"] for r in results)
        peak_reduction = baseline_peak_grid - managed_peak_grid

        peak_hours = [r for r in results if is_peak_period(r["hour"])]
        baseline_peak_energy = sum(r["grid_import_before_v2h_kw"] for r in peak_hours)
        managed_peak_energy = sum(r["grid_import_after_v2h_kw"] for r in peak_hours)
        peak_energy_reduction = baseline_peak_energy - managed_peak_energy

        total_ev_discharge = sum(r["ev_power_kw"] for r in results)
        peak_ev_discharge = sum(r["ev_power_kw"] for r in peak_hours)
        non_peak_ev_discharge = total_ev_discharge - peak_ev_discharge

        baseline_cost = sum(r["baseline_cost_aud"] for r in results)
        managed_cost = sum(r["managed_cost_aud"] for r in results)
        cost_saving = baseline_cost - managed_cost

        relay_on_hours = [r["hour"] for r in results if r["relay_state"] == "ON"]
        peak_support_hours = [
            r["hour"] for r in results
            if r["relay_state"] == "ON" and is_peak_period(r["hour"])
        ]

        grid_stress_before = [
            r["hour"] for r in results
            if r["grid_import_before_v2h_kw"] > r["grid_limit_kw"]
        ]

        grid_stress_after = [
            r["hour"] for r in results
            if r["grid_import_after_v2h_kw"] > r["grid_limit_kw"]
        ]

        ev_unavailable_when_needed = [
            r["hour"] for r in results
            if r["excess_demand_kw"] > 0 and r["ev_available"] == 0
        ]

        low_soc_protection_hours = [
            r["hour"] for r in results
            if r["decision"] == "LOW_SOC_PROTECTION"
        ]

        final_soc = results[-1]["soc_after_percent"]
        minimum_soc = min(r["soc_after_percent"] for r in results)
        soc_drop = initial_soc - final_soc

        pv_self_consumption = (total_pv_used / total_pv) * 100 if total_pv > 0 else 0
        grid_reduction_percent = (grid_import_reduction / baseline_grid_import) * 100 if baseline_grid_import > 0 else 0
        peak_reduction_percent = (peak_reduction / baseline_peak_grid) * 100 if baseline_peak_grid > 0 else 0
        peak_energy_reduction_percent = (peak_energy_reduction / baseline_peak_energy) * 100 if baseline_peak_energy > 0 else 0
        cost_saving_percent = (cost_saving / baseline_cost) * 100 if baseline_cost > 0 else 0

        summary = {
            "Scenario": "TOU-aware peak demand mitigation with PV and V2H",
            "Total home load energy (kWh)": round(total_home_load, 2),
            "Total PV generation (kWh)": round(total_pv, 2),
            "PV used directly by home (kWh)": round(total_pv_used, 2),
            "PV surplus/export estimate (kWh)": round(total_pv_surplus, 2),
            "PV self-consumption (%)": round(pv_self_consumption, 1),

            "Baseline grid import before V2H (kWh)": round(baseline_grid_import, 2),
            "Managed grid import after V2H (kWh)": round(managed_grid_import, 2),
            "Grid import reduction (kWh)": round(grid_import_reduction, 2),
            "Grid import reduction (%)": round(grid_reduction_percent, 1),

            "Baseline peak grid import (kW)": round(baseline_peak_grid, 2),
            "Managed peak grid import (kW)": round(managed_peak_grid, 2),
            "Peak demand reduction (kW)": round(peak_reduction, 2),
            "Peak demand reduction (%)": round(peak_reduction_percent, 1),

            "Peak-period grid import before V2H (kWh)": round(baseline_peak_energy, 2),
            "Peak-period grid import after V2H (kWh)": round(managed_peak_energy, 2),
            "Peak-period import reduction (kWh)": round(peak_energy_reduction, 2),
            "Peak-period import reduction (%)": round(peak_energy_reduction_percent, 1),

            "EV discharge energy (kWh)": round(total_ev_discharge, 2),
            "EV discharge during peak window (kWh)": round(peak_ev_discharge, 2),
            "EV discharge outside peak window (kWh)": round(non_peak_ev_discharge, 2),

            "Initial SOC (%)": round(initial_soc, 2),
            "Final SOC (%)": round(final_soc, 2),
            "Minimum SOC (%)": round(minimum_soc, 2),
            "SOC drop (%)": round(soc_drop, 2),
            "SOC reserve limit (%)": SOC_RESERVE,

            "Relay ON hours": len(relay_on_hours),
            "V2H enabled periods": fmt_hours(relay_on_hours),
            "Peak support periods": fmt_hours(peak_support_hours),
            "Grid stress periods before V2H": fmt_hours(grid_stress_before),
            "Grid stress periods after V2H": fmt_hours(grid_stress_after),
            "EV unavailable when needed": fmt_hours(ev_unavailable_when_needed),
            "Low SOC protection hours": fmt_hours(low_soc_protection_hours),

            "Estimated cost before V2H ($)": round(baseline_cost, 2),
            "Estimated cost after V2H ($)": round(managed_cost, 2),
            "Estimated cost saving ($)": round(cost_saving, 2),
            "Estimated cost saving (%)": round(cost_saving_percent, 1),

            "Main benefit": "Reduced evening peak import and peak-period grid stress",
            "Main trade-off": f"EV SOC reduced by {round(soc_drop, 2)}%"
        }

        print("\n================ PEAK DEMAND MITIGATION SUMMARY MATRIX ================")
        for key, value in summary.items():
            print(f"{key:<50}: {value}")
        print("=======================================================================")

        with open(summary_file, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["Metric", "Value"])
            for key, value in summary.items():
                writer.writerow([key, value])

        print(f"\nHourly log saved to: {hourly_log_file}")
        print(f"Summary matrix saved to: {summary_file}")
        print("Scenario 4 peak demand mitigation simulation complete.")


if __name__ == "__main__":
    main()