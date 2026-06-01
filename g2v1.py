from time import sleep
from datetime import datetime
import csv
import os

try:
    from gpiozero import OutputDevice
except ImportError:
    # Allows testing on laptop without gpiozero installed
    class OutputDevice:
        def __init__(self, pin, active_high=True, initial_value=False):
            self.pin = pin
            self.active_high = active_high
            self.state = initial_value

        def on(self):
            self.state = True
            print(f"[TEST MODE] GPIO{self.pin} ON")

        def off(self):
            self.state = False
            print(f"[TEST MODE] GPIO{self.pin} OFF")


# ============================================================
# GPIO RELAY SETUP
# ============================================================
# Relay is used ONLY for G2V / charging demonstration.
# V2H discharging is simulated in the code and log only.
#
# For the demo photo:
# Relay ON  = smart SLA charger connected to battery
# Relay OFF = charger disconnected
#
# If your relay works opposite, change active_high=True to False.
# ============================================================

RELAY_GPIO = 27
relay = OutputDevice(RELAY_GPIO, active_high=True, initial_value=False)


# ============================================================
# SYSTEM CONSTANTS
# ============================================================

EV_BATTERY_KWH = 60.0

EV_MAX_DISCHARGE_POWER_KW = 3.3
EV_MAX_CHARGE_POWER_KW = 3.3

CHARGING_EFFICIENCY = 0.90
DISCHARGING_EFFICIENCY = 0.95

SOC_MIN = 20.0
SOC_MAX = 95.0
SOC_RESERVE = 30.0

# Charging target for off-peak / night top-up
SOC_NIGHT_CHARGE_TARGET = 80.0

PV_REFERENCE_KW = 4.0

# 24 simulated hours = 5 real minutes
HOUR_DELAY_SECONDS = 12.5

# Initial simulated EV/SLA battery SOC
ev_soc = 75.0


# ============================================================
# SUNNY DAY BASE CASE DATA
# ============================================================

hours = list(range(24))

# Basic residential home load demand in kW
home_load_kw = [
    0.45, 0.38, 0.35, 0.32, 0.35, 0.55,
    0.90, 1.25, 1.10, 0.95, 0.85, 0.80,
    0.90, 0.95, 1.05, 1.25, 1.65, 2.20,
    2.75, 3.10, 2.80, 2.20, 1.40, 0.85
]

# Sunny day 4 kWp PV generation in kW
pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.05,
    0.35, 0.95, 1.80, 2.70, 3.45, 3.90,
    4.00, 3.75, 3.10, 2.10, 1.00, 0.25,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

# EV / battery available all day for this demo
ev_available = [1] * 24


# ============================================================
# MEMBERSHIP FUNCTIONS
# ============================================================

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
    # Evening peak-shaving period
    return 17 <= hour <= 21


def is_off_peak_hour(hour):
    # Night / early morning charging window
    return 0 <= hour <= 5 or 22 <= hour <= 23


# ============================================================
# V2H FUZZY CONTROLLER
# ============================================================
# This controller still decides when V2H discharge would happen.
# BUT the physical relay will NOT turn on for V2H.
# V2H is simulation/logging only in this charging demo.
# ============================================================

def fuzzy_v2h_controller(hour, load, pv, soc, available):
    net_load = load - pv
    solar_ratio = pv / PV_REFERENCE_KW

    # Hard safety rules
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

    # Score:
    # 0   = hold
    # 60  = slow discharge
    # 80  = medium discharge
    # 100 = fast discharge
    rules = []

    # Hold when solar is enough
    rules.append((solar_surplus, 0.0))
    rules.append((solar_high, 0.0))
    rules.append((balanced_load, 0.0))
    rules.append((soc_low, 0.0))

    # V2H discharge during peak demand
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

    if fuzzy_score >= 55.0 and net_load > 0:
        v2h_requested = True
        v2h_power = min(EV_MAX_DISCHARGE_POWER_KW, net_load)

        if fuzzy_score >= 85.0:
            decision = "FAST_V2H_DISCHARGE_SIM_ONLY"
        else:
            decision = "SLOW_V2H_DISCHARGE_SIM_ONLY"
    else:
        v2h_requested = False
        v2h_power = 0.0
        decision = "NO_V2H"

    return decision, v2h_power, v2h_requested, fuzzy_score, net_load, solar_ratio


# ============================================================
# G2V CHARGING CONTROLLER
# ============================================================
# This is the ONLY part allowed to switch the relay ON.
# The relay connects the smart SLA charger to the battery.
# ============================================================

def g2v_charging_controller(hour, load, pv, soc, available):
    net_load = load - pv
    solar_surplus_kw = max(0.0, pv - load)

    if available == 0:
        return "CHARGING_BLOCKED_EV_NOT_AVAILABLE", 0.0, False

    if soc >= SOC_MAX:
        return "CHARGING_BLOCKED_SOC_MAX", 0.0, False

    # Case 1: Midday solar surplus charging
    # This represents charging when PV generation is higher than home load.
    if solar_surplus_kw > 0.30 and 9 <= hour <= 15:
        remaining_capacity_kw_equivalent = ((SOC_MAX - soc) / 100.0) * EV_BATTERY_KWH
        charge_power = min(EV_MAX_CHARGE_POWER_KW, solar_surplus_kw, remaining_capacity_kw_equivalent)

        if charge_power > 0:
            return "G2V_CHARGE_FROM_SOLAR_SURPLUS", charge_power, True

    # Case 2: Off-peak charging top-up
    # This represents grid-to-vehicle / charger-to-battery charging at low demand hours.
    if is_off_peak_hour(hour) and soc < SOC_NIGHT_CHARGE_TARGET:
        remaining_capacity_kw_equivalent = ((SOC_NIGHT_CHARGE_TARGET - soc) / 100.0) * EV_BATTERY_KWH
        charge_power = min(EV_MAX_CHARGE_POWER_KW, remaining_capacity_kw_equivalent)

        if charge_power > 0:
            return "G2V_OFF_PEAK_TOP_UP_CHARGE", charge_power, True

    return "NO_G2V_CHARGING", 0.0, False


# ============================================================
# SOC UPDATE
# ============================================================

def update_soc(soc, v2h_power_kw, g2v_charge_power_kw):
    # V2H power reduces SOC in simulation only.
    soc_drop = (v2h_power_kw / EV_BATTERY_KWH) * 100.0 / DISCHARGING_EFFICIENCY

    # G2V charging increases SOC.
    soc_gain = (g2v_charge_power_kw * CHARGING_EFFICIENCY / EV_BATTERY_KWH) * 100.0

    new_soc = soc - soc_drop + soc_gain

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    return new_soc


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)
    log_file = "logs/sunny_g2v_charging_demo_with_v2h_sim_log.csv"

    results = []

    print("================================================================")
    print(" SUNNY DAY G2V CHARGING DEMO WITH V2H SIMULATION STARTED")
    print(" Relay ON  = G2V charging active, SLA charger connected")
    print(" Relay OFF = charger disconnected")
    print(" V2H mode  = simulated/logged only, relay stays OFF")
    print(" 24 simulated hours = 5 real minutes")
    print("================================================================")

    try:
        for i, hour in enumerate(hours):
            load = home_load_kw[i]
            pv = pv_generation_kw[i]
            available = ev_available[i]

            # First calculate V2H request from fuzzy logic
            v2h_decision, v2h_power, v2h_requested, fuzzy_score, net_load, solar_ratio = fuzzy_v2h_controller(
                hour,
                load,
                pv,
                ev_soc,
                available
            )

            # Then calculate charging request
            g2v_decision, g2v_charge_power, g2v_relay_request = g2v_charging_controller(
                hour,
                load,
                pv,
                ev_soc,
                available
            )

            # ------------------------------------------------------------
            # IMPORTANT HARDWARE RULE
            # ------------------------------------------------------------
            # Physical relay is allowed to turn ON only for G2V charging.
            # Even if V2H is requested, the relay must stay OFF.
            # ------------------------------------------------------------

            if g2v_relay_request:
                hardware_relay_on = True
                operation_mode = "G2V_CHARGING_RELAY_ON"

                # Do not simulate V2H discharge at the same time as charging
                final_v2h_power = 0.0
                final_g2v_charge_power = g2v_charge_power
                final_decision = g2v_decision

            elif v2h_requested:
                hardware_relay_on = False
                operation_mode = "V2H_SIM_ONLY_RELAY_OFF"

                final_v2h_power = v2h_power
                final_g2v_charge_power = 0.0
                final_decision = v2h_decision

            else:
                hardware_relay_on = False
                operation_mode = "HOLD_RELAY_OFF"

                final_v2h_power = 0.0
                final_g2v_charge_power = 0.0
                final_decision = "HOLD"

            # Apply hardware relay command
            if hardware_relay_on:
                relay.on()
            else:
                relay.off()

            managed_grid = net_load - final_v2h_power + final_g2v_charge_power

            print(
                f"{hour:02d}:00 | "
                f"Load={load:4.2f} kW | "
                f"PV={pv:4.2f} kW | "
                f"Net={net_load:5.2f} kW | "
                f"SOC={ev_soc:5.1f}% | "
                f"Fuzzy={fuzzy_score:5.1f} | "
                f"V2H_sim={final_v2h_power:4.2f} kW | "
                f"G2V_charge={final_g2v_charge_power:4.2f} kW | "
                f"Grid={managed_grid:5.2f} kW | "
                f"Relay={'ON ' if hardware_relay_on else 'OFF'} | "
                f"{operation_mode} | "
                f"{final_decision}"
            )

            results.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "home_load_kw": load,
                "pv_generation_kw": pv,
                "net_load_kw": round(net_load, 3),
                "ev_soc_percent_before_update": round(ev_soc, 2),
                "solar_ratio": round(solar_ratio, 3),
                "fuzzy_v2h_score": round(fuzzy_score, 2),
                "v2h_requested_by_fuzzy": "YES" if v2h_requested else "NO",
                "v2h_power_simulated_kw": round(final_v2h_power, 3),
                "g2v_charge_power_kw": round(final_g2v_charge_power, 3),
                "managed_grid_kw": round(managed_grid, 3),
                "operation_mode": operation_mode,
                "decision": final_decision,
                "hardware_relay_state": "ON" if hardware_relay_on else "OFF",
                "relay_note": "Relay ON only for G2V charging, never for V2H in this demo"
            })

            ev_soc = update_soc(
                ev_soc,
                final_v2h_power,
                final_g2v_charge_power
            )

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        relay.off()
        print("Relay OFF safely.")

        with open(log_file, "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=[
                "timestamp",
                "hour",
                "home_load_kw",
                "pv_generation_kw",
                "net_load_kw",
                "ev_soc_percent_before_update",
                "solar_ratio",
                "fuzzy_v2h_score",
                "v2h_requested_by_fuzzy",
                "v2h_power_simulated_kw",
                "g2v_charge_power_kw",
                "managed_grid_kw",
                "operation_mode",
                "decision",
                "hardware_relay_state",
                "relay_note"
            ])

            writer.writeheader()
            writer.writerows(results)

        print(f"Log saved to: {log_file}")
        print("Demo complete.")


if __name__ == "__main__":
    main()