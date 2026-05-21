from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os


# ============================================================
# HARSHIL'S SCENARIO
# Dynamic price + export limit + CO2 + battery wear aware V2H
#
# Relay IN2 -> Raspberry Pi GPIO27 / physical pin 13
#
# Relay/Lamp:
# ON  = V2H discharge active
# OFF = hold / PV charging / grid charging / protection mode
#
# Main idea:
# The controller does not discharge only because price is high.
# It checks financial benefit, CO2 intensity, PV export limits,
# SOC reserve, forecast risk, EV availability, and battery wear.
#
# 24 simulated hours = 5 real minutes
# 1 simulated hour = 12.5 seconds
# ============================================================


# -----------------------------
# Relay setup
# -----------------------------
# Your relay was working correctly with active_high=True.
# If the lamp works opposite again, change True to False.

relay = OutputDevice(27, active_high=True, initial_value=False)


# -----------------------------
# Folder and file paths
# -----------------------------

DATA_DIR = "data"
LOG_DIR = "logs"

ENERGY_PROFILE_FILE = os.path.join(DATA_DIR, "harshil_energy_profile.csv")
MARKET_NETWORK_FILE = os.path.join(DATA_DIR, "harshil_market_network.csv")
BATTERY_PROFILE_FILE = os.path.join(DATA_DIR, "harshil_battery_profile.csv")

HOURLY_LOG_FILE = os.path.join(LOG_DIR, "harshil_hourly_log.csv")
SUMMARY_MATRIX_FILE = os.path.join(LOG_DIR, "harshil_summary_matrix.csv")
RULE_TRACE_FILE = os.path.join(LOG_DIR, "harshil_rule_trace.csv")
EVENT_LOG_FILE = os.path.join(LOG_DIR, "harshil_event_log.csv")


# -----------------------------
# System settings
# -----------------------------

EV_BATTERY_KWH = 60.0
EV_MAX_DISCHARGE_KW = 3.3
EV_MAX_CHARGE_KW = 3.3

SOC_MIN = 20.0
SOC_MAX = 95.0

PV_REFERENCE_KW = 4.0
INITIAL_SOC = 72.0

# 24 hours compressed into 5 minutes
HOUR_DELAY_SECONDS = 12.5

# Relay turns ON only when V2H score crosses this value
V2H_SCORE_THRESHOLD = 55.0

# Used to reduce excessive battery cycling
DAILY_DISCHARGE_BUDGET_KWH = 10.0


# ============================================================
# Create input CSV files if they are missing
# ============================================================

def create_input_csvs_if_missing():
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(ENERGY_PROFILE_FILE):
        energy_rows = [
            # hour, home_load_kw, pv_actual_kw, pv_forecast_kw, ev_available, trip_reserve_soc, critical_load_level
            [0, 0.55, 0.00, 0.00, 1, 55, 0.20],
            [1, 0.48, 0.00, 0.00, 1, 55, 0.20],
            [2, 0.42, 0.00, 0.00, 1, 55, 0.20],
            [3, 0.40, 0.00, 0.00, 1, 55, 0.20],
            [4, 0.44, 0.00, 0.00, 1, 55, 0.20],
            [5, 0.65, 0.05, 0.10, 1, 55, 0.30],
            [6, 1.05, 0.30, 0.40, 1, 55, 0.40],
            [7, 1.45, 0.85, 1.00, 0, 60, 0.50],
            [8, 1.30, 1.75, 1.90, 0, 60, 0.40],
            [9, 1.10, 2.65, 2.80, 0, 50, 0.30],
            [10, 1.25, 3.35, 3.60, 1, 45, 0.30],
            [11, 1.60, 3.75, 4.00, 1, 45, 0.40],
            [12, 1.80, 4.00, 3.60, 1, 45, 0.50],
            [13, 1.55, 3.85, 3.30, 1, 45, 0.40],
            [14, 1.35, 3.50, 2.90, 1, 45, 0.40],
            [15, 1.55, 2.70, 2.20, 1, 45, 0.50],
            [16, 2.10, 1.45, 1.10, 1, 45, 0.60],
            [17, 2.85, 0.35, 0.25, 1, 45, 0.75],
            [18, 3.35, 0.00, 0.00, 1, 45, 0.90],
            [19, 3.70, 0.00, 0.00, 1, 45, 1.00],
            [20, 3.30, 0.00, 0.00, 1, 45, 0.90],
            [21, 2.60, 0.00, 0.00, 1, 50, 0.70],
            [22, 1.55, 0.00, 0.00, 1, 55, 0.40],
            [23, 0.95, 0.00, 0.00, 1, 55, 0.30],
        ]

        with open(ENERGY_PROFILE_FILE, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                "hour",
                "home_load_kw",
                "pv_actual_kw",
                "pv_forecast_kw",
                "ev_available",
                "trip_reserve_soc",
                "critical_load_level",
            ])
            writer.writerows(energy_rows)

    if not os.path.exists(MARKET_NETWORK_FILE):
        market_rows = [
            # hour, import_price_c_per_kwh, feed_in_price_c_per_kwh, export_limit_kw, network_stress_level, grid_co2_kg_per_kwh
            [0, 18, 1.0, 5.0, 0.20, 0.74],
            [1, 16, 1.0, 5.0, 0.20, 0.72],
            [2, 15, 1.0, 5.0, 0.20, 0.71],
            [3, 14, 1.0, 5.0, 0.20, 0.70],
            [4, 15, 1.0, 5.0, 0.25, 0.70],
            [5, 18, 1.0, 5.0, 0.30, 0.69],
            [6, 24, 1.0, 5.0, 0.40, 0.65],
            [7, 32, 1.0, 5.0, 0.50, 0.60],
            [8, 28, 1.0, 4.0, 0.45, 0.52],
            [9, 22, 0.5, 2.5, 0.40, 0.42],
            [10, 18, 0.0, 1.5, 0.55, 0.34],
            [11, 12, 0.0, 1.0, 0.70, 0.28],
            [12, 8, 0.0, 0.8, 0.85, 0.25],
            [13, 10, 0.0, 0.8, 0.85, 0.28],
            [14, 15, 0.5, 1.0, 0.75, 0.35],
            [15, 22, 1.0, 1.5, 0.65, 0.45],
            [16, 45, 1.0, 2.0, 0.80, 0.62],
            [17, 70, 1.0, 2.0, 0.90, 0.78],
            [18, 85, 1.0, 1.5, 0.95, 0.86],
            [19, 75, 1.0, 1.5, 0.95, 0.88],
            [20, 55, 1.0, 2.0, 0.85, 0.82],
            [21, 40, 1.0, 3.0, 0.65, 0.75],
            [22, 22, 1.0, 5.0, 0.35, 0.72],
            [23, 18, 1.0, 5.0, 0.25, 0.70],
        ]

        with open(MARKET_NETWORK_FILE, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                "hour",
                "import_price_c_per_kwh",
                "feed_in_price_c_per_kwh",
                "export_limit_kw",
                "network_stress_level",
                "grid_co2_kg_per_kwh",
            ])
            writer.writerows(market_rows)

    if not os.path.exists(BATTERY_PROFILE_FILE):
        battery_rows = [
            # hour, battery_temp_c, cycle_budget_remaining, battery_wear_cost_c_per_kwh
            [0, 22, 1.00, 7],
            [1, 22, 1.00, 7],
            [2, 21, 1.00, 7],
            [3, 21, 1.00, 7],
            [4, 21, 1.00, 7],
            [5, 22, 1.00, 7],
            [6, 23, 0.98, 7],
            [7, 24, 0.96, 7],
            [8, 25, 0.95, 7],
            [9, 26, 0.95, 8],
            [10, 28, 0.94, 8],
            [11, 30, 0.93, 8],
            [12, 32, 0.92, 9],
            [13, 33, 0.90, 9],
            [14, 33, 0.88, 9],
            [15, 32, 0.86, 9],
            [16, 31, 0.84, 9],
            [17, 30, 0.82, 9],
            [18, 30, 0.80, 10],
            [19, 31, 0.76, 10],
            [20, 30, 0.72, 10],
            [21, 28, 0.68, 9],
            [22, 26, 0.66, 8],
            [23, 24, 0.66, 8],
        ]

        with open(BATTERY_PROFILE_FILE, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                "hour",
                "battery_temp_c",
                "cycle_budget_remaining",
                "battery_wear_cost_c_per_kwh",
            ])
            writer.writerows(battery_rows)


# ============================================================
# CSV loading
# ============================================================

def read_csv_by_hour(file_path):
    data = {}

    with open(file_path, "r", newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            hour = int(row["hour"])
            data[hour] = row

    return data


def load_input_data():
    energy_data = read_csv_by_hour(ENERGY_PROFILE_FILE)
    market_data = read_csv_by_hour(MARKET_NETWORK_FILE)
    battery_data = read_csv_by_hour(BATTERY_PROFILE_FILE)

    combined_rows = []

    for hour in range(24):
        energy = energy_data[hour]
        market = market_data[hour]
        battery = battery_data[hour]

        combined_rows.append({
            "hour": hour,
            "home_load_kw": float(energy["home_load_kw"]),
            "pv_actual_kw": float(energy["pv_actual_kw"]),
            "pv_forecast_kw": float(energy["pv_forecast_kw"]),
            "ev_available": int(energy["ev_available"]),
            "trip_reserve_soc": float(energy["trip_reserve_soc"]),
            "critical_load_level": float(energy["critical_load_level"]),

            "import_price_c_per_kwh": float(market["import_price_c_per_kwh"]),
            "feed_in_price_c_per_kwh": float(market["feed_in_price_c_per_kwh"]),
            "export_limit_kw": float(market["export_limit_kw"]),
            "network_stress_level": float(market["network_stress_level"]),
            "grid_co2_kg_per_kwh": float(market["grid_co2_kg_per_kwh"]),

            "battery_temp_c": float(battery["battery_temp_c"]),
            "cycle_budget_remaining": float(battery["cycle_budget_remaining"]),
            "battery_wear_cost_c_per_kwh": float(battery["battery_wear_cost_c_per_kwh"]),
        })

    return combined_rows


# ============================================================
# Fuzzy helper functions
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


def best_label(membership_dict):
    selected_label = "unknown"
    selected_value = -1.0

    for label, value in membership_dict.items():
        if value > selected_value:
            selected_label = label
            selected_value = value

    return selected_label, selected_value


# ============================================================
# Time windows
# ============================================================

def is_night_offpeak(hour):
    return hour <= 5 or hour >= 22


def is_midday_solar_window(hour):
    return 10 <= hour <= 15


# ============================================================
# Feature calculations
# ============================================================

def calculate_features(row, soc, cycle_budget_remaining):
    load = row["home_load_kw"]
    pv = row["pv_actual_kw"]
    pv_forecast = row["pv_forecast_kw"]

    net_load = load - pv

    pv_surplus = max(pv - load, 0.0)
    export_pressure = max(pv_surplus - row["export_limit_kw"], 0.0)

    forecast_error_kw = abs(pv_forecast - pv)
    forecast_risk = min(forecast_error_kw / PV_REFERENCE_KW, 1.0)

    soc_margin = soc - row["trip_reserve_soc"]

    financial_benefit = (
        row["import_price_c_per_kwh"]
        - row["feed_in_price_c_per_kwh"]
        - row["battery_wear_cost_c_per_kwh"]
    )

    # CO2 gives a bonus to V2H when grid emissions are high.
    # Example: 0.80 kg/kWh gives about 8 c/kWh extra value.
    carbon_bonus = row["grid_co2_kg_per_kwh"] * 10.0
    combined_benefit = financial_benefit + carbon_bonus

    temperature_stress = max(row["battery_temp_c"] - 30.0, 0.0) / 15.0
    budget_stress = 1.0 - cycle_budget_remaining
    wear_cost_stress = row["battery_wear_cost_c_per_kwh"] / 15.0

    wear_stress = (
        0.45 * temperature_stress
        + 0.35 * budget_stress
        + 0.20 * wear_cost_stress
    )

    if wear_stress > 1.0:
        wear_stress = 1.0

    return {
        "net_load": net_load,
        "pv_surplus": pv_surplus,
        "export_pressure": export_pressure,
        "forecast_error_kw": forecast_error_kw,
        "forecast_risk": forecast_risk,
        "soc_margin": soc_margin,
        "financial_benefit": financial_benefit,
        "carbon_bonus": carbon_bonus,
        "combined_benefit": combined_benefit,
        "wear_stress": wear_stress,
    }


# ============================================================
# Fuzzy V2H controller
# ============================================================

def fuzzy_v2h_controller(row, soc, cycle_budget_remaining):
    hour = row["hour"]
    features = calculate_features(row, soc, cycle_budget_remaining)

    net_load = features["net_load"]
    soc_margin = features["soc_margin"]
    combined_benefit = features["combined_benefit"]
    export_pressure = features["export_pressure"]
    forecast_risk = features["forecast_risk"]
    wear_stress = features["wear_stress"]
    co2 = row["grid_co2_kg_per_kwh"]

    # -----------------------------
    # Hard protection rules
    # -----------------------------

    if row["ev_available"] == 0:
        return {
            "decision": "EV_NOT_AVAILABLE",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_score": 0.0,
            "dominant_rule": "Hard rule: EV unavailable",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    if soc <= SOC_MIN:
        return {
            "decision": "HARD_SOC_MIN_PROTECTION",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_score": 0.0,
            "dominant_rule": "Hard rule: SOC minimum protection",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    if soc_margin <= 0:
        return {
            "decision": "TRIP_RESERVE_PROTECTION",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_score": 0.0,
            "dominant_rule": "Hard rule: user trip reserve protection",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    if cycle_budget_remaining <= 0:
        return {
            "decision": "CYCLE_BUDGET_PROTECTION",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_score": 0.0,
            "dominant_rule": "Hard rule: daily cycle budget exhausted",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    # -----------------------------
    # Priority 1: PV charging
    # -----------------------------
    # Relay remains OFF because the lamp only shows V2H discharge.

    if (
        is_midday_solar_window(hour)
        and features["pv_surplus"] > 0.20
        and soc < SOC_MAX
        and row["feed_in_price_c_per_kwh"] <= 1.0
    ):
        available_battery_room_kwh = ((SOC_MAX - soc) / 100.0) * EV_BATTERY_KWH

        charge_power = min(
            EV_MAX_CHARGE_KW,
            features["pv_surplus"],
            available_battery_room_kwh,
        )

        return {
            "decision": "PV_EXPORT_LIMIT_CHARGING",
            "ev_power_kw": -charge_power,
            "relay_on": False,
            "fuzzy_score": -85.0,
            "dominant_rule": "PV surplus + low feed-in value + export limit pressure",
            "dominant_strength": 1.0,
            "dominant_score": -85.0,
            "features": features,
            "levels": {},
        }

    # -----------------------------
    # Priority 2: night reserve charging
    # -----------------------------
    # Relay remains OFF because this is charging, not discharge.

    if is_night_offpeak(hour) and soc < row["trip_reserve_soc"]:
        required_energy_kwh = ((row["trip_reserve_soc"] - soc) / 100.0) * EV_BATTERY_KWH

        charge_power = min(
            EV_MAX_CHARGE_KW,
            required_energy_kwh,
        )

        return {
            "decision": "NIGHT_RESERVE_CHARGING",
            "ev_power_kw": -charge_power,
            "relay_on": False,
            "fuzzy_score": -60.0,
            "dominant_rule": "Night off-peak charging to meet user reserve",
            "dominant_strength": 1.0,
            "dominant_score": -60.0,
            "features": features,
            "levels": {},
        }

    # -----------------------------
    # Fuzzification
    # -----------------------------

    net_load_mf = {
        "surplus": trapezoid(net_load, -5.0, -5.0, -0.8, -0.1),
        "balanced": triangle(net_load, -0.5, 0.0, 0.5),
        "low_deficit": triangle(net_load, 0.2, 1.2, 2.5),
        "medium_deficit": triangle(net_load, 1.5, 2.8, 4.0),
        "high_deficit": trapezoid(net_load, 3.2, 4.0, 6.0, 6.0),
    }

    benefit_mf = {
        "negative": trapezoid(combined_benefit, -50.0, -50.0, 0.0, 8.0),
        "low": triangle(combined_benefit, 5.0, 15.0, 25.0),
        "medium": triangle(combined_benefit, 20.0, 35.0, 50.0),
        "high": triangle(combined_benefit, 45.0, 60.0, 75.0),
        "very_high": trapezoid(combined_benefit, 70.0, 85.0, 120.0, 120.0),
    }

    soc_margin_mf = {
        "low": triangle(soc_margin, 0.0, 10.0, 25.0),
        "medium": triangle(soc_margin, 15.0, 30.0, 45.0),
        "high": trapezoid(soc_margin, 35.0, 50.0, 80.0, 80.0),
    }

    export_pressure_mf = {
        "none": trapezoid(export_pressure, 0.0, 0.0, 0.05, 0.20),
        "low": triangle(export_pressure, 0.10, 0.40, 0.80),
        "high": trapezoid(export_pressure, 0.60, 1.00, 4.00, 4.00),
    }

    forecast_risk_mf = {
        "low": trapezoid(forecast_risk, 0.0, 0.0, 0.05, 0.15),
        "medium": triangle(forecast_risk, 0.10, 0.25, 0.45),
        "high": trapezoid(forecast_risk, 0.35, 0.55, 1.00, 1.00),
    }

    wear_stress_mf = {
        "low": trapezoid(wear_stress, 0.0, 0.0, 0.20, 0.40),
        "medium": triangle(wear_stress, 0.25, 0.50, 0.75),
        "high": trapezoid(wear_stress, 0.65, 0.80, 1.00, 1.00),
    }

    co2_mf = {
        "low": trapezoid(co2, 0.00, 0.00, 0.35, 0.45),
        "medium": triangle(co2, 0.35, 0.55, 0.75),
        "high": trapezoid(co2, 0.65, 0.75, 1.20, 1.20),
    }

    levels = {
        "net_load_level": best_label(net_load_mf)[0],
        "benefit_level": best_label(benefit_mf)[0],
        "soc_margin_level": best_label(soc_margin_mf)[0],
        "export_pressure_level": best_label(export_pressure_mf)[0],
        "forecast_risk_level": best_label(forecast_risk_mf)[0],
        "wear_stress_level": best_label(wear_stress_mf)[0],
        "co2_level": best_label(co2_mf)[0],
    }

    # -----------------------------
    # Fuzzy rule base
    # -----------------------------
    # Score:
    # 0   = hold
    # 60  = weak V2H
    # 80  = medium V2H
    # 100 = strong V2H

    rules = []

    rules.append((
        max(
            benefit_mf["negative"],
            wear_stress_mf["high"],
            forecast_risk_mf["high"] * soc_margin_mf["low"],
            net_load_mf["balanced"],
            net_load_mf["surplus"],
        ),
        0.0,
        "Hold: poor benefit / high wear / forecast risk / no deficit",
    ))

    rules.append((
        min(
            net_load_mf["high_deficit"],
            benefit_mf["very_high"],
            soc_margin_mf["high"],
            co2_mf["high"],
            wear_stress_mf["low"],
        ),
        100.0,
        "Strong V2H: high deficit + very high benefit + high CO2 + safe SOC",
    ))

    rules.append((
        min(
            net_load_mf["medium_deficit"],
            max(benefit_mf["high"], benefit_mf["very_high"]),
            max(soc_margin_mf["medium"], soc_margin_mf["high"]),
            max(co2_mf["medium"], co2_mf["high"]),
        ),
        80.0,
        "Medium V2H: medium deficit + high value + medium/high CO2",
    ))

    rules.append((
        min(
            net_load_mf["low_deficit"],
            benefit_mf["high"],
            soc_margin_mf["high"],
            co2_mf["high"],
        ),
        60.0,
        "Weak V2H: low deficit but high value and high CO2",
    ))

    rules.append((
        min(
            row["network_stress_level"],
            max(net_load_mf["medium_deficit"], net_load_mf["high_deficit"]),
            max(benefit_mf["high"], benefit_mf["very_high"]),
            max(soc_margin_mf["medium"], soc_margin_mf["high"]),
        ),
        85.0,
        "Network support V2H: stressed network + valuable import reduction",
    ))

    numerator = 0.0
    denominator = 0.0

    dominant_rule = "No active rule"
    dominant_strength = 0.0
    dominant_score = 0.0

    for strength, score, rule_name in rules:
        numerator += strength * score
        denominator += strength

        if strength > dominant_strength:
            dominant_strength = strength
            dominant_score = score
            dominant_rule = rule_name

    if denominator == 0.0:
        fuzzy_score = 0.0
    else:
        fuzzy_score = numerator / denominator

    # -----------------------------
    # Final V2H decision
    # -----------------------------

    if fuzzy_score >= V2H_SCORE_THRESHOLD and net_load > 0:
        if fuzzy_score >= 85.0:
            ev_power = min(EV_MAX_DISCHARGE_KW, net_load)
            decision = "STRONG_NET_BENEFIT_CO2_V2H"

        elif fuzzy_score >= 70.0:
            ev_power = min(2.2, net_load)
            decision = "MEDIUM_NET_BENEFIT_CO2_V2H"

        else:
            ev_power = min(1.2, net_load)
            decision = "WEAK_NET_BENEFIT_CO2_V2H"

        relay_on = True

    else:
        ev_power = 0.0
        relay_on = False
        decision = "HOLD"

    return {
        "decision": decision,
        "ev_power_kw": ev_power,
        "relay_on": relay_on,
        "fuzzy_score": fuzzy_score,
        "dominant_rule": dominant_rule,
        "dominant_strength": dominant_strength,
        "dominant_score": dominant_score,
        "features": features,
        "levels": levels,
    }


# ============================================================
# SOC update
# ============================================================

def update_soc(soc, ev_power_kw):
    # ev_power > 0 means discharging
    # ev_power < 0 means charging

    soc_change = (ev_power_kw / EV_BATTERY_KWH) * 100.0
    new_soc = soc - soc_change

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    return new_soc


# ============================================================
# Summary matrix
# ============================================================

def export_and_curtailment(grid_power_kw, export_limit_kw):
    export_candidate = max(-grid_power_kw, 0.0)
    exported = min(export_candidate, export_limit_kw)
    curtailed = max(export_candidate - export_limit_kw, 0.0)

    return exported, curtailed


def create_summary_matrix(results):
    total_home_load = sum(row["home_load_kw"] for row in results)
    total_pv = sum(row["pv_actual_kw"] for row in results)

    baseline_import = 0.0
    managed_import = 0.0

    baseline_export = 0.0
    managed_export = 0.0

    baseline_curtailment = 0.0
    managed_curtailment = 0.0

    baseline_bill = 0.0
    managed_bill_before_wear = 0.0

    baseline_emissions = 0.0
    managed_emissions = 0.0

    battery_wear_cost = 0.0

    ev_discharge_energy = 0.0
    ev_charge_energy = 0.0
    pv_charge_energy = 0.0
    grid_charge_energy = 0.0

    baseline_peak_import = 0.0
    managed_peak_import = 0.0

    reserve_violations = 0
    forecast_risk_hold_events = 0

    for row in results:
        baseline_grid = row["baseline_grid_kw"]
        managed_grid = row["managed_grid_kw"]
        export_limit = row["export_limit_kw"]

        baseline_imp = max(baseline_grid, 0.0)
        managed_imp = max(managed_grid, 0.0)

        baseline_exp, baseline_curt = export_and_curtailment(
            baseline_grid,
            export_limit,
        )

        managed_exp, managed_curt = export_and_curtailment(
            managed_grid,
            export_limit,
        )

        baseline_import += baseline_imp
        managed_import += managed_imp

        baseline_export += baseline_exp
        managed_export += managed_exp

        baseline_curtailment += baseline_curt
        managed_curtailment += managed_curt

        baseline_bill += (
            baseline_imp * (row["import_price_c_per_kwh"] / 100.0)
            - baseline_exp * (row["feed_in_price_c_per_kwh"] / 100.0)
        )

        managed_bill_before_wear += (
            managed_imp * (row["import_price_c_per_kwh"] / 100.0)
            - managed_exp * (row["feed_in_price_c_per_kwh"] / 100.0)
        )

        baseline_emissions += baseline_imp * row["grid_co2_kg_per_kwh"]
        managed_emissions += managed_imp * row["grid_co2_kg_per_kwh"]

        if row["ev_power_kw"] > 0:
            ev_discharge_energy += row["ev_power_kw"]
            battery_wear_cost += row["ev_power_kw"] * (
                row["battery_wear_cost_c_per_kwh"] / 100.0
            )

        if row["ev_power_kw"] < 0:
            charge_energy = abs(row["ev_power_kw"])
            ev_charge_energy += charge_energy

            if "PV" in row["decision"]:
                pv_charge_energy += charge_energy
            else:
                grid_charge_energy += charge_energy

        baseline_peak_import = max(baseline_peak_import, baseline_imp)
        managed_peak_import = max(managed_peak_import, managed_imp)

        if row["soc_after_percent"] < row["trip_reserve_soc"]:
            reserve_violations += 1

        if row["decision"] == "HOLD" and row.get("forecast_risk_level", "unknown") == "high":
            forecast_risk_hold_events += 1

    managed_bill_after_wear = managed_bill_before_wear + battery_wear_cost

    gross_bill_saving = baseline_bill - managed_bill_before_wear
    net_bill_saving = baseline_bill - managed_bill_after_wear

    emissions_avoided = baseline_emissions - managed_emissions

    peak_reduction = baseline_peak_import - managed_peak_import

    if baseline_peak_import > 0:
        peak_reduction_percent = (peak_reduction / baseline_peak_import) * 100.0
    else:
        peak_reduction_percent = 0.0

    curtailment_reduction = baseline_curtailment - managed_curtailment

    relay_on_hours = sum(
        1 for row in results
        if row["relay_state"] == "ON"
    )

    relay_on_periods = [
        f"{row['hour']:02d}:00"
        for row in results
        if row["relay_state"] == "ON"
    ]

    minimum_soc = min(row["soc_after_percent"] for row in results)
    final_soc = results[-1]["soc_after_percent"]

    cycle_budget_used = ev_discharge_energy / DAILY_DISCHARGE_BUDGET_KWH

    summary = {
        "Scenario": "Harshil's scenario - dynamic price, export limit, CO2 and battery wear aware V2H",

        "Total home load energy (kWh)": round(total_home_load, 2),
        "Total PV generation (kWh)": round(total_pv, 2),

        "Baseline grid import (kWh)": round(baseline_import, 2),
        "Managed grid import (kWh)": round(managed_import, 2),
        "Grid import reduction (kWh)": round(baseline_import - managed_import, 2),

        "Baseline grid export (kWh)": round(baseline_export, 2),
        "Managed grid export (kWh)": round(managed_export, 2),

        "Baseline PV curtailment (kWh)": round(baseline_curtailment, 2),
        "Managed PV curtailment (kWh)": round(managed_curtailment, 2),
        "PV curtailment reduction (kWh)": round(curtailment_reduction, 2),

        "EV discharge energy (kWh)": round(ev_discharge_energy, 2),
        "EV charge energy total (kWh)": round(ev_charge_energy, 2),
        "PV charging energy (kWh)": round(pv_charge_energy, 2),
        "Grid charging energy (kWh)": round(grid_charge_energy, 2),

        "Baseline electricity bill ($)": round(baseline_bill, 2),
        "Managed bill before wear ($)": round(managed_bill_before_wear, 2),
        "Battery wear cost ($)": round(battery_wear_cost, 2),
        "Managed bill after wear ($)": round(managed_bill_after_wear, 2),
        "Gross bill saving before wear ($)": round(gross_bill_saving, 2),
        "Net bill saving after wear ($)": round(net_bill_saving, 2),

        "Baseline CO2 emissions (kg CO2-e)": round(baseline_emissions, 2),
        "Managed CO2 emissions (kg CO2-e)": round(managed_emissions, 2),
        "Net CO2 emissions avoided (kg CO2-e)": round(emissions_avoided, 2),

        "Baseline peak import (kW)": round(baseline_peak_import, 2),
        "Managed peak import (kW)": round(managed_peak_import, 2),
        "Peak import reduction (kW)": round(peak_reduction, 2),
        "Peak import reduction (%)": round(peak_reduction_percent, 1),

        "Initial SOC (%)": round(INITIAL_SOC, 2),
        "Final SOC (%)": round(final_soc, 2),
        "Minimum SOC (%)": round(minimum_soc, 2),
        "Reserve violation hours": reserve_violations,

        "Relay ON hours": relay_on_hours,
        "Relay ON periods": ", ".join(relay_on_periods),

        "Cycle budget used (%)": round(cycle_budget_used * 100.0, 1),
        "Forecast-risk hold events": forecast_risk_hold_events,
    }

    return summary


def print_summary_matrix(summary):
    print("\n================ HARSHIL'S SUMMARY MATRIX ================")

    for key, value in summary.items():
        print(f"{key:55s}: {value}")

    print("==========================================================")


def save_summary_matrix(summary):
    with open(SUMMARY_MATRIX_FILE, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["metric", "value"])

        for key, value in summary.items():
            writer.writerow([key, value])


# ============================================================
# Logging helpers
# ============================================================

def save_hourly_log(results):
    with open(HOURLY_LOG_FILE, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)


def save_rule_trace(rule_trace):
    if len(rule_trace) == 0:
        return

    with open(RULE_TRACE_FILE, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rule_trace[0].keys()))
        writer.writeheader()
        writer.writerows(rule_trace)


def save_event_log(event_log):
    fieldnames = [
        "timestamp",
        "hour",
        "event",
        "decision",
        "relay_state",
        "ev_power_kw",
        "soc_percent",
    ]

    with open(EVENT_LOG_FILE, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(event_log)


# ============================================================
# Main program
# ============================================================

def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    create_input_csvs_if_missing()
    input_rows = load_input_data()

    ev_soc = INITIAL_SOC
    cycle_budget_remaining = 1.0

    results = []
    rule_trace = []
    event_log = []

    previous_decision = None
    previous_relay_state = "OFF"

    print("===================================================")
    print(" HARSHIL'S SCENARIO")
    print(" Dynamic price + export limit + CO2 + battery wear")
    print(" Relay ON  = V2H discharge active")
    print(" Relay OFF = hold / charging / protection")
    print(" 24 simulated hours = 5 real minutes")
    print("===================================================")

    try:
        for row in input_rows:
            hour = row["hour"]
            soc_before = ev_soc

            controller_output = fuzzy_v2h_controller(
                row,
                ev_soc,
                cycle_budget_remaining,
            )

            decision = controller_output["decision"]
            ev_power = controller_output["ev_power_kw"]
            relay_on = controller_output["relay_on"]
            fuzzy_score = controller_output["fuzzy_score"]
            dominant_rule = controller_output["dominant_rule"]
            dominant_strength = controller_output.get("dominant_strength", 0.0)
            dominant_score = controller_output.get("dominant_score", 0.0)
            features = controller_output["features"]
            levels = controller_output["levels"]

            baseline_grid = features["net_load"]
            managed_grid = baseline_grid - ev_power

            if relay_on:
                relay.on()
                relay_state = "ON"
            else:
                relay.off()
                relay_state = "OFF"

            ev_soc = update_soc(ev_soc, ev_power)
            soc_after = ev_soc

            if ev_power > 0:
                cycle_budget_remaining -= ev_power / DAILY_DISCHARGE_BUDGET_KWH

                if cycle_budget_remaining < 0.0:
                    cycle_budget_remaining = 0.0

            print(
                f"{hour:02d}:00 | "
                f"Load={row['home_load_kw']:4.2f} | "
                f"PV={row['pv_actual_kw']:4.2f} | "
                f"GridBase={baseline_grid:5.2f} | "
                f"Price={row['import_price_c_per_kwh']:5.1f}c | "
                f"CO2={row['grid_co2_kg_per_kwh']:4.2f} | "
                f"Benefit={features['combined_benefit']:5.1f}c | "
                f"SOC={soc_before:5.1f}%->{soc_after:5.1f}% | "
                f"Score={fuzzy_score:6.1f} | "
                f"EV={ev_power:5.2f} | "
                f"GridManaged={managed_grid:5.2f} | "
                f"Relay={relay_state:3s} | "
                f"{decision}"
            )

            hourly_row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,

                "home_load_kw": row["home_load_kw"],
                "pv_actual_kw": row["pv_actual_kw"],
                "pv_forecast_kw": row["pv_forecast_kw"],

                "baseline_grid_kw": round(baseline_grid, 3),
                "managed_grid_kw": round(managed_grid, 3),

                "import_price_c_per_kwh": row["import_price_c_per_kwh"],
                "feed_in_price_c_per_kwh": row["feed_in_price_c_per_kwh"],
                "export_limit_kw": row["export_limit_kw"],
                "network_stress_level": row["network_stress_level"],
                "grid_co2_kg_per_kwh": row["grid_co2_kg_per_kwh"],

                "battery_temp_c": row["battery_temp_c"],
                "battery_wear_cost_c_per_kwh": row["battery_wear_cost_c_per_kwh"],
                "cycle_budget_remaining": round(cycle_budget_remaining, 3),

                "ev_available": row["ev_available"],
                "trip_reserve_soc": row["trip_reserve_soc"],
                "critical_load_level": row["critical_load_level"],

                "soc_before_percent": round(soc_before, 2),
                "soc_after_percent": round(soc_after, 2),
                "soc_margin_percent": round(features["soc_margin"], 2),

                "financial_benefit_c_per_kwh": round(features["financial_benefit"], 2),
                "carbon_bonus_c_per_kwh": round(features["carbon_bonus"], 2),
                "combined_benefit_c_per_kwh": round(features["combined_benefit"], 2),

                "pv_surplus_kw": round(features["pv_surplus"], 3),
                "export_pressure_kw": round(features["export_pressure"], 3),
                "forecast_error_kw": round(features["forecast_error_kw"], 3),
                "forecast_risk": round(features["forecast_risk"], 3),
                "wear_stress": round(features["wear_stress"], 3),

                # These fields fix the previous KeyError.
                "net_load_level": levels.get("net_load_level", "hard_rule"),
                "benefit_level": levels.get("benefit_level", "hard_rule"),
                "soc_margin_level": levels.get("soc_margin_level", "hard_rule"),
                "export_pressure_level": levels.get("export_pressure_level", "hard_rule"),
                "forecast_risk_level": levels.get("forecast_risk_level", "hard_rule"),
                "wear_stress_level": levels.get("wear_stress_level", "hard_rule"),
                "co2_level": levels.get("co2_level", "hard_rule"),

                "fuzzy_score": round(fuzzy_score, 2),
                "dominant_rule": dominant_rule,
                "dominant_strength": round(dominant_strength, 3),
                "dominant_score": round(dominant_score, 2),

                "decision": decision,
                "ev_power_kw": round(ev_power, 3),
                "relay_state": relay_state,
            }

            results.append(hourly_row)

            rule_trace.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "net_load_level": hourly_row["net_load_level"],
                "benefit_level": hourly_row["benefit_level"],
                "soc_margin_level": hourly_row["soc_margin_level"],
                "export_pressure_level": hourly_row["export_pressure_level"],
                "forecast_risk_level": hourly_row["forecast_risk_level"],
                "wear_stress_level": hourly_row["wear_stress_level"],
                "co2_level": hourly_row["co2_level"],
                "dominant_rule": dominant_rule,
                "dominant_strength": round(dominant_strength, 3),
                "dominant_score": round(dominant_score, 2),
                "fuzzy_score": round(fuzzy_score, 2),
                "decision": decision,
                "relay_state": relay_state,
            })

            if decision != previous_decision or relay_state != previous_relay_state:
                event_log.append({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "hour": hour,
                    "event": "STATE_CHANGE",
                    "decision": decision,
                    "relay_state": relay_state,
                    "ev_power_kw": round(ev_power, 3),
                    "soc_percent": round(soc_before, 2),
                })

            previous_decision = decision
            previous_relay_state = relay_state

            sleep(HOUR_DELAY_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        relay.off()
        print("Relay OFF safely.")

        if len(results) > 0:
            save_hourly_log(results)
            save_rule_trace(rule_trace)
            save_event_log(event_log)

            summary = create_summary_matrix(results)
            print_summary_matrix(summary)
            save_summary_matrix(summary)

            print(f"\nHourly log saved to: {HOURLY_LOG_FILE}")
            print(f"Summary matrix saved to: {SUMMARY_MATRIX_FILE}")
            print(f"Rule trace saved to: {RULE_TRACE_FILE}")
            print(f"Event log saved to: {EVENT_LOG_FILE}")
            print(f"Input CSV files are in: {DATA_DIR}/")
        else:
            print("No results recorded.")

        print("Harshil's scenario complete.")


if __name__ == "__main__":
    main()