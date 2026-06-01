from time import sleep
from datetime import datetime
import csv
import os

try:
    from gpiozero import OutputDevice
except ImportError:
    # Allows testing on laptop without Raspberry Pi GPIO.
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
# THESIS SCENARIO:
# V2H WITH SIMULATED CHARGING + BATTERY-WEAR-AWARE FUZZY CONTROL
# ============================================================
#
# Scenario design:
#   1) 14:00–16:00  Afternoon V2H event, relay ON
#   2) 16:00–17:00  Simulated PV/G2V charging, relay OFF
#   3) 17:00–21:00  Evening peak V2H event, relay ON
#
# Relay meaning:
#   Relay ON  = V2H discharge demonstration active
#   Relay OFF = hold / simulated charging / protection
#
# Hardware:
#   Relay IN2 -> Raspberry Pi GPIO27 / physical pin 13
#
# Timing:
#   24 simulated hours = 5 real minutes
#   1 simulated hour = 12.5 seconds
#
# ============================================================


# ============================================================
# REAL-WORLD DATA AND MODELLING REFERENCES
# ============================================================
#
# These references explain where the modelled values come from.
# The script uses representative hourly values for a lab-scale
# demonstration. Replace CSV rows with downloaded real data later
# if required.
#
# [R1] Residential load + rooftop PV profile shape:
#      Ausgrid Solar Home Electricity Data via CSIRO NEAR.
#      Provides half-hourly gross solar generation and household
#      consumption for 300 solar homes.
#      https://near.csiro.au/assets/42966a8f-bc3c-4bde-91d6-91bc5826aa21
#
# [R2] Rooftop PV actual and forecast modelling:
#      AEMO Australian Solar Energy Forecasting System, ASEFS.
#      Produces solar forecasts for large solar and small-scale
#      distributed rooftop PV.
#      https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/nem-forecasting-and-planning/operational-forecasting/solar-and-wind-energy-forecasting/australian-solar-energy-forecasting-system
#
# [R3] Retail import tariff:
#      Essential Services Commission Victoria Default Offer 2025–26.
#      Domestic two-period TOU tariff: peak 3 pm–9 pm, off-peak all
#      other times. This script uses CitiPower example rates:
#      peak = 36.33 c/kWh, off-peak = 22.06 c/kWh.
#      https://www.esc.vic.gov.au/electricity-and-gas/prices-tariffs-and-benchmarks/victorian-default-offer
#
# [R4] Feed-in tariff / export value:
#      ESC minimum feed-in tariff review 2025–26 showed very low
#      solar export values, including 0.00 c/kWh daytime and
#      6.57 c/kWh evening peak in the time-varying benchmark.
#      From 1 July 2025, ESC no longer sets a minimum feed-in tariff.
#      Retailer feed-in tariffs cannot be below zero.
#      https://www.esc.vic.gov.au/electricity-and-gas/prices-tariffs-and-benchmarks/minimum-feed-tariff/minimum-feed-tariff-review-2025-26
#
# [R5] CO2 factor:
#      Australian National Greenhouse Accounts Factors 2024.
#      Victoria grid electricity factor can be modelled around
#      0.86 kg CO2-e/kWh when scope 2 and scope 3 are combined.
#      This script uses hourly representative values around that
#      benchmark to show midday low-carbon and evening high-carbon
#      operation.
#      https://www.dcceew.gov.au/climate-change/publications/national-greenhouse-accounts-factors-2024
#
# [R6] Battery cost / degradation cost:
#      IEA Batteries and Secure Energy Transitions states lithium-ion
#      battery prices declined to less than USD 140/kWh in 2023.
#      This script uses a simplified wear-cost model for thesis
#      demonstration. It is not a warranty model.
#      https://www.iea.org/reports/batteries-and-secure-energy-transitions
#
# [R7] V2H / HEMS modelling variables:
#      Literature commonly uses EV SOC, availability, minimum SOC,
#      maximum charge/discharge power, user preference and EV
#      charge/discharge power as HEMS inputs/outputs.
#      See uploaded V2H and HEMS papers used in this project.
#
# ============================================================


# ============================================================
# RELAY SETUP
# ============================================================

RELAY_GPIO = 27
relay = OutputDevice(RELAY_GPIO, active_high=True, initial_value=False)


# ============================================================
# FOLDER AND FILE PATHS
# ============================================================

DATA_DIR = "data"
LOG_DIR = "logs"

ENERGY_PROFILE_FILE = os.path.join(DATA_DIR, "thesis_energy_profile.csv")
MARKET_NETWORK_FILE = os.path.join(DATA_DIR, "thesis_market_network.csv")
BATTERY_PROFILE_FILE = os.path.join(DATA_DIR, "thesis_battery_profile.csv")

HOURLY_LOG_FILE = os.path.join(LOG_DIR, "thesis_hourly_log.csv")
SUMMARY_MATRIX_FILE = os.path.join(LOG_DIR, "thesis_summary_matrix.csv")
RULE_TRACE_FILE = os.path.join(LOG_DIR, "thesis_rule_trace.csv")
EVENT_LOG_FILE = os.path.join(LOG_DIR, "thesis_event_log.csv")
SOURCE_REFERENCE_FILE = os.path.join(LOG_DIR, "thesis_data_source_references.csv")


# ============================================================
# SYSTEM SETTINGS
# ============================================================

EV_BATTERY_KWH = 60.0
EV_MAX_DISCHARGE_KW = 3.3
EV_MAX_CHARGE_KW = 3.3

DISCHARGE_EFFICIENCY = 0.95
CHARGE_EFFICIENCY = 0.90

SOC_MIN = 20.0
SOC_MAX = 95.0
INITIAL_SOC = 82.0

PV_REFERENCE_KW = 4.0

HOUR_DELAY_SECONDS = 12.5

# Controller threshold
V2H_SCORE_THRESHOLD = 55.0

# Daily V2H energy budget to avoid unrealistic battery cycling
DAILY_DISCHARGE_BUDGET_KWH = 16.0

# Notional control weighting for carbon and grid stress.
# These are not electricity market prices. They are controller weights.
CARBON_VALUE_C_PER_KG_CO2 = 10.0
GRID_STRESS_VALUE_C_PER_KWH = 20.0

# Battery aging model assumptions for summary only.
# Simplified equivalent-full-cycle model:
# EOL reference = 75% remaining capacity.
# Assumed cycle life to EOL = 3000 equivalent full cycles.
BATTERY_EOL_RETAINED_CAPACITY_PERCENT = 75.0
ASSUMED_CYCLE_LIFE_EFC = 3000.0
BASE_CAPACITY_FADE_PERCENT_PER_EFC = (
    (100.0 - BATTERY_EOL_RETAINED_CAPACITY_PERCENT) / ASSUMED_CYCLE_LIFE_EFC
)

# Always overwrite demo CSVs so the intended thesis scenario runs correctly.
OVERWRITE_INPUT_CSVS = True


# ============================================================
# SOURCE REFERENCE SUMMARY
# ============================================================

SOURCE_REFERENCES = [
    {
        "item": "home_load_kw and pv_actual_kw profile shape",
        "source": "Ausgrid Solar Home Electricity Data via CSIRO NEAR",
        "reason": "Half-hourly household consumption and rooftop PV generation for solar homes",
        "url": "https://near.csiro.au/assets/42966a8f-bc3c-4bde-91d6-91bc5826aa21",
    },
    {
        "item": "pv_forecast_kw and PV forecast error concept",
        "source": "AEMO Australian Solar Energy Forecasting System",
        "reason": "AEMO rooftop and utility solar forecasting framework",
        "url": "https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/nem-forecasting-and-planning/operational-forecasting/solar-and-wind-energy-forecasting/australian-solar-energy-forecasting-system",
    },
    {
        "item": "import_price_c_per_kwh",
        "source": "ESC Victorian Default Offer 2025–26",
        "reason": "Domestic two-period TOU tariff, peak 3 pm–9 pm and off-peak all other times",
        "url": "https://www.esc.vic.gov.au/electricity-and-gas/prices-tariffs-and-benchmarks/victorian-default-offer",
    },
    {
        "item": "feed_in_price_c_per_kwh",
        "source": "ESC minimum feed-in tariff review 2025–26",
        "reason": "Low daytime export value and higher evening export benchmark",
        "url": "https://www.esc.vic.gov.au/electricity-and-gas/prices-tariffs-and-benchmarks/minimum-feed-tariff/minimum-feed-tariff-review-2025-26",
    },
    {
        "item": "grid_co2_kg_per_kwh",
        "source": "Australian National Greenhouse Accounts Factors 2024",
        "reason": "Victoria grid electricity emissions factor used as benchmark",
        "url": "https://www.dcceew.gov.au/climate-change/publications/national-greenhouse-accounts-factors-2024",
    },
    {
        "item": "battery_wear_cost and capacity fade model",
        "source": "IEA Batteries and Secure Energy Transitions + V2H battery degradation literature",
        "reason": "Battery cost trend and simplified battery-throughput aging model",
        "url": "https://www.iea.org/reports/batteries-and-secure-energy-transitions",
    },
]


# ============================================================
# CREATE INPUT CSV FILES
# ============================================================

def create_input_csvs():
    os.makedirs(DATA_DIR, exist_ok=True)

    if OVERWRITE_INPUT_CSVS or not os.path.exists(ENERGY_PROFILE_FILE):
        energy_rows = [
            # hour, home_load_kw, pv_actual_kw, pv_forecast_kw, ev_available,
            # trip_reserve_soc, critical_load_level, control_window
            [0, 0.55, 0.00, 0.00, 1, 55, 0.20, "none"],
            [1, 0.48, 0.00, 0.00, 1, 55, 0.20, "none"],
            [2, 0.42, 0.00, 0.00, 1, 55, 0.20, "none"],
            [3, 0.40, 0.00, 0.00, 1, 55, 0.20, "none"],
            [4, 0.45, 0.00, 0.00, 1, 55, 0.20, "none"],
            [5, 0.65, 0.05, 0.10, 1, 55, 0.30, "none"],
            [6, 1.05, 0.25, 0.35, 1, 55, 0.40, "none"],

            # Commuter away period
            [7, 1.45, 0.65, 0.75, 0, 60, 0.50, "none"],
            [8, 1.30, 1.25, 1.40, 0, 60, 0.40, "none"],
            [9, 1.10, 2.10, 2.30, 0, 60, 0.30, "none"],

            # EV returns, PV is strong, but controller waits for planned event
            [10, 1.25, 3.25, 3.40, 1, 55, 0.30, "none"],
            [11, 1.50, 3.85, 4.00, 1, 55, 0.40, "none"],
            [12, 1.70, 4.00, 4.10, 1, 55, 0.45, "none"],
            [13, 1.80, 3.55, 3.60, 1, 55, 0.50, "none"],

            # 2-hour afternoon V2H support event
            [14, 2.90, 1.35, 1.50, 1, 55, 0.70, "afternoon_v2h"],
            [15, 3.10, 1.10, 1.20, 1, 55, 0.80, "afternoon_v2h"],

            # Simulated charging between afternoon V2H and evening peak V2H
            [16, 1.35, 2.45, 2.30, 1, 55, 0.30, "between_event_charging"],

            # 4-hour evening V2H support event, 17:00–21:00
            [17, 3.00, 0.55, 0.55, 1, 55, 0.80, "evening_v2h"],
            [18, 3.60, 0.00, 0.00, 1, 55, 1.00, "evening_v2h"],
            [19, 3.50, 0.00, 0.00, 1, 55, 1.00, "evening_v2h"],
            [20, 2.90, 0.00, 0.00, 1, 55, 0.90, "evening_v2h"],

            [21, 2.20, 0.00, 0.00, 1, 55, 0.70, "none"],
            [22, 1.40, 0.00, 0.00, 1, 55, 0.40, "none"],
            [23, 0.90, 0.00, 0.00, 1, 55, 0.30, "none"],
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
                "control_window",
            ])
            writer.writerows(energy_rows)

    if OVERWRITE_INPUT_CSVS or not os.path.exists(MARKET_NETWORK_FILE):
        market_rows = [
            # hour, import_price_c_per_kwh, feed_in_price_c_per_kwh,
            # export_limit_kw, grid_stress_level, grid_co2_kg_per_kwh

            # Off-peak retail price based on CitiPower VDO 2025–26: 22.06 c/kWh
            [0, 22.06, 1.00, 5.0, 0.20, 0.72],
            [1, 22.06, 1.00, 5.0, 0.20, 0.72],
            [2, 22.06, 1.00, 5.0, 0.20, 0.71],
            [3, 22.06, 1.00, 5.0, 0.20, 0.70],
            [4, 22.06, 1.00, 5.0, 0.25, 0.70],
            [5, 22.06, 1.00, 5.0, 0.30, 0.69],
            [6, 22.06, 1.00, 5.0, 0.40, 0.65],

            # Morning / midday low feed-in and lower CO2 because PV is strong
            [7, 22.06, 0.00, 4.0, 0.45, 0.58],
            [8, 22.06, 0.00, 3.0, 0.45, 0.50],
            [9, 22.06, 0.00, 2.0, 0.50, 0.42],
            [10, 22.06, 0.00, 1.5, 0.60, 0.34],
            [11, 22.06, 0.00, 1.0, 0.65, 0.28],
            [12, 22.06, 0.00, 0.8, 0.70, 0.25],
            [13, 22.06, 0.00, 0.8, 0.65, 0.30],

            # Afternoon support period, modelled stress rises
            [14, 22.06, 0.00, 1.0, 0.70, 0.55],

            # Peak retail price based on CitiPower VDO 2025–26: 36.33 c/kWh
            [15, 36.33, 6.57, 1.5, 0.80, 0.65],
            [16, 36.33, 6.57, 1.5, 0.75, 0.70],
            [17, 36.33, 6.57, 2.0, 0.88, 0.78],
            [18, 36.33, 6.57, 2.0, 0.95, 0.86],
            [19, 36.33, 6.57, 2.0, 0.95, 0.86],
            [20, 36.33, 6.57, 2.0, 0.90, 0.82],

            # Off-peak again after 9 pm
            [21, 22.06, 1.00, 4.0, 0.55, 0.75],
            [22, 22.06, 1.00, 5.0, 0.35, 0.72],
            [23, 22.06, 1.00, 5.0, 0.25, 0.70],
        ]

        with open(MARKET_NETWORK_FILE, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                "hour",
                "import_price_c_per_kwh",
                "feed_in_price_c_per_kwh",
                "export_limit_kw",
                "grid_stress_level",
                "grid_co2_kg_per_kwh",
            ])
            writer.writerows(market_rows)

    if OVERWRITE_INPUT_CSVS or not os.path.exists(BATTERY_PROFILE_FILE):
        battery_rows = [
            # hour, battery_temp_c, battery_wear_cost_c_per_kwh
            [0, 22, 7],
            [1, 22, 7],
            [2, 21, 7],
            [3, 21, 7],
            [4, 21, 7],
            [5, 22, 7],
            [6, 23, 7],
            [7, 24, 7],
            [8, 25, 7],
            [9, 26, 8],
            [10, 27, 8],
            [11, 29, 8],
            [12, 31, 9],
            [13, 32, 9],
            [14, 32, 9],
            [15, 31, 9],
            [16, 30, 9],
            [17, 30, 9],
            [18, 31, 10],
            [19, 31, 10],
            [20, 30, 10],
            [21, 28, 9],
            [22, 26, 8],
            [23, 24, 8],
        ]

        with open(BATTERY_PROFILE_FILE, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                "hour",
                "battery_temp_c",
                "battery_wear_cost_c_per_kwh",
            ])
            writer.writerows(battery_rows)


# ============================================================
# CSV LOADING
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
            "control_window": energy["control_window"],

            "import_price_c_per_kwh": float(market["import_price_c_per_kwh"]),
            "feed_in_price_c_per_kwh": float(market["feed_in_price_c_per_kwh"]),
            "export_limit_kw": float(market["export_limit_kw"]),
            "grid_stress_level": float(market["grid_stress_level"]),
            "grid_co2_kg_per_kwh": float(market["grid_co2_kg_per_kwh"]),

            "battery_temp_c": float(battery["battery_temp_c"]),
            "battery_wear_cost_c_per_kwh": float(battery["battery_wear_cost_c_per_kwh"]),
        })

    return combined_rows


# ============================================================
# FUZZY MEMBERSHIP FUNCTIONS
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


def best_label(membership_dict):
    selected_label = "unknown"
    selected_value = -1.0

    for label, value in membership_dict.items():
        if value > selected_value:
            selected_label = label
            selected_value = value

    return selected_label, selected_value


# ============================================================
# FEATURE CALCULATION
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

    financial_value = (
        row["import_price_c_per_kwh"]
        - row["feed_in_price_c_per_kwh"]
        - row["battery_wear_cost_c_per_kwh"]
    )

    carbon_value = row["grid_co2_kg_per_kwh"] * CARBON_VALUE_C_PER_KG_CO2
    grid_stress_value = row["grid_stress_level"] * GRID_STRESS_VALUE_C_PER_KWH

    value_signal = financial_value + carbon_value + grid_stress_value

    temperature_stress = max(row["battery_temp_c"] - 30.0, 0.0) / 15.0
    cycle_budget_stress = 1.0 - cycle_budget_remaining
    wear_cost_stress = row["battery_wear_cost_c_per_kwh"] / 15.0

    battery_wear_stress = (
        0.40 * temperature_stress
        + 0.35 * cycle_budget_stress
        + 0.25 * wear_cost_stress
    )

    if battery_wear_stress > 1.0:
        battery_wear_stress = 1.0

    return {
        "net_load": net_load,
        "pv_surplus": pv_surplus,
        "export_pressure": export_pressure,
        "forecast_error_kw": forecast_error_kw,
        "forecast_risk": forecast_risk,
        "soc_margin": soc_margin,

        "financial_value_c_per_kwh": financial_value,
        "carbon_value_c_per_kwh": carbon_value,
        "grid_stress_value_c_per_kwh": grid_stress_value,
        "value_signal_c_per_kwh": value_signal,

        "battery_wear_stress": battery_wear_stress,
    }


# ============================================================
# FUZZY V2H CONTROLLER
# ============================================================
#
# Fuzzy inputs used:
#   1. net_load
#   2. value_signal
#   3. soc_margin
#   4. grid_stress_level
#   5. battery_wear_stress
#
# Non-fuzzy hard protections:
#   - EV availability
#   - SOC minimum
#   - trip reserve SOC
#   - daily discharge budget
#   - control window permission
#   - charging priority
#
# ============================================================

def fuzzy_v2h_controller(row, soc, cycle_budget_remaining):
    hour = row["hour"]
    control_window = row["control_window"]

    features = calculate_features(row, soc, cycle_budget_remaining)

    net_load = features["net_load"]
    value_signal = features["value_signal_c_per_kwh"]
    soc_margin = features["soc_margin"]
    grid_stress = row["grid_stress_level"]
    battery_wear_stress = features["battery_wear_stress"]

    # -----------------------------
    # Hard protection rules
    # -----------------------------

    if row["ev_available"] == 0:
        return {
            "decision": "EV_NOT_AVAILABLE",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_score": 0.0,
            "dominant_rule": "Hard rule: EV not available",
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
            "dominant_rule": "Hard rule: trip reserve protection",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    if cycle_budget_remaining <= 0:
        return {
            "decision": "DAILY_CYCLE_BUDGET_PROTECTION",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_score": 0.0,
            "dominant_rule": "Hard rule: daily V2H energy budget exhausted",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    # -----------------------------
    # Simulated charging mode
    # -----------------------------
    # Charging is simulated only.
    # Relay stays OFF because the relay/lamp is used to show V2H discharge.

    if control_window == "between_event_charging":
        available_battery_room_kwh = ((SOC_MAX - soc) / 100.0) * EV_BATTERY_KWH

        charge_power = min(
            EV_MAX_CHARGE_KW,
            max(features["pv_surplus"], 0.0),
            available_battery_room_kwh,
        )

        if charge_power > 0.05:
            return {
                "decision": "SIMULATED_PV_G2V_CHARGING",
                "ev_power_kw": -charge_power,
                "relay_on": False,
                "fuzzy_score": -70.0,
                "dominant_rule": "Charging rule: between-event PV surplus charging",
                "dominant_strength": 1.0,
                "dominant_score": -70.0,
                "features": features,
                "levels": {},
            }

    # -----------------------------
    # Fuzzification
    # -----------------------------

    net_load_mf = {
        "surplus": trapezoid(net_load, -5.0, -5.0, -0.8, -0.1),
        "low_deficit": triangle(net_load, 0.1, 0.9, 1.8),
        "medium_deficit": triangle(net_load, 1.2, 2.5, 3.8),
        "high_deficit": trapezoid(net_load, 3.2, 4.0, 6.0, 6.0),
    }

    value_mf = {
        "low": trapezoid(value_signal, -50.0, -50.0, 8.0, 15.0),
        "medium": triangle(value_signal, 12.0, 24.0, 36.0),
        "high": trapezoid(value_signal, 30.0, 42.0, 80.0, 80.0),
    }

    soc_margin_mf = {
        "critical": trapezoid(soc_margin, -20.0, -20.0, 2.0, 6.0),
        "safe": triangle(soc_margin, 4.0, 15.0, 28.0),
        "high": trapezoid(soc_margin, 22.0, 32.0, 70.0, 70.0),
    }

    grid_stress_mf = {
        "low": trapezoid(grid_stress, 0.0, 0.0, 0.25, 0.45),
        "medium": triangle(grid_stress, 0.35, 0.60, 0.80),
        "high": trapezoid(grid_stress, 0.70, 0.85, 1.00, 1.00),
    }

    battery_wear_mf = {
        "low": trapezoid(battery_wear_stress, 0.0, 0.0, 0.25, 0.45),
        "medium": triangle(battery_wear_stress, 0.30, 0.55, 0.80),
        "high": trapezoid(battery_wear_stress, 0.70, 0.85, 1.00, 1.00),
    }

    levels = {
        "net_load_level": best_label(net_load_mf)[0],
        "value_signal_level": best_label(value_mf)[0],
        "soc_margin_level": best_label(soc_margin_mf)[0],
        "grid_stress_level_fuzzy": best_label(grid_stress_mf)[0],
        "battery_wear_level": best_label(battery_wear_mf)[0],
    }

    # -----------------------------
    # Fuzzy rule base
    # -----------------------------
    # Output score:
    #   0   = hold
    #   60  = minor/weak V2H
    #   80  = medium V2H
    #   100 = major V2H

    rules = []

    rules.append((
        max(
            net_load_mf["surplus"],
            value_mf["low"],
            soc_margin_mf["critical"],
            battery_wear_mf["high"],
        ),
        0.0,
        "Hold: no deficit / low value / SOC critical / high battery wear",
    ))

    rules.append((
        min(
            net_load_mf["low_deficit"],
            value_mf["high"],
            soc_margin_mf["high"],
            grid_stress_mf["high"],
            battery_wear_mf["low"],
        ),
        60.0,
        "Weak V2H: low deficit but high value and safe battery",
    ))

    rules.append((
        min(
            max(net_load_mf["low_deficit"], net_load_mf["medium_deficit"]),
            max(value_mf["medium"], value_mf["high"]),
            max(soc_margin_mf["safe"], soc_margin_mf["high"]),
            max(grid_stress_mf["medium"], grid_stress_mf["high"]),
            max(battery_wear_mf["low"], battery_wear_mf["medium"]),
        ),
        75.0,
        "Afternoon support: useful deficit + value + grid stress + safe SOC",
    ))

    rules.append((
        min(
            net_load_mf["medium_deficit"],
            value_mf["high"],
            max(soc_margin_mf["safe"], soc_margin_mf["high"]),
            grid_stress_mf["high"],
            max(battery_wear_mf["low"], battery_wear_mf["medium"]),
        ),
        88.0,
        "Strong V2H: medium deficit + high value + high grid stress",
    ))

    rules.append((
        min(
            net_load_mf["high_deficit"],
            value_mf["high"],
            max(soc_margin_mf["safe"], soc_margin_mf["high"]),
            grid_stress_mf["high"],
            max(battery_wear_mf["low"], battery_wear_mf["medium"]),
        ),
        100.0,
        "Major V2H: high deficit + high value + high grid stress + acceptable wear",
    ))

    rules.append((
        min(
            grid_stress_mf["high"],
            max(net_load_mf["medium_deficit"], net_load_mf["high_deficit"]),
            max(value_mf["medium"], value_mf["high"]),
            soc_margin_mf["safe"],
        ),
        85.0,
        "Network support: grid stress high and SOC still above reserve",
    ))

    numerator = 0.0
    denominator = 0.0

    dominant_rule = "No active fuzzy rule"
    dominant_strength = 0.0
    dominant_score = 0.0

    for strength, score, rule_name in rules:
        numerator += strength * score
        denominator += strength

        if strength > dominant_strength:
            dominant_strength = strength
            dominant_score = score
            dominant_rule = rule_name

    fuzzy_score = numerator / denominator if denominator != 0 else 0.0

    # -----------------------------
    # User-approved control window
    # -----------------------------

    v2h_window_allowed = control_window in ["afternoon_v2h", "evening_v2h"]

    if not v2h_window_allowed:
        return {
            "decision": "HOLD_NOT_IN_USER_V2H_WINDOW",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_score": fuzzy_score,
            "dominant_rule": dominant_rule,
            "dominant_strength": dominant_strength,
            "dominant_score": dominant_score,
            "features": features,
            "levels": levels,
        }

    # -----------------------------
    # Final discharge power decision
    # -----------------------------

    max_energy_allowed_by_soc_kwh = max(
        ((soc - row["trip_reserve_soc"]) / 100.0) * EV_BATTERY_KWH,
        0.0,
    )

    max_power_allowed_by_budget_kw = max(
        cycle_budget_remaining * DAILY_DISCHARGE_BUDGET_KWH,
        0.0,
    )

    if fuzzy_score >= V2H_SCORE_THRESHOLD and net_load > 0:
        if control_window == "afternoon_v2h":
            requested_power = min(1.6, net_load)
            decision = "AFTERNOON_2H_MINOR_V2H_EVENT"

        elif control_window == "evening_v2h":
            if fuzzy_score >= 85.0:
                requested_power = min(EV_MAX_DISCHARGE_KW, net_load)
                decision = "EVENING_4H_MAJOR_V2H_EVENT"
            else:
                requested_power = min(2.2, net_load)
                decision = "EVENING_4H_MODERATE_V2H_EVENT"

        else:
            requested_power = 0.0
            decision = "HOLD"

        ev_power = min(
            requested_power,
            max_energy_allowed_by_soc_kwh,
            max_power_allowed_by_budget_kw,
        )

        if ev_power > 0.05:
            relay_on = True
        else:
            ev_power = 0.0
            relay_on = False
            decision = "SOC_OR_BUDGET_LIMITED_HOLD"

    else:
        ev_power = 0.0
        relay_on = False
        decision = "FUZZY_SCORE_BELOW_THRESHOLD_HOLD"

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
# SOC UPDATE
# ============================================================

def update_soc(soc, ev_power_kw):
    # ev_power_kw > 0 means V2H discharge to home.
    # ev_power_kw < 0 means simulated charging.

    if ev_power_kw > 0:
        battery_energy_used_kwh = ev_power_kw / DISCHARGE_EFFICIENCY
        soc_change = (battery_energy_used_kwh / EV_BATTERY_KWH) * 100.0
        new_soc = soc - soc_change

    elif ev_power_kw < 0:
        charge_input_kwh = abs(ev_power_kw)
        stored_energy_kwh = charge_input_kwh * CHARGE_EFFICIENCY
        soc_change = (stored_energy_kwh / EV_BATTERY_KWH) * 100.0
        new_soc = soc + soc_change

    else:
        new_soc = soc

    if new_soc < SOC_MIN:
        new_soc = SOC_MIN

    if new_soc > SOC_MAX:
        new_soc = SOC_MAX

    return new_soc


# ============================================================
# GRID IMPORT / EXPORT HELPER
# ============================================================

def import_export_curtailment(grid_power_kw, export_limit_kw):
    import_kw = max(grid_power_kw, 0.0)

    export_candidate_kw = max(-grid_power_kw, 0.0)
    export_kw = min(export_candidate_kw, export_limit_kw)
    curtailment_kw = max(export_candidate_kw - export_limit_kw, 0.0)

    return import_kw, export_kw, curtailment_kw


# ============================================================
# SUMMARY MATRIX
# ============================================================

def hours_list(results, condition):
    selected = []

    for row in results:
        if condition(row):
            selected.append(f"{row['hour']:02d}:00")

    return ", ".join(selected) if selected else "None"


def create_summary_matrix(results):
    total_home_load_kwh = sum(row["home_load_kw"] for row in results)
    total_pv_kwh = sum(row["pv_actual_kw"] for row in results)

    without_v2h_import_kwh = 0.0
    with_v2h_import_kwh = 0.0

    without_v2h_export_kwh = 0.0
    with_v2h_export_kwh = 0.0

    without_v2h_curtailment_kwh = 0.0
    with_v2h_curtailment_kwh = 0.0

    without_v2h_bill = 0.0
    with_v2h_bill_before_wear = 0.0

    without_v2h_emissions = 0.0
    with_v2h_emissions = 0.0

    without_v2h_peak_import_kw = 0.0
    with_v2h_peak_import_kw = 0.0

    battery_wear_cost = 0.0

    ev_discharge_energy_to_home_kwh = 0.0
    battery_energy_depleted_kwh = 0.0

    ev_charge_input_kwh = 0.0
    ev_charge_stored_kwh = 0.0

    afternoon_v2h_energy_kwh = 0.0
    evening_v2h_energy_kwh = 0.0

    battery_throughput_kwh = 0.0

    forecast_risk_holds = 0
    reserve_violations = 0

    for row in results:
        baseline_grid = row["without_v2h_grid_kw"]
        managed_grid = row["with_v2h_grid_kw"]
        export_limit = row["export_limit_kw"]

        base_imp, base_exp, base_curt = import_export_curtailment(
            baseline_grid,
            export_limit,
        )

        managed_imp, managed_exp, managed_curt = import_export_curtailment(
            managed_grid,
            export_limit,
        )

        without_v2h_import_kwh += base_imp
        with_v2h_import_kwh += managed_imp

        without_v2h_export_kwh += base_exp
        with_v2h_export_kwh += managed_exp

        without_v2h_curtailment_kwh += base_curt
        with_v2h_curtailment_kwh += managed_curt

        without_v2h_bill += (
            base_imp * (row["import_price_c_per_kwh"] / 100.0)
            - base_exp * (row["feed_in_price_c_per_kwh"] / 100.0)
        )

        with_v2h_bill_before_wear += (
            managed_imp * (row["import_price_c_per_kwh"] / 100.0)
            - managed_exp * (row["feed_in_price_c_per_kwh"] / 100.0)
        )

        without_v2h_emissions += base_imp * row["grid_co2_kg_per_kwh"]
        with_v2h_emissions += managed_imp * row["grid_co2_kg_per_kwh"]

        without_v2h_peak_import_kw = max(without_v2h_peak_import_kw, base_imp)
        with_v2h_peak_import_kw = max(with_v2h_peak_import_kw, managed_imp)

        if row["ev_power_kw"] > 0:
            ev_discharge_energy_to_home_kwh += row["ev_power_kw"]
            battery_depleted = row["ev_power_kw"] / DISCHARGE_EFFICIENCY
            battery_energy_depleted_kwh += battery_depleted
            battery_throughput_kwh += battery_depleted

            battery_wear_cost += row["ev_power_kw"] * (
                row["battery_wear_cost_c_per_kwh"] / 100.0
            )

            if row["control_window"] == "afternoon_v2h":
                afternoon_v2h_energy_kwh += row["ev_power_kw"]

            if row["control_window"] == "evening_v2h":
                evening_v2h_energy_kwh += row["ev_power_kw"]

        if row["ev_power_kw"] < 0:
            charge_input = abs(row["ev_power_kw"])
            charge_stored = charge_input * CHARGE_EFFICIENCY

            ev_charge_input_kwh += charge_input
            ev_charge_stored_kwh += charge_stored
            battery_throughput_kwh += charge_stored

        if row["soc_after_percent"] < row["trip_reserve_soc"]:
            reserve_violations += 1

        if row["decision"] == "HOLD_NOT_IN_USER_V2H_WINDOW" and row["forecast_risk"] > 0.30:
            forecast_risk_holds += 1

    with_v2h_bill_after_wear = with_v2h_bill_before_wear + battery_wear_cost

    gross_bill_saving_before_wear = without_v2h_bill - with_v2h_bill_before_wear
    net_bill_saving_after_wear = without_v2h_bill - with_v2h_bill_after_wear

    import_reduction_kwh = without_v2h_import_kwh - with_v2h_import_kwh
    emissions_avoided_kg = without_v2h_emissions - with_v2h_emissions

    peak_reduction_kw = without_v2h_peak_import_kw - with_v2h_peak_import_kw

    if without_v2h_peak_import_kw > 0:
        peak_reduction_percent = (peak_reduction_kw / without_v2h_peak_import_kw) * 100.0
    else:
        peak_reduction_percent = 0.0

    curtailment_reduction_kwh = without_v2h_curtailment_kwh - with_v2h_curtailment_kwh

    equivalent_full_cycles_throughput = battery_throughput_kwh / (2.0 * EV_BATTERY_KWH)
    equivalent_full_cycles_discharge_only = battery_energy_depleted_kwh / EV_BATTERY_KWH

    avg_battery_temp = sum(row["battery_temp_c"] for row in results) / len(results)
    temperature_multiplier = 1.0 + max(avg_battery_temp - 25.0, 0.0) * 0.02

    estimated_capacity_fade_percent = (
        equivalent_full_cycles_throughput
        * BASE_CAPACITY_FADE_PERCENT_PER_EFC
        * temperature_multiplier
    )

    relay_on_hours = sum(1 for row in results if row["relay_state"] == "ON")

    active_v2h_rows = [row for row in results if row["relay_state"] == "ON"]
    active_scores = [row["fuzzy_score"] for row in active_v2h_rows]

    if active_scores:
        avg_active_fuzzy_score = sum(active_scores) / len(active_scores)
        max_active_fuzzy_score = max(active_scores)
    else:
        avg_active_fuzzy_score = 0.0
        max_active_fuzzy_score = 0.0

    final_soc = results[-1]["soc_after_percent"]
    min_soc = min(row["soc_after_percent"] for row in results)
    final_reserve_margin = final_soc - results[-1]["trip_reserve_soc"]

    next_day_ready = "Yes" if final_reserve_margin >= 0 else "No"

    decision_counts = {}

    for row in results:
        decision = row["decision"]

        if decision not in decision_counts:
            decision_counts[decision] = 0

        decision_counts[decision] += 1

    matrix = []

    def add(section, metric, value, unit, note):
        matrix.append({
            "section": section,
            "metric": metric,
            "value": value,
            "unit": unit,
            "note": note,
        })

    add("DATA & MODELLING SOURCES", "Load/PV profile basis", "Ausgrid/CSIRO NEAR", "-", "Hourly values are modelled from real household + rooftop PV profile shapes")
    add("DATA & MODELLING SOURCES", "PV forecast basis", "AEMO ASEFS", "-", "PV forecast values are representative forecast estimates")
    add("DATA & MODELLING SOURCES", "Import tariff basis", "ESC VDO 2025-26 CitiPower TOU", "-", "Peak 3 pm-9 pm, off-peak otherwise")
    add("DATA & MODELLING SOURCES", "Feed-in value basis", "ESC FiT 2025-26 benchmark", "-", "Low daytime export value and evening export value")
    add("DATA & MODELLING SOURCES", "CO2 basis", "NGA Victoria factor + hourly shape", "-", "Representative hourly CO2 values for control logic")
    add("DATA & MODELLING SOURCES", "Battery wear basis", "IEA + simplified EFC model", "-", "Battery wear model is simplified for thesis demonstration")

    add("FUZZY CONTROLLER", "Number of fuzzy inputs", 5, "inputs", "net load, value signal, SOC margin, grid stress, battery wear")
    add("FUZZY CONTROLLER", "Fuzzy input 1", "net_load", "kW", "House demand minus PV generation")
    add("FUZZY CONTROLLER", "Fuzzy input 2", "value_signal", "c/kWh equivalent", "Import price, feed-in value, carbon value and grid stress value")
    add("FUZZY CONTROLLER", "Fuzzy input 3", "soc_margin", "%", "SOC above user trip reserve")
    add("FUZZY CONTROLLER", "Fuzzy input 4", "grid_stress", "0-1", "Grid/network stress level")
    add("FUZZY CONTROLLER", "Fuzzy input 5", "battery_wear_stress", "0-1", "Battery temperature, cycle budget and wear cost")
    add("FUZZY CONTROLLER", "Average fuzzy score during V2H", round(avg_active_fuzzy_score, 2), "%", "Average score only during relay ON V2H hours")
    add("FUZZY CONTROLLER", "Maximum fuzzy score during V2H", round(max_active_fuzzy_score, 2), "%", "Highest V2H confidence score")
    add("FUZZY CONTROLLER", "Forecast-risk hold events", forecast_risk_holds, "hours", "Times controller held while forecast uncertainty was high")

    add("SCENARIO OPERATION", "Afternoon V2H event period", "14:00-16:00", "-", "2-hour planned V2H event")
    add("SCENARIO OPERATION", "Between-event charging period", "16:00-17:00", "-", "Simulated charging only, relay OFF")
    add("SCENARIO OPERATION", "Evening V2H event period", "17:00-21:00", "-", "4-hour evening peak support event")
    add("SCENARIO OPERATION", "Relay ON hours", relay_on_hours, "hours", "Physical relay/lamp active only for V2H discharge")
    add("SCENARIO OPERATION", "Relay ON periods", hours_list(results, lambda r: r["relay_state"] == "ON"), "-", "Actual relay ON hours")
    add("SCENARIO OPERATION", "Charging periods", hours_list(results, lambda r: r["ev_power_kw"] < 0), "-", "Simulated charging hours, relay OFF")

    add("ENERGY BALANCE", "Total home load", round(total_home_load_kwh, 2), "kWh", "Daily household load")
    add("ENERGY BALANCE", "Total PV generation", round(total_pv_kwh, 2), "kWh", "Daily rooftop PV generation")
    add("ENERGY BALANCE", "EV discharge to home", round(ev_discharge_energy_to_home_kwh, 2), "kWh", "Useful V2H energy supplied to home")
    add("ENERGY BALANCE", "Afternoon V2H energy", round(afternoon_v2h_energy_kwh, 2), "kWh", "Energy supplied in 2-hour event")
    add("ENERGY BALANCE", "Evening V2H energy", round(evening_v2h_energy_kwh, 2), "kWh", "Energy supplied in 4-hour event")
    add("ENERGY BALANCE", "Simulated charging input", round(ev_charge_input_kwh, 2), "kWh", "Charging energy drawn from PV surplus/control window")
    add("ENERGY BALANCE", "Stored charging energy", round(ev_charge_stored_kwh, 2), "kWh", "Energy added to battery after charging efficiency")

    add("WITH V2H vs WITHOUT V2H", "Grid import without V2H", round(without_v2h_import_kwh, 2), "kWh", "Baseline import if EV is not used")
    add("WITH V2H vs WITHOUT V2H", "Grid import with V2H", round(with_v2h_import_kwh, 2), "kWh", "Managed import with V2H and simulated charging")
    add("WITH V2H vs WITHOUT V2H", "Grid import reduction", round(import_reduction_kwh, 2), "kWh", "Import reduction from V2H operation")
    add("WITH V2H vs WITHOUT V2H", "Peak import without V2H", round(without_v2h_peak_import_kw, 2), "kW", "Highest baseline grid import")
    add("WITH V2H vs WITHOUT V2H", "Peak import with V2H", round(with_v2h_peak_import_kw, 2), "kW", "Highest managed grid import")
    add("WITH V2H vs WITHOUT V2H", "Peak import reduction", round(peak_reduction_kw, 2), "kW", "Peak shaving result")
    add("WITH V2H vs WITHOUT V2H", "Peak import reduction", round(peak_reduction_percent, 1), "%", "Percentage peak reduction")

    add("COST COMPARISON", "Bill without V2H", round(without_v2h_bill, 2), "$", "Baseline daily electricity cost")
    add("COST COMPARISON", "Bill with V2H before wear", round(with_v2h_bill_before_wear, 2), "$", "Managed bill before battery wear")
    add("COST COMPARISON", "Battery wear cost", round(battery_wear_cost, 2), "$", "Estimated user battery degradation cost")
    add("COST COMPARISON", "Bill with V2H after wear", round(with_v2h_bill_after_wear, 2), "$", "Managed bill including wear cost")
    add("COST COMPARISON", "Gross saving before wear", round(gross_bill_saving_before_wear, 2), "$", "Saving before battery degradation cost")
    add("COST COMPARISON", "Net saving after wear", round(net_bill_saving_after_wear, 2), "$", "Saving after user battery wear cost")

    add("CO2 COMPARISON", "CO2 without V2H", round(without_v2h_emissions, 2), "kg CO2-e", "Baseline grid import emissions")
    add("CO2 COMPARISON", "CO2 with V2H", round(with_v2h_emissions, 2), "kg CO2-e", "Managed grid import emissions")
    add("CO2 COMPARISON", "CO2 avoided", round(emissions_avoided_kg, 2), "kg CO2-e", "Emission reduction from reduced peak grid import")

    add("PV EXPORT / CURTAILMENT", "PV export without V2H", round(without_v2h_export_kwh, 2), "kWh", "Baseline exported PV")
    add("PV EXPORT / CURTAILMENT", "PV export with V2H", round(with_v2h_export_kwh, 2), "kWh", "Managed exported PV")
    add("PV EXPORT / CURTAILMENT", "Curtailment without V2H", round(without_v2h_curtailment_kwh, 2), "kWh", "Baseline export-limit curtailment")
    add("PV EXPORT / CURTAILMENT", "Curtailment with V2H", round(with_v2h_curtailment_kwh, 2), "kWh", "Managed export-limit curtailment")
    add("PV EXPORT / CURTAILMENT", "Curtailment reduction", round(curtailment_reduction_kwh, 2), "kWh", "PV curtailment avoided")

    add("BATTERY SOC & AGING", "Initial SOC", round(INITIAL_SOC, 2), "%", "SOC at start of day")
    add("BATTERY SOC & AGING", "Final SOC", round(final_soc, 2), "%", "SOC at end of day")
    add("BATTERY SOC & AGING", "Minimum SOC", round(min_soc, 2), "%", "Lowest SOC reached")
    add("BATTERY SOC & AGING", "Final SOC margin above reserve", round(final_reserve_margin, 2), "%", "Final SOC minus user trip reserve")
    add("BATTERY SOC & AGING", "Next-day commute ready", next_day_ready, "-", "Yes if final SOC remains above reserve")
    add("BATTERY SOC & AGING", "Reserve violation hours", reserve_violations, "hours", "Hours where SOC dropped below trip reserve")
    add("BATTERY SOC & AGING", "Battery throughput", round(battery_throughput_kwh, 2), "kWh", "Charge/discharge throughput used for aging estimate")
    add("BATTERY SOC & AGING", "EFC throughput estimate", round(equivalent_full_cycles_throughput, 4), "EFC", "Equivalent full cycles from charge/discharge throughput")
    add("BATTERY SOC & AGING", "EFC discharge-only estimate", round(equivalent_full_cycles_discharge_only, 4), "EFC", "Equivalent full cycles from discharge energy")
    add("BATTERY SOC & AGING", "Estimated capacity fade", round(estimated_capacity_fade_percent, 5), "%", "Simplified aging estimate for this one-day event")

    for decision, count in decision_counts.items():
        add("DECISION COUNTS", decision, count, "hours", "Number of hours this controller decision occurred")

    return matrix


def print_summary_matrix(matrix):
    print("\n================ THESIS SUMMARY MATRIX ================")

    current_section = None

    for row in matrix:
        if row["section"] != current_section:
            current_section = row["section"]
            print(f"\n--- {current_section} ---")

        print(
            f"{row['metric']:<42} "
            f"{str(row['value']):<22} "
            f"{row['unit']:<12} "
            f"{row['note']}"
        )

    print("\n=======================================================")


def save_summary_matrix(matrix):
    with open(SUMMARY_MATRIX_FILE, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=[
            "section",
            "metric",
            "value",
            "unit",
            "note",
        ])

        writer.writeheader()
        writer.writerows(matrix)


def save_source_references():
    with open(SOURCE_REFERENCE_FILE, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=[
            "item",
            "source",
            "reason",
            "url",
        ])

        writer.writeheader()
        writer.writerows(SOURCE_REFERENCES)


# ============================================================
# LOGGING HELPERS
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
# MAIN PROGRAM
# ============================================================

def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    create_input_csvs()
    save_source_references()

    input_rows = load_input_data()

    ev_soc = INITIAL_SOC
    cycle_budget_remaining = 1.0

    results = []
    rule_trace = []
    event_log = []

    previous_decision = None
    previous_relay_state = "OFF"

    print("===================================================")
    print(" THESIS V2H SCENARIO STARTED")
    print(" 14:00-16:00 = 2-hour afternoon V2H relay event")
    print(" 16:00-17:00 = simulated charging, relay OFF")
    print(" 17:00-21:00 = 4-hour evening V2H relay event")
    print(" Relay ON    = V2H discharge active")
    print(" Relay OFF   = hold / simulated charging / protection")
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

            without_v2h_grid = features["net_load"]
            with_v2h_grid = without_v2h_grid - ev_power

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
                f"Window={row['control_window']:<23} | "
                f"Load={row['home_load_kw']:4.2f} kW | "
                f"PV={row['pv_actual_kw']:4.2f} kW | "
                f"NoV2HGrid={without_v2h_grid:5.2f} kW | "
                f"WithV2HGrid={with_v2h_grid:5.2f} kW | "
                f"SOC={soc_before:5.1f}%->{soc_after:5.1f}% | "
                f"Value={features['value_signal_c_per_kwh']:5.1f}c | "
                f"Wear={features['battery_wear_stress']:4.2f} | "
                f"Score={fuzzy_score:6.1f} | "
                f"EV={ev_power:5.2f} kW | "
                f"Relay={relay_state:3s} | "
                f"{decision}"
            )

            hourly_row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,
                "control_window": row["control_window"],

                "home_load_kw": row["home_load_kw"],
                "pv_actual_kw": row["pv_actual_kw"],
                "pv_forecast_kw": row["pv_forecast_kw"],

                "without_v2h_grid_kw": round(without_v2h_grid, 3),
                "with_v2h_grid_kw": round(with_v2h_grid, 3),

                "import_price_c_per_kwh": row["import_price_c_per_kwh"],
                "feed_in_price_c_per_kwh": row["feed_in_price_c_per_kwh"],
                "export_limit_kw": row["export_limit_kw"],
                "grid_stress_level": row["grid_stress_level"],
                "grid_co2_kg_per_kwh": row["grid_co2_kg_per_kwh"],

                "battery_temp_c": row["battery_temp_c"],
                "battery_wear_cost_c_per_kwh": row["battery_wear_cost_c_per_kwh"],
                "battery_wear_stress": round(features["battery_wear_stress"], 3),

                "cycle_budget_remaining": round(cycle_budget_remaining, 3),

                "ev_available": row["ev_available"],
                "trip_reserve_soc": row["trip_reserve_soc"],
                "critical_load_level": row["critical_load_level"],

                "soc_before_percent": round(soc_before, 2),
                "soc_after_percent": round(soc_after, 2),
                "soc_margin_percent": round(features["soc_margin"], 2),

                "pv_surplus_kw": round(features["pv_surplus"], 3),
                "export_pressure_kw": round(features["export_pressure"], 3),
                "forecast_error_kw": round(features["forecast_error_kw"], 3),
                "forecast_risk": round(features["forecast_risk"], 3),

                "financial_value_c_per_kwh": round(features["financial_value_c_per_kwh"], 2),
                "carbon_value_c_per_kwh": round(features["carbon_value_c_per_kwh"], 2),
                "grid_stress_value_c_per_kwh": round(features["grid_stress_value_c_per_kwh"], 2),
                "value_signal_c_per_kwh": round(features["value_signal_c_per_kwh"], 2),

                "net_load_level": levels.get("net_load_level", "hard_rule"),
                "value_signal_level": levels.get("value_signal_level", "hard_rule"),
                "soc_margin_level": levels.get("soc_margin_level", "hard_rule"),
                "grid_stress_level_fuzzy": levels.get("grid_stress_level_fuzzy", "hard_rule"),
                "battery_wear_level": levels.get("battery_wear_level", "hard_rule"),

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
                "control_window": row["control_window"],
                "net_load_level": hourly_row["net_load_level"],
                "value_signal_level": hourly_row["value_signal_level"],
                "soc_margin_level": hourly_row["soc_margin_level"],
                "grid_stress_level_fuzzy": hourly_row["grid_stress_level_fuzzy"],
                "battery_wear_level": hourly_row["battery_wear_level"],
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
        print("\nRelay OFF safely.")

        if len(results) > 0:
            save_hourly_log(results)
            save_rule_trace(rule_trace)
            save_event_log(event_log)

            summary_matrix = create_summary_matrix(results)
            print_summary_matrix(summary_matrix)
            save_summary_matrix(summary_matrix)

            print(f"\nHourly log saved to: {HOURLY_LOG_FILE}")
            print(f"Summary matrix saved to: {SUMMARY_MATRIX_FILE}")
            print(f"Rule trace saved to: {RULE_TRACE_FILE}")
            print(f"Event log saved to: {EVENT_LOG_FILE}")
            print(f"Source references saved to: {SOURCE_REFERENCE_FILE}")
            print(f"Input CSV files saved in: {DATA_DIR}/")
        else:
            print("No results recorded.")

        print("Thesis V2H scenario complete.")


if __name__ == "__main__":
    main()