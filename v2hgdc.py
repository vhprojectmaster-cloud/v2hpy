from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SCENARIO 4: GRID DEMAND CONTROL WITH PV
#
# Relay IN2 -> GPIO27 / physical pin 13
# Relay ON  = EV battery supports excess grid demand
# Relay OFF = grid demand is within selected limit
#
# Priority:
# PV reduces household grid import first.
# EV supports only if remaining grid import exceeds the limit.
# ============================================================

relay = OutputDevice(27, active_high=True, initial_value=False)

EV_BATTERY_KWH = 60.0
EV_MAX_POWER_KW = 3.3

SOC_MIN = 20.0
SOC_RESERVE = 35.0

GRID_IMPORT_LIMIT_KW = 2.0
HOUR_DELAY_SECONDS = 12.5

ev_soc = 82.0
hours = list(range(24))

home_load_kw = [
    0.50, 0.42, 0.38, 0.35, 0.40, 0.65,
    1.10, 1.35, 1.05, 0.95, 0.90, 0.85,
    0.95, 1.20, 1.55, 2.10, 2.65, 3.10,
    3.45, 3.25, 2.80, 2.20, 1.35, 0.80
]

# PV included here.
# It reduces the grid import before EV/V2H support is considered.
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

def grid_demand_controller(load, pv, soc, available):
    grid_before_v2h = max(load - pv, 0.0)
    excess_demand = grid_before_v2h - GRID_IMPORT_LIMIT_KW

    if available == 0:
        return "EV_NOT_AVAILABLE", 0.0, False, grid_before_v2h, max(excess_demand, 0.0)

    if soc <= SOC_RESERVE:
        return "LOW_SOC_PROTECTION", 0.0, False, grid_before_v2h, max(excess_demand, 0.0)

    if excess_demand > 0:
        ev_power = min(EV_MAX_POWER_KW, excess_demand)
        return "GRID_LIMIT_EXCEEDED_EV_SUPPORT_ACTIVE", ev_power, True, grid_before_v2h, excess_demand

    return "GRID_WITHIN_LIMIT_AFTER_PV", 0.0, False, grid_before_v2h, 0.0

def update_soc(soc, ev_power):
    soc_drop = (ev_power / EV_BATTERY_KWH) * 100.0
    return max(soc - soc_drop, SOC_MIN)

def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)
    log_file = "logs/scenario4_grid_demand_control_with_pv_log.csv"
    results = []

    print("===================================================")
    print(" SCENARIO 4: GRID DEMAND CONTROL WITH PV")
    print(" PV reduces grid import first; EV limits excess demand")
    print(f" Grid import limit = {GRID_IMPORT_LIMIT_KW:.1f} kW")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = home_load_kw[i]
            pv = pv_generation_kw[i]
            available = ev_available[i]

            decision, ev_power, relay_on, grid_before, excess_demand = grid_demand_controller(
                load, pv, ev_soc, available
            )

            grid_after = max(grid_before - ev_power, 0.0)

            if relay_on:
                relay.on()
            else:
                relay.off()

            print(
                f"{hour:02d}:00 | "
                f"Load={load:4.2f} kW | "
                f"PV={pv:4.2f} kW | "
                f"Grid Before={grid_before:5.2f} kW | "
                f"Limit={GRID_IMPORT_LIMIT_KW:4.2f} kW | "
                f"Excess={excess_demand:4.2f} kW | "
                f"EV Available={available} | "
                f"SOC={ev_soc:5.1f}% | "
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
                "excess_demand_kw": round(excess_demand, 3),
                "ev_available": available,
                "ev_soc_percent": round(ev_soc, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "grid_import_after_v2h_kw": round(grid_after, 3),
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
                "home_load_kw",
                "pv_generation_kw",
                "grid_import_before_v2h_kw",
                "grid_import_limit_kw",
                "excess_demand_kw",
                "ev_available",
                "ev_soc_percent",
                "decision",
                "ev_power_kw",
                "grid_import_after_v2h_kw",
                "relay_state"
            ])
            writer.writeheader()
            writer.writerows(results)

        print("Relay OFF safely.")
        print(f"Log saved to: {log_file}")
        print("Demo complete.")

if _name_ == "_main_":
    main()