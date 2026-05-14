from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# CUSTOM USER-BASED HEMS + PV + V2H FUZZY RELAY CONTROLLER
#
# Hardware:
# Relay IN2 -> Raspberry Pi GPIO27 / physical pin 13
#
# Relay/Lamp:
# ON  = V2H discharge active
# OFF = hold / charging / EV unavailable
#
# User inputs before each run:
# 1. Current EV SOC
# 2. Minimum reserve SOC
# 3. Morning reserve SOC
# 4. EV availability in night/morning/afternoon/evening
#
# 24 simulated hours = 5 real minutes
# 1 simulated hour = 12.5 seconds
# ============================================================


# -----------------------------
# RELAY SETUP
# -----------------------------
# Your relay was previously fixed using active_high=True.
# If the lamp works opposite, change True to False.

relay = OutputDevice(27, active_high=True, initial_value=False)


# -----------------------------
# SYSTEM CONSTANTS
# -----------------------------

EV_BATTERY_KWH = 60.0
EV_MAX_DISCHARGE_KW = 3.3
EV_MAX_CHARGE_KW = 3.3

SOC_MIN_HARD = 20.0
SOC_MAX = 95.0

PV_REFERENCE_KW = 4.0

# 5-minute demo
HOUR_DELAY_SECONDS = 12.5


# -----------------------------
# REPRESENTATIVE SUNNY WEEKDAY DATA
# -----------------------------
# Home load and PV are representative hourly values.
# They are used for Raspberry Pi relay demonstration.

hours = list(range(24))

home_load_kw = [
    0.55, 0.48, 0.42, 0.40, 0.44, 0.65,
    1.05, 1.45, 1.30, 1.10, 1.25, 4.20,
    4.60, 2.00, 1.25, 1.35, 1.85, 2.55,
    3.10, 3.55, 3.25, 2.55, 1.55, 0.95
]

pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.05,
    0.30, 0.85, 1.75, 2.65, 3.35, 3.75,
    4.00, 3.85, 3.50, 2.70, 1.45, 0.35,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00
]

# Representative time-of-use tariff in cents/kWh
price_cents_per_kwh = [
    20, 20, 20, 20, 20, 20,
    28, 28, 28, 28, 28, 28,
    28, 28, 28, 28, 42, 42,
    42, 42, 42, 42, 20, 20
]

# Representative grid carbon intensity in kg CO2-e/kWh
carbon_kg_per_kwh = [
    0.75, 0.74, 0.73, 0.72, 0.72, 0.70,
    0.68, 0.62, 0.55, 0.45, 0.35, 0.30,
    0.28, 0.30, 0.36, 0.45, 0.58, 0.70,
    0.78, 0.82, 0.80, 0.76, 0.73, 0.72
]


# -----------------------------
# USER INPUT HELPERS
# -----------------------------

def get_float_input(prompt, minimum, maximum):
    while True:
        try:
            value = float(input(prompt))

            if value < minimum or value > maximum:
                print(f"Please enter a value between {minimum} and {maximum}.")
            else:
                return value

        except ValueError:
            print("Invalid input. Please enter a number.")


def get_yes_no_input(prompt):
    while True:
        answer = input(prompt).strip().lower()

        if answer in ["y", "yes"]:
            return 1

        if answer in ["n", "no"]:
            return 0

        print("Please enter y or n.")


def build_ev_availability():
    print("\nDefine EV availability for the day.")
    print("Night     = 00:00 to 05:00")
    print("Morning   = 06:00 to 11:00")
    print("Afternoon = 12:00 to 16:00")
    print("Evening   = 17:00 to 23:00\n")

    night_available = get_yes_no_input("Is EV available at night? (y/n): ")
    morning_available = get_yes_no_input("Is EV available in morning? (y/n): ")
    afternoon_available = get_yes_no_input("Is EV available in afternoon? (y/n): ")
    evening_available = get_yes_no_input("Is EV available in evening? (y/n): ")

    availability = []

    for hour in hours:
        if 0 <= hour <= 5:
            availability.append(night_available)
        elif 6 <= hour <= 11:
            availability.append(morning_available)
        elif 12 <= hour <= 16:
            availability.append(afternoon_available)
        else:
            availability.append(evening_available)

    return availability


def get_user_settings():
    print("===================================================")
    print(" CUSTOM USER V2H INPUT SETUP")
    print("===================================================")

    current_soc = get_float_input(
        "Enter current EV SOC percentage, example 70: ",
        20.0,
        95.0
    )

    reserve_soc = get_float_input(
        "Enter minimum reserve SOC percentage, example 35: ",
        20.0,
        90.0
    )

    morning_reserve_soc = get_float_input(
        "Enter required morning reserve SOC percentage, example 55: ",
        reserve_soc,
        95.0
    )

    availability = build_ev_availability()

    return current_soc, reserve_soc, morning_reserve_soc, availability


# -----------------------------
# MEMBERSHIP FUNCTIONS
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


# -----------------------------
# TIME WINDOWS
# -----------------------------

def is_noon_v2h_window(hour):
    return 11 <= hour <= 12


def is_pv_charge_window(hour):
    return 13 <= hour <= 16


def is_evening_peak_window(hour):
    return 17 <= hour <= 21


def is_night_offpeak_window(hour):
    return hour >= 22 or hour <= 5


def required_reserve_soc(hour, reserve_soc, morning_reserve_soc):
    # Higher reserve at night and early morning
    if hour >= 22 or hour <= 8:
        return morning_reserve_soc

    return reserve_soc


# -----------------------------
# FUZZY CONTROLLER
# -----------------------------

def fuzzy_v2h_controller(
    hour,
    load,
    pv,
    soc,
    available,
    price,
    carbon,
    reserve_soc,
    morning_reserve_soc
):
    net_load = load - pv
    solar_ratio = pv / PV_REFERENCE_KW

    required_reserve = required_reserve_soc(
        hour,
        reserve_soc,
        morning_reserve_soc
    )

    soc_margin = soc - required_reserve

    # Hard safety and user constraints
    if available == 0:
        return "EV_UNAVAILABLE", 0.0, False, 0.0, net_load, solar_ratio, soc_margin, required_reserve

    if soc <= SOC_MIN_HARD:
        return "HARD_SOC_MIN_PROTECTION", 0.0, False, 0.0, net_load, solar_ratio, soc_margin, required_reserve

    if soc_margin <= 0:
        return "USER_RESERVE_PROTECTION", 0.0, False, 0.0, net_load, solar_ratio, soc_margin, required_reserve

    # --------------------------------------------------------
    # Simulated PV charging during afternoon solar surplus
    # Relay OFF because lamp only represents V2H discharge.
    # --------------------------------------------------------

    if is_pv_charge_window(hour) and net_load < -0.10 and soc < SOC_MAX:
        surplus_power = abs(net_load)
        available_soc_room_kwh = ((SOC_MAX - soc) / 100.0) * EV_BATTERY_KWH
        charge_power = min(EV_MAX_CHARGE_KW, surplus_power, available_soc_room_kwh)

        ev_power = -charge_power

        return (
            "PV_SURPLUS_CHARGING_SIMULATED",
            ev_power,
            False,
            -80.0,
            net_load,
            solar_ratio,
            soc_margin,
            required_reserve
        )

    # --------------------------------------------------------
    # Simulated night off-peak charging to morning reserve
    # Relay OFF because this is charging, not V2H discharge.
    # --------------------------------------------------------

    if is_night_offpeak_window(hour) and soc < morning_reserve_soc:
        required_energy_kwh = ((morning_reserve_soc - soc) / 100.0) * EV_BATTERY_KWH
        charge_power = min(EV_MAX_CHARGE_KW, required_energy_kwh)

        ev_power = -charge_power

        return (
            "NIGHT_OFFPEAK_CHARGING_TO_MORNING_RESERVE",
            ev_power,
            False,
            -60.0,
            net_load,
            solar_ratio,
            soc_margin,
            required_reserve
        )

    # -----------------------------
    # Fuzzy input memberships
    # -----------------------------

    balanced_load = triangle(net_load, -0.4, 0.0, 0.4)
    low_deficit = triangle(net_load, 0.2, 1.2, 2.5)
    medium_deficit = triangle(net_load, 1.5, 2.8, 4.0)
    high_deficit = trapezoid(net_load, 3.2, 4.0, 6.0, 6.0)

    margin_low = triangle(soc_margin, 0.0, 10.0, 25.0)
    margin_medium = triangle(soc_margin, 15.0, 30.0, 45.0)
    margin_high = trapezoid(soc_margin, 35.0, 50.0, 80.0, 80.0)

    price_low = trapezoid(price, 0.0, 0.0, 22.0, 27.0)
    price_medium = triangle(price, 24.0, 30.0, 36.0)
    price_high = trapezoid(price, 34.0, 40.0, 60.0, 60.0)

    solar_low = trapezoid(solar_ratio, 0.0, 0.0, 0.15, 0.35)
    solar_high = trapezoid(solar_ratio, 0.65, 0.85, 1.20, 1.20)

    carbon_medium = triangle(carbon, 0.35, 0.55, 0.75)
    carbon_high = trapezoid(carbon, 0.65, 0.75, 1.20, 1.20)

    noon_window = 1.0 if is_noon_v2h_window(hour) else 0.0
    evening_window = 1.0 if is_evening_peak_window(hour) else 0.0

    # -----------------------------
    # Fuzzy rules
    # Score:
    # 0   = hold
    # 60  = weak V2H
    # 80  = medium V2H
    # 100 = strong V2H
    # -----------------------------

    rules = []

    # Hold rules
    rules.append((balanced_load, 0.0))
    rules.append((margin_low, 0.0))
    rules.append((solar_high, 0.0))
    rules.append((price_low, 0.0))

    # Noon V2H support
    rules.append((
        min(noon_window, medium_deficit, max(margin_medium, margin_high), price_medium),
        65.0
    ))

    rules.append((
        min(noon_window, high_deficit, max(margin_medium, margin_high), max(carbon_medium, carbon_high)),
        75.0
    ))

    # Evening V2H support
    rules.append((
        min(evening_window, high_deficit, margin_high, price_high, carbon_high, solar_low),
        100.0
    ))

    rules.append((
        min(evening_window, high_deficit, margin_medium, price_high, carbon_high, solar_low),
        90.0
    ))

    rules.append((
        min(evening_window, medium_deficit, max(margin_medium, margin_high), price_high, max(carbon_medium, carbon_high), solar_low),
        80.0
    ))

    rules.append((
        min(evening_window, low_deficit, margin_high, price_high, solar_low),
        60.0
    ))

    numerator = 0.0
    denominator = 0.0

    for strength, score in rules:
        numerator += strength * score
        denominator += strength

    if denominator == 0.0:
        fuzzy_score = 0.0
    else:
        fuzzy_score = numerator / denominator

    # Final V2H decision
    if fuzzy_score >= 55.0 and net_load > 0 and soc_margin > 0:
        relay_on = True

        if is_noon_v2h_window(hour):
            ev_power = min(1.5, net_load)
            decision = "NOON_USER_APPROVED_V2H"

        elif is_evening_peak_window(hour):
            ev_power = min(EV_MAX_DISCHARGE_KW, net_load)

            if fuzzy_score >= 85.0:
                decision = "EVENING_STRONG_USER_APPROVED_V2H"
            else:
                decision = "EVENING_MEDIUM_USER_APPROVED_V2H"

        else:
            ev_power = min(1.0, net_load)
            decision = "GENERAL_USER_APPROVED_V2H"

    else:
        relay_on = False
        ev_power = 0.0
        decision = "HOLD"

    return decision, ev_power, relay_on, fuzzy_score, net_load, solar_ratio, soc_margin, required_reserve


# -----------------------------
# SOC UPDATE
# -----------------------------

def update_soc(soc, ev_power):
    # ev_power > 0 means discharge
    # ev_power < 0 means charge
    soc_change = (ev_power / EV_BATTERY_KWH) * 100.0
    new_soc = soc - soc_change

    if new_soc < SOC_MIN_HARD:
        new_soc = SOC_MIN_HARD

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    return new_soc


# -----------------------------
# MAIN PROGRAM
# -----------------------------

def main():
    ev_soc, reserve_soc, morning_reserve_soc, ev_available = get_user_settings()

    os.makedirs("logs", exist_ok=True)
    log_file = "logs/custom_user_v2h_fuzzy_log.csv"

    results = []

    relay_previous_state = False
    relay_on_events = 0

    baseline_import_energy = 0.0
    managed_import_energy = 0.0
    ev_discharge_energy = 0.0
    ev_charge_energy = 0.0
    pv_charge_energy = 0.0
    night_charge_energy = 0.0

    baseline_cost = 0.0
    managed_cost = 0.0

    baseline_emissions = 0.0
    managed_emissions = 0.0

    baseline_peak = 0.0
    managed_peak = 0.0
    minimum_soc = ev_soc

    print("\n===================================================")
    print(" CUSTOM USER V2H FUZZY DEMO STARTED")
    print(" Relay ON  = V2H discharge active")
    print(" Relay OFF = hold / charging / EV unavailable")
    print(" 24 simulated hours = 5 real minutes")
    print("===================================================")

    try:
        for i, hour in enumerate(hours):
            load = home_load_kw[i]
            pv = pv_generation_kw[i]
            available = ev_available[i]
            price = price_cents_per_kwh[i]
            carbon = carbon_kg_per_kwh[i]

            (
                decision,
                ev_power,
                relay_on,
                fuzzy_score,
                net_load,
                solar_ratio,
                soc_margin,
                required_reserve
            ) = fuzzy_v2h_controller(
                hour,
                load,
                pv,
                ev_soc,
                available,
                price,
                carbon,
                reserve_soc,
                morning_reserve_soc
            )

            managed_grid = net_load - ev_power

            if relay_on:
                relay.on()
            else:
                relay.off()

            if relay_on and not relay_previous_state:
                relay_on_events += 1

            relay_previous_state = relay_on

            baseline_import = max(net_load, 0.0)
            managed_import = max(managed_grid, 0.0)

            baseline_import_energy += baseline_import
            managed_import_energy += managed_import

            baseline_cost += baseline_import * (price / 100.0)
            managed_cost += managed_import * (price / 100.0)

            baseline_emissions += baseline_import * carbon
            managed_emissions += managed_import * carbon

            baseline_peak = max(baseline_peak, baseline_import)
            managed_peak = max(managed_peak, managed_import)

            if ev_power > 0:
                ev_discharge_energy += ev_power

            if ev_power < 0:
                charge_energy = abs(ev_power)
                ev_charge_energy += charge_energy

                if "PV_SURPLUS" in decision:
                    pv_charge_energy += charge_energy

                if "NIGHT_OFFPEAK" in decision:
                    night_charge_energy += charge_energy

            print(
                f"{hour:02d}:00 | "
                f"Load={load:4.2f} kW | "
                f"PV={pv:4.2f} kW | "
                f"Net={net_load:5.2f} kW | "
                f"SOC={ev_soc:5.1f}% | "
                f"ReqRes={required_reserve:5.1f}% | "
                f"Margin={soc_margin:5.1f}% | "
                f"Avail={available} | "
                f"Score={fuzzy_score:6.1f} | "
                f"EV={ev_power:5.2f} kW | "
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
                "price_cents_per_kwh": price,
                "carbon_kg_per_kwh": carbon,
                "ev_available": available,
                "required_reserve_soc": round(required_reserve, 2),
                "ev_soc_percent": round(ev_soc, 2),
                "soc_margin_percent": round(soc_margin, 2),
                "solar_ratio": round(solar_ratio, 3),
                "fuzzy_score": round(fuzzy_score, 2),
                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "managed_grid_kw": round(managed_grid, 3),
                "relay_state": "ON" if relay_on else "OFF"
            })

            ev_soc = update_soc(ev_soc, ev_power)
            minimum_soc = min(minimum_soc, ev_soc)

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        relay.off()
        print("Relay OFF safely.")

        fieldnames = [
            "timestamp",
            "hour",
            "home_load_kw",
            "pv_generation_kw",
            "net_load_kw",
            "price_cents_per_kwh",
            "carbon_kg_per_kwh",
            "ev_available",
            "required_reserve_soc",
            "ev_soc_percent",
            "soc_margin_percent",
            "solar_ratio",
            "fuzzy_score",
            "decision",
            "ev_power_kw",
            "managed_grid_kw",
            "relay_state"
        ]

        with open(log_file, "w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

        peak_reduction_kw = baseline_peak - managed_peak

        if baseline_peak > 0:
            peak_reduction_percent = (peak_reduction_kw / baseline_peak) * 100.0
        else:
            peak_reduction_percent = 0.0

        cost_saving = baseline_cost - managed_cost
        emissions_avoided = baseline_emissions - managed_emissions

        print("\n================ SUMMARY METRICS ================")
        print(f"Baseline import energy:       {baseline_import_energy:6.2f} kWh")
        print(f"Managed import energy:        {managed_import_energy:6.2f} kWh")
        print(f"EV discharge energy:          {ev_discharge_energy:6.2f} kWh")
        print(f"EV charge energy total:       {ev_charge_energy:6.2f} kWh")
        print(f"PV charging energy:           {pv_charge_energy:6.2f} kWh")
        print(f"Night off-peak charge energy: {night_charge_energy:6.2f} kWh")
        print(f"Baseline peak demand:         {baseline_peak:6.2f} kW")
        print(f"Managed peak demand:          {managed_peak:6.2f} kW")
        print(f"Peak reduction:               {peak_reduction_kw:6.2f} kW ({peak_reduction_percent:5.1f}%)")
        print(f"Minimum SOC:                  {minimum_soc:6.1f}%")
        print(f"Relay ON events:              {relay_on_events}")
        print(f"Estimated baseline cost:      ${baseline_cost:6.2f}")
        print(f"Estimated managed cost:       ${managed_cost:6.2f}")
        print(f"Estimated cost saving:        ${cost_saving:6.2f}")
        print(f"Baseline emissions:           {baseline_emissions:6.2f} kg CO2-e")
        print(f"Managed emissions:            {managed_emissions:6.2f} kg CO2-e")
        print(f"Estimated emissions avoided:  {emissions_avoided:6.2f} kg CO2-e")
        print("=================================================")
        print(f"Log saved to: {log_file}")
        print("Demo complete.")


if __name__ == "__main__":
    main()