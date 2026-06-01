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
# SCENARIO 2: HARSHIL'S SCENARIO
# FUZZY DECISION CONTROLLER FOR HOLD / CHARGE / DISCHARGE
# ============================================================
#
# Main idea:
# The fuzzy controller decides the action each hour:
#
#   HOLD       = no EV energy action
#   CHARGE     = EV charging mode, relay OFF
#   DISCHARGE  = V2H mode, relay ON
#
# Relay rule:
#   Relay ON  = DISCHARGE only
#   Relay OFF = HOLD or CHARGE
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
# The values below are representative scenario values for a
# lab-scale thesis demonstration. They are not live API values.
# The profile shapes and assumptions are based on the references
# listed here.
#
# [R1] Residential load + rooftop PV profile shape:
#      Ausgrid Solar Home Electricity Data via CSIRO NEAR.
#      Provides half-hourly gross solar generation and household
#      consumption for solar homes.
#      https://near.csiro.au/assets/42966a8f-bc3c-4bde-91d6-91bc5826aa21
#
# [R2] Rooftop PV actual and forecast modelling:
#      AEMO Australian Solar Energy Forecasting System, ASEFS.
#      Produces solar forecasts for large solar and small-scale
#      distributed PV systems.
#      https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/nem-forecasting-and-planning/operational-forecasting/solar-and-wind-energy-forecasting/australian-solar-energy-forecasting-system
#
# [R3] Retail import tariff:
#      Essential Services Commission Victorian Default Offer 2025-26.
#      Domestic two-period TOU tariff uses peak 3 pm-9 pm and
#      off-peak all other times. The values here use a representative
#      CitiPower-style peak/off-peak model for the scenario.
#      https://www.esc.vic.gov.au/electricity-and-gas/prices-tariffs-and-benchmarks/victorian-default-offer
#
# [R4] Feed-in tariff / export value:
#      ESC minimum feed-in tariff review 2025-26 showed low daytime
#      export value and higher evening export value. This is used
#      to model the idea that midday export is not always valuable.
#      https://www.esc.vic.gov.au/electricity-and-gas/prices-tariffs-and-benchmarks/minimum-feed-tariff/minimum-feed-tariff-review-2025-26
#
# [R5] CO2 factor:
#      Australian National Greenhouse Accounts Factors.
#      Victoria grid electricity emissions are used as the benchmark
#      idea, with hourly representative values used for the scenario.
#      https://www.dcceew.gov.au/climate-change/publications/national-greenhouse-accounts-factors-2025
#
# [R6] Battery cost / degradation cost:
#      IEA battery cost trend and V2H battery degradation literature
#      are used to justify a simplified user battery-wear cost.
#      This is not a warranty model.
#      https://www.iea.org/reports/batteries-and-secure-energy-transitions
#
# ============================================================


# ============================================================
# RELAY SETUP
# ============================================================

RELAY_GPIO = 27
relay = OutputDevice(RELAY_GPIO, active_high=True, initial_value=False)


# ============================================================
# SCENARIO 2 HARSHIL FILE PATHS
# ============================================================

DATA_DIR = os.path.join("data", "scenario2_harshil_files")
LOG_DIR = os.path.join("logs", "scenario2_harshil_files")

ENERGY_PROFILE_FILE = os.path.join(DATA_DIR, "energy_profile.csv")
MARKET_NETWORK_FILE = os.path.join(DATA_DIR, "market_network_profile.csv")
BATTERY_PROFILE_FILE = os.path.join(DATA_DIR, "battery_profile.csv")

HOURLY_LOG_FILE = os.path.join(LOG_DIR, "hourly_log.csv")
SUMMARY_MATRIX_FILE = os.path.join(LOG_DIR, "summary_matrix.csv")
RULE_TRACE_FILE = os.path.join(LOG_DIR, "rule_trace.csv")
EVENT_LOG_FILE = os.path.join(LOG_DIR, "event_log.csv")
SOURCE_REFERENCE_FILE = os.path.join(LOG_DIR, "data_source_references.csv")


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

# Fuzzy output thresholds:
# Negative output means charge.
# Positive output means discharge.
CHARGE_SCORE_THRESHOLD = -45.0
DISCHARGE_SCORE_THRESHOLD = 55.0

# Daily discharge budget avoids unrealistic battery cycling.
DAILY_DISCHARGE_BUDGET_KWH = 16.0

# These are controller weighting values, not market prices.
CARBON_VALUE_C_PER_KG_CO2 = 10.0
GRID_STRESS_VALUE_C_PER_KWH = 20.0

# Simplified aging model assumptions for summary only.
BATTERY_EOL_RETAINED_CAPACITY_PERCENT = 75.0
ASSUMED_CYCLE_LIFE_EFC = 3000.0
BASE_CAPACITY_FADE_PERCENT_PER_EFC = (
    (100.0 - BATTERY_EOL_RETAINED_CAPACITY_PERCENT) / ASSUMED_CYCLE_LIFE_EFC
)

# Always overwrite demo CSVs so the intended scenario runs.
OVERWRITE_INPUT_CSVS = True


# ============================================================
# SOURCE REFERENCE SUMMARY
# ============================================================

SOURCE_REFERENCES = [
    {
        "item": "home_load_kw and pv_actual_kw profile shape",
        "source": "Ausgrid Solar Home Electricity Data via CSIRO NEAR",
        "reason": "Household consumption and rooftop PV profile shape",
        "url": "https://near.csiro.au/assets/42966a8f-bc3c-4bde-91d6-91bc5826aa21",
    },
    {
        "item": "pv_forecast_kw and forecast error concept",
        "source": "AEMO Australian Solar Energy Forecasting System",
        "reason": "Rooftop PV and solar forecasting basis",
        "url": "https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/nem-forecasting-and-planning/operational-forecasting/solar-and-wind-energy-forecasting/australian-solar-energy-forecasting-system",
    },
    {
        "item": "import_price_c_per_kwh",
        "source": "ESC Victorian Default Offer 2025-26",
        "reason": "Domestic two-period time-of-use retail tariff basis",
        "url": "https://www.esc.vic.gov.au/electricity-and-gas/prices-tariffs-and-benchmarks/victorian-default-offer",
    },
    {
        "item": "feed_in_price_c_per_kwh",
        "source": "ESC minimum feed-in tariff review 2025-26",
        "reason": "Low daytime export value and evening export value basis",
        "url": "https://www.esc.vic.gov.au/electricity-and-gas/prices-tariffs-and-benchmarks/minimum-feed-tariff/minimum-feed-tariff-review-2025-26",
    },
    {
        "item": "grid_co2_kg_per_kwh",
        "source": "Australian National Greenhouse Accounts Factors",
        "reason": "Victoria grid electricity emissions benchmark",
        "url": "https://www.dcceew.gov.au/climate-change/publications/national-greenhouse-accounts-factors-2025",
    },
    {
        "item": "battery_wear_cost and capacity fade estimate",
        "source": "IEA battery cost trend and V2H degradation literature",
        "reason": "Simplified user battery-wear cost and aging model",
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
            # hour, home_load_kw, pv_actual_kw, pv_forecast_kw,
            # ev_available, trip_reserve_soc, critical_load_level

            [0, 0.55, 0.00, 0.00, 1, 55, 0.20],
            [1, 0.48, 0.00, 0.00, 1, 55, 0.20],
            [2, 0.42, 0.00, 0.00, 1, 55, 0.20],
            [3, 0.40, 0.00, 0.00, 1, 55, 0.20],
            [4, 0.45, 0.00, 0.00, 1, 55, 0.20],
            [5, 0.65, 0.05, 0.10, 1, 55, 0.30],
            [6, 1.05, 0.25, 0.35, 1, 55, 0.40],

            # EV away from home during working hours.
            [7, 1.45, 0.65, 0.75, 0, 60, 0.50],
            [8, 1.30, 1.25, 1.40, 0, 60, 0.40],
            [9, 1.10, 2.10, 2.30, 0, 60, 0.30],
            [10, 1.25, 3.25, 3.40, 0, 60, 0.30],
            [11, 1.50, 3.85, 4.00, 0, 60, 0.40],
            [12, 1.70, 4.00, 4.10, 0, 60, 0.45],
            [13, 1.80, 3.55, 3.60, 0, 60, 0.50],

            # EV returns; fuzzy controller should see useful afternoon support.
            [14, 2.90, 1.35, 1.50, 1, 55, 0.70],
            [15, 3.10, 1.10, 1.20, 1, 55, 0.80],

            # PV surplus after first discharge; fuzzy controller should choose CHARGE.
            [16, 1.35, 2.45, 2.30, 1, 55, 0.30],

            # Evening peak; fuzzy controller should choose DISCHARGE.
            [17, 3.00, 0.55, 0.55, 1, 55, 0.80],
            [18, 3.60, 0.00, 0.00, 1, 55, 1.00],
            [19, 3.50, 0.00, 0.00, 1, 55, 1.00],
            [20, 2.90, 0.00, 0.00, 1, 55, 0.90],

            # After peak; fuzzy controller should normally hold.
            [21, 2.20, 0.00, 0.00, 1, 55, 0.70],
            [22, 1.40, 0.00, 0.00, 1, 55, 0.40],
            [23, 0.90, 0.00, 0.00, 1, 55, 0.30],
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

    if OVERWRITE_INPUT_CSVS or not os.path.exists(MARKET_NETWORK_FILE):
        market_rows = [
            # hour, import_price_c_per_kwh, feed_in_price_c_per_kwh,
            # export_limit_kw, grid_stress_level, grid_co2_kg_per_kwh

            [0, 22.06, 1.00, 5.0, 0.20, 0.72],
            [1, 22.06, 1.00, 5.0, 0.20, 0.72],
            [2, 22.06, 1.00, 5.0, 0.20, 0.71],
            [3, 22.06, 1.00, 5.0, 0.20, 0.70],
            [4, 22.06, 1.00, 5.0, 0.25, 0.70],
            [5, 22.06, 1.00, 5.0, 0.30, 0.69],
            [6, 22.06, 1.00, 5.0, 0.40, 0.65],

            [7, 22.06, 0.00, 4.0, 0.45, 0.58],
            [8, 22.06, 0.00, 3.0, 0.45, 0.50],
            [9, 22.06, 0.00, 2.0, 0.50, 0.42],
            [10, 22.06, 0.00, 1.5, 0.60, 0.34],
            [11, 22.06, 0.00, 1.0, 0.65, 0.28],
            [12, 22.06, 0.00, 0.8, 0.70, 0.25],
            [13, 22.06, 0.00, 0.8, 0.65, 0.30],

            # Afternoon stress starts rising.
            [14, 22.06, 0.00, 1.0, 0.85, 0.55],

            # Peak retail period.
            [15, 36.33, 6.57, 1.5, 0.90, 0.65],
            [16, 36.33, 6.57, 1.5, 0.75, 0.70],
            [17, 36.33, 6.57, 2.0, 0.88, 0.78],
            [18, 36.33, 6.57, 2.0, 0.95, 0.86],
            [19, 36.33, 6.57, 2.0, 0.95, 0.86],
            [20, 36.33, 6.57, 2.0, 0.90, 0.82],

            # After peak.
            [21, 22.06, 1.00, 4.0, 0.35, 0.65],
            [22, 22.06, 1.00, 5.0, 0.30, 0.70],
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
# FUZZY CONTROLLER
# ============================================================
#
# Inputs:
#   1. net_load
#   2. value_signal
#   3. soc_margin
#   4. grid_stress
#   5. battery_wear_stress
#
# Output:
#   fuzzy_action_score
#
#   Negative score  -> CHARGE
#   Near zero score -> HOLD
#   Positive score  -> DISCHARGE
#
# Simple final decisions:
#   HOLD
#   CHARGE
#   DISCHARGE
#
# ============================================================

def fuzzy_controller(row, soc, cycle_budget_remaining):
    features = calculate_features(row, soc, cycle_budget_remaining)

    net_load = features["net_load"]
    value_signal = features["value_signal_c_per_kwh"]
    soc_margin = features["soc_margin"]
    grid_stress = row["grid_stress_level"]
    battery_wear_stress = features["battery_wear_stress"]

    # -----------------------------
    # Hard safety checks
    # -----------------------------

    if row["ev_available"] == 0:
        return {
            "decision": "HOLD",
            "decision_reason": "EV not available",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_action_score": 0.0,
            "dominant_rule": "Hard rule: EV not available",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    if soc <= SOC_MIN:
        return {
            "decision": "HOLD",
            "decision_reason": "SOC minimum protection",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_action_score": 0.0,
            "dominant_rule": "Hard rule: SOC minimum protection",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    if soc_margin <= 0:
        return {
            "decision": "HOLD",
            "decision_reason": "Trip reserve protection",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_action_score": 0.0,
            "dominant_rule": "Hard rule: trip reserve protection",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    if cycle_budget_remaining <= 0:
        return {
            "decision": "HOLD",
            "decision_reason": "Daily discharge budget protection",
            "ev_power_kw": 0.0,
            "relay_on": False,
            "fuzzy_action_score": 0.0,
            "dominant_rule": "Hard rule: daily discharge budget exhausted",
            "dominant_strength": 1.0,
            "dominant_score": 0.0,
            "features": features,
            "levels": {},
        }

    # -----------------------------
    # Fuzzification
    # -----------------------------

    net_load_mf = {
        "surplus": trapezoid(net_load, -5.0, -5.0, -0.40, -0.05),
        "balanced": triangle(net_load, -0.40, 0.0, 0.40),
        "low_deficit": triangle(net_load, 0.10, 0.90, 1.80),
        "medium_deficit": triangle(net_load, 1.20, 2.50, 3.80),
        "high_deficit": trapezoid(net_load, 3.20, 4.00, 6.00, 6.00),
    }

    value_mf = {
        "low": trapezoid(value_signal, -50.0, -50.0, 8.0, 15.0),
        "medium": triangle(value_signal, 12.0, 26.0, 40.0),
        "high": trapezoid(value_signal, 32.0, 44.0, 90.0, 90.0),
    }

    soc_margin_mf = {
        "tight": trapezoid(soc_margin, -20.0, -20.0, 1.0, 4.0),
        "safe": triangle(soc_margin, 2.0, 12.0, 28.0),
        "high": trapezoid(soc_margin, 22.0, 34.0, 80.0, 80.0),
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
    #
    # Output score:
    #   -100 = strong charge
    #    -70 = normal charge
    #      0 = hold
    #    +60 = light discharge
    #    +80 = medium discharge
    #   +100 = strong discharge
    #
    # All decisions are made by fuzzy score.
    # -----------------------------

    rules = []

    # HOLD rules
    rules.append((
        max(
            net_load_mf["balanced"],
            value_mf["low"],
            soc_margin_mf["tight"],
            battery_wear_mf["high"],
        ),
        0.0,
        "Hold: balanced load, low value, tight SOC, or high battery wear",
    ))

    # CHARGE rules
    rules.append((
        min(
            net_load_mf["surplus"],
            max(soc_margin_mf["tight"], soc_margin_mf["safe"]),
            max(battery_wear_mf["low"], battery_wear_mf["medium"]),
        ),
        -90.0,
        "Charge: PV surplus and battery has usable room",
    ))

    rules.append((
        min(
            net_load_mf["surplus"],
            soc_margin_mf["high"],
            battery_wear_mf["low"],
        ),
        -55.0,
        "Charge: PV surplus top-up with low battery stress",
    ))

    # DISCHARGE rules
    rules.append((
        min(
            net_load_mf["low_deficit"],
            max(value_mf["medium"], value_mf["high"]),
            max(soc_margin_mf["safe"], soc_margin_mf["high"]),
            max(grid_stress_mf["medium"], grid_stress_mf["high"]),
            max(battery_wear_mf["low"], battery_wear_mf["medium"]),
        ),
        60.0,
        "Discharge: small deficit but useful grid support value",
    ))

    rules.append((
        min(
            net_load_mf["medium_deficit"],
            max(value_mf["medium"], value_mf["high"]),
            max(soc_margin_mf["safe"], soc_margin_mf["high"]),
            max(grid_stress_mf["medium"], grid_stress_mf["high"]),
            max(battery_wear_mf["low"], battery_wear_mf["medium"]),
        ),
        80.0,
        "Discharge: medium deficit with good value and acceptable wear",
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
        "Discharge: high deficit, high value, high grid stress",
    ))

    rules.append((
        min(
            grid_stress_mf["high"],
            max(net_load_mf["medium_deficit"], net_load_mf["high_deficit"]),
            max(value_mf["medium"], value_mf["high"]),
            soc_margin_mf["safe"],
        ),
        85.0,
        "Discharge: grid support while SOC is still safe",
    ))

    # -----------------------------
    # Defuzzification
    # -----------------------------

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

    if denominator == 0.0:
        fuzzy_action_score = 0.0
    else:
        fuzzy_action_score = numerator / denominator

    # -----------------------------
    # Final decision from fuzzy score
    # -----------------------------

    decision = "HOLD"
    decision_reason = "Fuzzy output near hold region"
    ev_power_kw = 0.0
    relay_on = False

    # CHARGE decision
    if (
        fuzzy_action_score <= CHARGE_SCORE_THRESHOLD
        and features["pv_surplus"] > 0.05
        and soc < SOC_MAX
    ):
        available_battery_room_kwh = ((SOC_MAX - soc) / 100.0) * EV_BATTERY_KWH

        charge_power = min(
            EV_MAX_CHARGE_KW,
            features["pv_surplus"],
            available_battery_room_kwh,
        )

        if charge_power > 0.05:
            decision = "CHARGE"
            decision_reason = "Fuzzy controller selected charging"
            ev_power_kw = -charge_power
            relay_on = False

    # DISCHARGE decision
    elif (
        fuzzy_action_score >= DISCHARGE_SCORE_THRESHOLD
        and net_load > 0.05
        and soc_margin > 0
    ):
        max_energy_allowed_by_soc_kwh = max(
            ((soc - row["trip_reserve_soc"]) / 100.0) * EV_BATTERY_KWH,
            0.0,
        )

        max_power_allowed_by_budget_kw = max(
            cycle_budget_remaining * DAILY_DISCHARGE_BUDGET_KWH,
            0.0,
        )

        if fuzzy_action_score >= 85.0:
            requested_power = min(EV_MAX_DISCHARGE_KW, net_load)
        elif fuzzy_action_score >= 70.0:
            requested_power = min(2.2, net_load)
        else:
            requested_power = min(1.4, net_load)

        discharge_power = min(
            requested_power,
            max_energy_allowed_by_soc_kwh,
            max_power_allowed_by_budget_kw,
        )

        if discharge_power > 0.05:
            decision = "DISCHARGE"
            decision_reason = "Fuzzy controller selected V2H discharge"
            ev_power_kw = discharge_power
            relay_on = True

    return {
        "decision": decision,
        "decision_reason": decision_reason,
        "ev_power_kw": ev_power_kw,
        "relay_on": relay_on,
        "fuzzy_action_score": fuzzy_action_score,
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
    with_controller_import_kwh = 0.0

    without_v2h_export_kwh = 0.0
    with_controller_export_kwh = 0.0

    without_v2h_curtailment_kwh = 0.0
    with_controller_curtailment_kwh = 0.0

    without_v2h_bill = 0.0
    with_controller_bill_before_wear = 0.0

    without_v2h_emissions = 0.0
    with_controller_emissions = 0.0

    without_v2h_peak_import_kw = 0.0
    with_controller_peak_import_kw = 0.0

    battery_wear_cost = 0.0

    discharge_energy_to_home_kwh = 0.0
    battery_energy_depleted_kwh = 0.0

    charge_input_kwh = 0.0
    charge_stored_kwh = 0.0

    afternoon_discharge_energy_kwh = 0.0
    evening_discharge_energy_kwh = 0.0

    battery_throughput_kwh = 0.0
    reserve_violations = 0

    for row in results:
        baseline_grid = row["without_v2h_grid_kw"]
        managed_grid = row["with_controller_grid_kw"]
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
        with_controller_import_kwh += managed_imp

        without_v2h_export_kwh += base_exp
        with_controller_export_kwh += managed_exp

        without_v2h_curtailment_kwh += base_curt
        with_controller_curtailment_kwh += managed_curt

        without_v2h_bill += (
            base_imp * (row["import_price_c_per_kwh"] / 100.0)
            - base_exp * (row["feed_in_price_c_per_kwh"] / 100.0)
        )

        with_controller_bill_before_wear += (
            managed_imp * (row["import_price_c_per_kwh"] / 100.0)
            - managed_exp * (row["feed_in_price_c_per_kwh"] / 100.0)
        )

        without_v2h_emissions += base_imp * row["grid_co2_kg_per_kwh"]
        with_controller_emissions += managed_imp * row["grid_co2_kg_per_kwh"]

        without_v2h_peak_import_kw = max(without_v2h_peak_import_kw, base_imp)
        with_controller_peak_import_kw = max(with_controller_peak_import_kw, managed_imp)

        if row["ev_power_kw"] > 0:
            discharge_energy_to_home_kwh += row["ev_power_kw"]

            battery_depleted = row["ev_power_kw"] / DISCHARGE_EFFICIENCY
            battery_energy_depleted_kwh += battery_depleted
            battery_throughput_kwh += battery_depleted

            battery_wear_cost += row["ev_power_kw"] * (
                row["battery_wear_cost_c_per_kwh"] / 100.0
            )

            if 14 <= row["hour"] <= 15:
                afternoon_discharge_energy_kwh += row["ev_power_kw"]

            if 17 <= row["hour"] <= 20:
                evening_discharge_energy_kwh += row["ev_power_kw"]

        if row["ev_power_kw"] < 0:
            input_energy = abs(row["ev_power_kw"])
            stored_energy = input_energy * CHARGE_EFFICIENCY

            charge_input_kwh += input_energy
            charge_stored_kwh += stored_energy
            battery_throughput_kwh += stored_energy

        if row["soc_after_percent"] < row["trip_reserve_soc"]:
            reserve_violations += 1

    with_controller_bill_after_wear = with_controller_bill_before_wear + battery_wear_cost

    gross_saving_before_wear = without_v2h_bill - with_controller_bill_before_wear
    net_saving_after_wear = without_v2h_bill - with_controller_bill_after_wear

    import_reduction_kwh = without_v2h_import_kwh - with_controller_import_kwh
    emissions_avoided_kg = without_v2h_emissions - with_controller_emissions

    peak_reduction_kw = without_v2h_peak_import_kw - with_controller_peak_import_kw

    if without_v2h_peak_import_kw > 0:
        peak_reduction_percent = (peak_reduction_kw / without_v2h_peak_import_kw) * 100.0
    else:
        peak_reduction_percent = 0.0

    curtailment_reduction_kwh = without_v2h_curtailment_kwh - with_controller_curtailment_kwh

    equivalent_full_cycles_throughput = battery_throughput_kwh / (2.0 * EV_BATTERY_KWH)
    equivalent_full_cycles_discharge_only = battery_energy_depleted_kwh / EV_BATTERY_KWH

    avg_battery_temp = sum(row["battery_temp_c"] for row in results) / len(results)
    temperature_multiplier = 1.0 + max(avg_battery_temp - 25.0, 0.0) * 0.02

    estimated_capacity_fade_percent = (
        equivalent_full_cycles_throughput
        * BASE_CAPACITY_FADE_PERCENT_PER_EFC
        * temperature_multiplier
    )

    active_rows = [row for row in results if row["decision"] == "DISCHARGE"]
    active_scores = [row["fuzzy_action_score"] for row in active_rows]

    if active_scores:
        average_discharge_score = sum(active_scores) / len(active_scores)
        maximum_discharge_score = max(active_scores)
    else:
        average_discharge_score = 0.0
        maximum_discharge_score = 0.0

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

    add("SCENARIO 2 HARSHIL OVERVIEW", "Scenario name", "Scenario 2: Harshil's Scenario", "-", "Fuzzy controller chooses HOLD, CHARGE, or DISCHARGE")
    add("SCENARIO 2 HARSHIL OVERVIEW", "Data/log folder name", "scenario2_harshil_files", "-", "All Scenario 2 Harshil files are saved here")
    add("SCENARIO 2 HARSHIL OVERVIEW", "Relay rule", "Relay ON only for DISCHARGE", "-", "CHARGE and HOLD keep relay OFF")

    add("FUZZY CONTROLLER", "Number of fuzzy inputs", 5, "inputs", "net load, value signal, SOC margin, grid stress, battery wear")
    add("FUZZY CONTROLLER", "Output decisions", "HOLD / CHARGE / DISCHARGE", "-", "Final operation is selected from fuzzy action score")
    add("FUZZY CONTROLLER", "Average discharge score", round(average_discharge_score, 2), "score", "Average fuzzy score during DISCHARGE hours")
    add("FUZZY CONTROLLER", "Maximum discharge score", round(maximum_discharge_score, 2), "score", "Highest fuzzy discharge score")
    add("FUZZY CONTROLLER", "Charge score threshold", CHARGE_SCORE_THRESHOLD, "score", "Score below this selects CHARGE")
    add("FUZZY CONTROLLER", "Discharge score threshold", DISCHARGE_SCORE_THRESHOLD, "score", "Score above this selects DISCHARGE")

    add("OPERATION LOG", "HOLD hours", hours_list(results, lambda r: r["decision"] == "HOLD"), "-", "Fuzzy controller selected hold")
    add("OPERATION LOG", "CHARGE hours", hours_list(results, lambda r: r["decision"] == "CHARGE"), "-", "Fuzzy controller selected charge")
    add("OPERATION LOG", "DISCHARGE hours", hours_list(results, lambda r: r["decision"] == "DISCHARGE"), "-", "Fuzzy controller selected V2H discharge")
    add("OPERATION LOG", "Relay ON periods", hours_list(results, lambda r: r["relay_state"] == "ON"), "-", "Physical relay/lamp active only during DISCHARGE")

    add("ENERGY BALANCE", "Total home load", round(total_home_load_kwh, 2), "kWh", "Daily household load")
    add("ENERGY BALANCE", "Total PV generation", round(total_pv_kwh, 2), "kWh", "Daily rooftop PV generation")
    add("ENERGY BALANCE", "Discharge energy to home", round(discharge_energy_to_home_kwh, 2), "kWh", "Useful V2H energy supplied")
    add("ENERGY BALANCE", "Afternoon discharge energy", round(afternoon_discharge_energy_kwh, 2), "kWh", "Discharge energy during 14:00-16:00")
    add("ENERGY BALANCE", "Evening discharge energy", round(evening_discharge_energy_kwh, 2), "kWh", "Discharge energy during 17:00-21:00")
    add("ENERGY BALANCE", "Charge input energy", round(charge_input_kwh, 2), "kWh", "Energy used for charging")
    add("ENERGY BALANCE", "Stored charge energy", round(charge_stored_kwh, 2), "kWh", "Energy added to battery after efficiency")

    add("WITH V2H vs WITHOUT V2H", "Grid import without V2H", round(without_v2h_import_kwh, 2), "kWh", "Baseline if EV is not used")
    add("WITH V2H vs WITHOUT V2H", "Grid import with controller", round(with_controller_import_kwh, 2), "kWh", "Managed result with fuzzy decisions")
    add("WITH V2H vs WITHOUT V2H", "Grid import reduction", round(import_reduction_kwh, 2), "kWh", "Import reduction from controller")
    add("WITH V2H vs WITHOUT V2H", "Peak import without V2H", round(without_v2h_peak_import_kw, 2), "kW", "Highest baseline import")
    add("WITH V2H vs WITHOUT V2H", "Peak import with controller", round(with_controller_peak_import_kw, 2), "kW", "Highest managed import")
    add("WITH V2H vs WITHOUT V2H", "Peak import reduction", round(peak_reduction_kw, 2), "kW", "Peak shaving result")
    add("WITH V2H vs WITHOUT V2H", "Peak import reduction", round(peak_reduction_percent, 1), "%", "Percentage peak reduction")

    add("COST COMPARISON", "Bill without V2H", round(without_v2h_bill, 2), "$", "Baseline daily electricity cost")
    add("COST COMPARISON", "Bill with controller before wear", round(with_controller_bill_before_wear, 2), "$", "Managed bill before battery wear")
    add("COST COMPARISON", "Battery wear cost", round(battery_wear_cost, 2), "$", "Estimated user battery degradation cost")
    add("COST COMPARISON", "Bill with controller after wear", round(with_controller_bill_after_wear, 2), "$", "Managed bill including wear cost")
    add("COST COMPARISON", "Gross saving before wear", round(gross_saving_before_wear, 2), "$", "Saving before battery wear cost")
    add("COST COMPARISON", "Net saving after wear", round(net_saving_after_wear, 2), "$", "Saving after battery wear cost")

    add("CO2 COMPARISON", "CO2 without V2H", round(without_v2h_emissions, 2), "kg CO2-e", "Baseline grid import emissions")
    add("CO2 COMPARISON", "CO2 with controller", round(with_controller_emissions, 2), "kg CO2-e", "Managed grid import emissions")
    add("CO2 COMPARISON", "CO2 avoided", round(emissions_avoided_kg, 2), "kg CO2-e", "Emission reduction from reduced grid import")

    add("PV EXPORT / CURTAILMENT", "PV export without V2H", round(without_v2h_export_kwh, 2), "kWh", "Baseline exported PV")
    add("PV EXPORT / CURTAILMENT", "PV export with controller", round(with_controller_export_kwh, 2), "kWh", "Managed exported PV")
    add("PV EXPORT / CURTAILMENT", "Curtailment without V2H", round(without_v2h_curtailment_kwh, 2), "kWh", "Baseline export-limit curtailment")
    add("PV EXPORT / CURTAILMENT", "Curtailment with controller", round(with_controller_curtailment_kwh, 2), "kWh", "Managed export-limit curtailment")
    add("PV EXPORT / CURTAILMENT", "Curtailment reduction", round(curtailment_reduction_kwh, 2), "kWh", "PV curtailment avoided")

    add("BATTERY SOC & AGING", "Initial SOC", round(INITIAL_SOC, 2), "%", "SOC at start of day")
    add("BATTERY SOC & AGING", "Final SOC", round(final_soc, 2), "%", "SOC at end of day")
    add("BATTERY SOC & AGING", "Minimum SOC", round(min_soc, 2), "%", "Lowest SOC reached")
    add("BATTERY SOC & AGING", "Final SOC margin above reserve", round(final_reserve_margin, 2), "%", "Final SOC minus user trip reserve")
    add("BATTERY SOC & AGING", "Next-day commute ready", next_day_ready, "-", "Yes if final SOC remains above reserve")
    add("BATTERY SOC & AGING", "Reserve violation hours", reserve_violations, "hours", "Hours where SOC dropped below trip reserve")
    add("BATTERY SOC & AGING", "Battery throughput", round(battery_throughput_kwh, 2), "kWh", "Charge/discharge throughput used for aging estimate")
    add("BATTERY SOC & AGING", "EFC throughput estimate", round(equivalent_full_cycles_throughput, 4), "EFC", "Equivalent full cycles from throughput")
    add("BATTERY SOC & AGING", "EFC discharge-only estimate", round(equivalent_full_cycles_discharge_only, 4), "EFC", "Equivalent full cycles from discharge energy")
    add("BATTERY SOC & AGING", "Estimated capacity fade", round(estimated_capacity_fade_percent, 5), "%", "Simplified one-day aging estimate")

    for decision, count in decision_counts.items():
        add("DECISION COUNTS", decision, count, "hours", "Number of hours this final decision occurred")

    return matrix


def print_summary_matrix(matrix):
    print("\n================ SCENARIO 2: HARSHIL'S SUMMARY MATRIX ================")

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

    print("\n======================================================================")


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
        "decision_reason",
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
    print(" SCENARIO 2: HARSHIL'S SCENARIO STARTED")
    print(" Fuzzy controller decides: HOLD / CHARGE / DISCHARGE")
    print(" Relay ON  = DISCHARGE")
    print(" Relay OFF = HOLD or CHARGE")
    print(" 24 simulated hours = 5 real minutes")
    print("===================================================")

    try:
        for row in input_rows:
            hour = row["hour"]
            soc_before = ev_soc

            controller_output = fuzzy_controller(
                row,
                ev_soc,
                cycle_budget_remaining,
            )

            decision = controller_output["decision"]
            decision_reason = controller_output["decision_reason"]
            ev_power = controller_output["ev_power_kw"]
            relay_on = controller_output["relay_on"]
            fuzzy_action_score = controller_output["fuzzy_action_score"]
            dominant_rule = controller_output["dominant_rule"]
            dominant_strength = controller_output.get("dominant_strength", 0.0)
            dominant_score = controller_output.get("dominant_score", 0.0)
            features = controller_output["features"]
            levels = controller_output["levels"]

            without_v2h_grid = features["net_load"]
            with_controller_grid = without_v2h_grid - ev_power

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
                f"Load={row['home_load_kw']:4.2f} kW | "
                f"PV={row['pv_actual_kw']:4.2f} kW | "
                f"NoV2HGrid={without_v2h_grid:5.2f} kW | "
                f"ManagedGrid={with_controller_grid:5.2f} kW | "
                f"SOC={soc_before:5.1f}%->{soc_after:5.1f}% | "
                f"Value={features['value_signal_c_per_kwh']:5.1f}c | "
                f"Wear={features['battery_wear_stress']:4.2f} | "
                f"Fuzzy={fuzzy_action_score:6.1f} | "
                f"EV={ev_power:5.2f} kW | "
                f"Relay={relay_state:3s} | "
                f"{decision}"
            )

            hourly_row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,

                "home_load_kw": row["home_load_kw"],
                "pv_actual_kw": row["pv_actual_kw"],
                "pv_forecast_kw": row["pv_forecast_kw"],

                "without_v2h_grid_kw": round(without_v2h_grid, 3),
                "with_controller_grid_kw": round(with_controller_grid, 3),

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

                "fuzzy_action_score": round(fuzzy_action_score, 2),
                "dominant_rule": dominant_rule,
                "dominant_strength": round(dominant_strength, 3),
                "dominant_score": round(dominant_score, 2),

                "decision": decision,
                "decision_reason": decision_reason,
                "ev_power_kw": round(ev_power, 3),
                "relay_state": relay_state,
            }

            results.append(hourly_row)

            rule_trace.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "hour": hour,

                "net_load_level": hourly_row["net_load_level"],
                "value_signal_level": hourly_row["value_signal_level"],
                "soc_margin_level": hourly_row["soc_margin_level"],
                "grid_stress_level_fuzzy": hourly_row["grid_stress_level_fuzzy"],
                "battery_wear_level": hourly_row["battery_wear_level"],

                "dominant_rule": dominant_rule,
                "dominant_strength": round(dominant_strength, 3),
                "dominant_score": round(dominant_score, 2),
                "fuzzy_action_score": round(fuzzy_action_score, 2),

                "decision": decision,
                "decision_reason": decision_reason,
                "relay_state": relay_state,
            })

            if decision != previous_decision or relay_state != previous_relay_state:
                event_log.append({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "hour": hour,
                    "event": "STATE_CHANGE",
                    "decision": decision,
                    "decision_reason": decision_reason,
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

            print(f"\nScenario 2 Harshil files saved to: {LOG_DIR}/")
            print(f"Scenario 2 Harshil data files saved to: {DATA_DIR}/")
        else:
            print("No results recorded.")

        print("Scenario 2: Harshil's Scenario complete.")


if __name__ == "__main__":
    main()