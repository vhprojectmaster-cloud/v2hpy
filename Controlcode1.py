

import csv
from datetime import datetime


# -----------------------------
# System specifications
# -----------------------------

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_MAX = 95.0
SOC_RESERVE = 30.0

TIME_STEP_HOURS = 1.0


# -----------------------------
# 24-hour input data
# These values represent your HEMS/V2H project profile.
# Later, we can load these from Excel or CSV.
# -----------------------------

hours = list(range(24))

home_load_kw = [
    0.42, 0.35, 0.30, 0.28, 0.30, 0.48,
    1.10, 1.80, 1.40, 1.20, 1.00, 0.90,
    1.05, 1.10, 1.20, 1.35, 1.70, 2.20,
    2.85, 3.50, 3.20, 2.60, 1.80, 1.20
]

pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    0.18, 0.55, 1.10, 2.20, 3.20, 4.10,
    4.50, 4.20, 3.40, 2.30, 1.20, 0.35,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

# EV availability:
# 1 = EV is at home
# 0 = EV is away
ev_available = [
    1, 1, 1, 1, 1, 1,
    1, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 1,
    1, 1, 1, 1, 1, 1
]


def is_peak_hour(hour: int) -> bool:
    """
    Peak shaving window.
    This matches the idea from your project:
    discharge during evening peak only.
    """
    return 17 <= hour <= 21


def hems_controller(hour: int, net_load_kw: float, soc_percent: float, available: int):
    """
    Simple first controller.

    Later we can replace this with:
    - fuzzy membership functions
    - fuzzy rule base
    - relay control
    - real sensor feedback

    Returns:
        decision_text
        ev_power_kw
    """

    if available == 0:
        return "EV_AWAY_HOLD", 0.0

    if soc_percent <= SOC_RESERVE:
        return "LOW_SOC_PROTECTION", 0.0

    if is_peak_hour(hour) and net_load_kw > 0:
        ev_power_kw = min(EV_MAX_POWER_KW, net_load_kw)
        return "PEAK_SHAVING_DISCHARGE", ev_power_kw

    if net_load_kw < 0 and soc_percent < SOC_MAX:
        charge_power_kw = max(-EV_MAX_POWER_KW, net_load_kw)
        return "SOLAR_SURPLUS_CHARGE", charge_power_kw

    return "HOLD", 0.0


def update_soc(soc_percent: float, ev_power_kw: float) -> float:
    """
    Updates EV SOC.

    Positive ev_power_kw means discharge, so SOC decreases.
    Negative ev_power_kw means charge, so SOC increases.
    """

    energy_change_kwh = ev_power_kw * TIME_STEP_HOURS
    soc_change_percent = (energy_change_kwh / EV_BATTERY_KWH) * 100.0

    new_soc = soc_percent - soc_change_percent

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    return new_soc


def run_simulation():
    soc_percent = 75.0
    results = []

    print("\n===================================================")
    print(" HEMS / V2H RASPBERRY PI CONTROL TEST")
    print("===================================================")
    print("Mode: Peak shaving with simulated PV and EV SOC")
    print("Equation: managed_grid = net_load - ev_power")
    print("---------------------------------------------------")

    for i, hour in enumerate(hours):
        load = home_load_kw[i]
        pv = pv_generation_kw[i]
        available = ev_available[i]

        net_load = load - pv

        decision, ev_power = hems_controller(
            hour=hour,
            net_load_kw=net_load,
            soc_percent=soc_percent,
            available=available
        )

        managed_grid = net_load - ev_power

        print(
            f"{hour:02d}:00 | "
            f"Load={load:5.2f} kW | "
            f"PV={pv:5.2f} kW | "
            f"Net={net_load:6.2f} kW | "
            f"SOC={soc_percent:5.1f}% | "
            f"EV={ev_power:6.2f} kW | "
            f"Grid={managed_grid:6.2f} kW | "
            f"{decision}"
        )

        results.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "hour": hour,
            "home_load_kw": load,
            "pv_generation_kw": pv,
            "net_load_kw": round(net_load, 3),
            "ev_available": available,
            "soc_percent": round(soc_percent, 2),
            "decision": decision,
            "ev_power_kw": round(ev_power, 3),
            "managed_grid_kw": round(managed_grid, 3),
        })

        soc_percent = update_soc(soc_percent, ev_power)

    save_results(results)


def save_results(results):
    output_file = "logs/hems_v2h_sim_results.csv"

    with open(output_file, mode="w", newline="") as file:
        fieldnames = [
            "timestamp",
            "hour",
            "home_load_kw",
            "pv_generation_kw",
            "net_load_kw",
            "ev_available",
            "soc_percent",
            "decision",
            "ev_power_kw",
            "managed_grid_kw",
        ]

        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print("---------------------------------------------------")
    print(f"Results saved to: {output_file}")
    print("Simulation complete.\n")


if __name__ == "__main__":
    run_simulation()