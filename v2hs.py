from gpiozero import OutputDevice
from time import sleep
from datetime import datetime
import csv
import os

# ============================================================
# SCENARIO 4 — TOU-AWARE PEAK DEMAND MITIGATION
#              WITH ROOFTOP PV + V2H + MAMDANI FUZZY LOGIC
#
# Hardware : Relay IN2 → GPIO27 / physical pin 13  (UNCHANGED)
#            Relay ON  = EV discharges → supports home load
#            Relay OFF = no V2H discharge
#
# Data     : AusNet VIC residential autumn weekday profile
#            • Load  : Ausgrid Open Data 2013-14, scaled to
#                      VIC 2024 climate + space heating
#            • PV    : 6.6 kW system, north-facing, 15° tilt,
#                      Melbourne lat -37.8°, PVWATTS autumn day
#            • Tariff: AusNet Services Schedule of Network
#                      Tariffs 2023-24 (Residential TOU)
#            • EV    : AEMO EV Uptake Model 2023 commuter
#                      profile, 62 kWh Nissan Leaf equivalent
#
# Control  : Mamdani Fuzzy Inference System
#            Inputs  : excess_demand_norm (how far grid exceeds
#                      the AusNet peak limit)
#                      soc_margin_norm    (usable SOC above
#                      the mobility reserve)
#                      tariff_level_norm  (TOU price signal)
#            Output  : discharge_level    (0 = hold, 1 = max)
#            Rules   : 9 Mamdani min-max rules
#            Defuzz  : Centroid (centre of gravity)
# ============================================================

relay = OutputDevice(27, active_high=True, initial_value=False)

# ── Battery constants ─────────────────────────────────────────
EV_CAPACITY_KWH  = 62.0    # Nissan Leaf 62 kWh (2023 model)
EV_MAX_POWER_KW  = 3.3     # SAE J1772 Level 2 bidirectional
SOC_MIN_PCT      = 20.0    # Hard floor — relay never closes below
SOC_RESERVE_PCT  = 35.0    # Mobility reserve — fuzzy protects this
FUZZY_RELAY_THRESHOLD = 0.25  # Min fuzzy output to close relay

HOUR_DELAY_SEC   = 12.5    # Compressed sim (1 hour = 12.5 s)

# ── AusNet VIC Residential TOU Tariff 2023-24 ────────────────
# Source: AusNet Services — Schedule of Network Tariffs 2023-24
#   Peak     16:00–21:59  $0.4752/kWh
#   Shoulder 07:00–15:59 & 22:00–23:59  $0.3021/kWh
#   Off-peak 00:00–06:59  $0.1716/kWh
TARIFF_PEAK      = 0.4752
TARIFF_SHOULDER  = 0.3021
TARIFF_OFFPEAK   = 0.1716

# ── AusNet per-household managed grid import targets ─────────
# AusNet Demand Management pilot: residential connection target
# during peak = 1.5 kW; normal operating limit = 3.0 kW
GRID_LIMIT_PEAK_KW   = 1.5
GRID_LIMIT_NORMAL_KW = 3.0

# ── AusNet VIC residential autumn weekday load profile (kW) ──
# Basis: Ausgrid Open Data 2013-14 smart meter medians,
# scaled to VIC autumn 2024 (space heating, typical 3-bed home)
# Profile characteristics:
#   • Overnight base ~0.4–0.5 kW (fridge, standby, hot water)
#   • Morning ramp 06:00–08:00 (shower, breakfast, heating)
#   • Midday trough 09:00–11:00 (occupants at work/school)
#   • Afternoon rise from 14:00 (return home, heating)
#   • Evening peak 17:00–20:00 (cooking, heating, TV, EV)
home_load_kw = [
    0.52, 0.44, 0.39, 0.36, 0.40, 0.68,   # 00–05 overnight
    1.12, 1.48, 1.25, 1.02, 0.90, 0.85,   # 06–11 morning
    0.92, 1.15, 1.48, 2.05, 3.10, 4.20,   # 12–17 afternoon
    4.80, 4.35, 3.72, 2.85, 1.65, 0.92    # 18–23 evening peak
]

# ── Rooftop PV generation profile (kW) ───────────────────────
# Basis: NREL PVWATTS v8 — Melbourne (lat -37.8°), autumn equinox
# System: 6.6 kW nameplate, north-facing, 15° tilt, 14% losses
# Cell temp model: Tc = Ta + GHI*(NOCT-20)/800, γ = -0.0035/°C
# Peak irradiance ~850 W/m² at solar noon (autumn Melbourne)
pv_generation_kw = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.03,   # 00–05 no sun
    0.22, 0.85, 1.75, 2.65, 3.30, 3.72,   # 06–11 morning ramp
    3.90, 3.55, 2.62, 1.50, 0.58, 0.08,   # 12–17 afternoon decline
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00    # 18–23 no sun
]

# ── EV availability profile ───────────────────────────────────
# Basis: AEMO EV Uptake Model 2023 — typical Melbourne commuter
# Departs ~07:00, returns ~14:00, plugged in for evening peak.
# SOC on return: ~78% (35 km commute, ~7 kWh from 62 kWh = 11%)
ev_available = [
    1, 1, 1, 1, 1, 1,   # 00–05 home overnight
    0, 0, 0, 0, 0, 0,   # 06–11 away (commute + work)
    0, 0, 1, 1, 1, 1,   # 12–17 returns 14:00, plugged in
    1, 1, 1, 1, 1, 1    # 18–23 home all evening
]

EV_SOC_ON_RETURN = 78.0   # % SOC when EV plugs in at 14:00
ev_soc     = EV_SOC_ON_RETURN
initial_soc = ev_soc

hours = list(range(24))


# ════════════════════════════════════════════════════════════
#  MAMDANI FUZZY INFERENCE SYSTEM
# ════════════════════════════════════════════════════════════
#
#  Three linguistic inputs, one linguistic output.
#  All MFs are triangular or trapezoidal — standard Mamdani form.
#
#  Input 1 — excess_demand_norm  [0, 1]
#    How far grid import exceeds the AusNet peak limit (1.5 kW),
#    normalised to EV_MAX_POWER_KW (3.3 kW) = full scale.
#    0 = at or below limit;  1 = excess ≥ 3.3 kW
#
#  Input 2 — soc_margin_norm  [0, 1]
#    Available SOC above the mobility reserve (35%), normalised
#    to the full usable range (35%–100% = 65 pp).
#    0 = at reserve;  1 = fully charged
#
#  Input 3 — tariff_level_norm  [0, 1]
#    Normalised TOU price: 0 = off-peak ($0.1716),  1 = peak ($0.4752)
#
#  Output — discharge_level  [0, 1]
#    Fraction of available EV power headroom to dispatch.
#    0 = no discharge;  1 = maximum safe discharge
#
# ════════════════════════════════════════════════════════════

def _trimf(x, a, b, c):
    """Triangular MF — zero outside [a,c], peak at b."""
    if x <= a or x >= c:
        return 0.0
    if x <= b:
        return (x - a) / (b - a) if b > a else 1.0
    return (c - x) / (c - b) if c > b else 1.0

def _trapmf(x, a, b, c, d):
    """Trapezoidal MF — zero outside [a,d], flat top [b,c].
    Inclusive at both endpoints so x=0 and x=1 score correctly."""
    if x < a or x > d:
        return 0.0
    if b <= x <= c:
        return 1.0
    if x < b:
        return (x - a) / (b - a) if b > a else 1.0
    return (d - x) / (d - c) if d > c else 1.0

# Input MFs
def _mf_excess(x):
    return {
        "low":    _trapmf(x, 0.00, 0.00, 0.12, 0.30),
        "medium": _trimf (x, 0.18, 0.42, 0.66),
        "high":   _trapmf(x, 0.52, 0.72, 1.00, 1.00),
    }

def _mf_soc(x):
    return {
        "low":    _trapmf(x, 0.00, 0.00, 0.18, 0.38),
        "medium": _trimf (x, 0.25, 0.50, 0.75),
        "high":   _trapmf(x, 0.62, 0.80, 1.00, 1.00),
    }

def _mf_tariff(x):
    return {
        "offpeak":  _trapmf(x, 0.00, 0.00, 0.18, 0.38),
        "shoulder": _trimf (x, 0.28, 0.52, 0.76),
        "peak":     _trapmf(x, 0.62, 0.80, 1.00, 1.00),
    }

# Output universe (101 points, 0.00 → 1.00)
_U = [i / 100.0 for i in range(101)]

def _mf_discharge(x):
    return {
        "none":   _trapmf(x, 0.00, 0.00, 0.05, 0.22),
        "low":    _trimf (x, 0.10, 0.28, 0.46),
        "medium": _trimf (x, 0.36, 0.55, 0.74),
        "high":   _trapmf(x, 0.62, 0.80, 1.00, 1.00),
    }

# ── 9-rule Mamdani rule base ──────────────────────────────────
#
#  R1  excess=low                             → none
#      (no excess demand, nothing to mitigate)
#
#  R2  excess=medium  soc=low                 → low
#      (some excess but battery nearly depleted → cautious)
#
#  R3  excess=medium  soc=medium  t=offpeak   → low
#      (moderate excess off-peak, cheap grid → prefer grid)
#
#  R4  excess=medium  soc=medium  t=shoulder  → medium
#      (moderate excess at shoulder price → moderate discharge)
#
#  R5  excess=medium  soc=medium  t=peak      → medium
#      (moderate excess at peak price → moderate discharge)
#
#  R6  excess=medium  soc=high    t=peak      → high
#      (moderate excess, battery full, expensive grid → strong)
#
#  R7  excess=high    soc=low                 → low
#      (large excess but battery low → protect SOC first)
#
#  R8  excess=high    soc=medium  t=peak      → high
#      (large excess at peak price, adequate SOC → maximum)
#
#  R9  excess=high    soc=high    t=peak      → high
#      (large excess, full battery, peak price → maximum)
#
def mamdani_fis(excess_n, soc_n, tariff_n):
    """
    Mamdani min-max fuzzy inference with centroid defuzzification.
    Returns crisp discharge_level in [0, 1].
    """
    e = _mf_excess(excess_n)
    s = _mf_soc(soc_n)
    t = _mf_tariff(tariff_n)

    rules = [
        (e["low"],                                   "none"),    # R1
        (min(e["medium"], s["low"]),                 "low"),     # R2
        (min(e["medium"], s["medium"], t["offpeak"]),"low"),     # R3
        (min(e["medium"], s["medium"], t["shoulder"]),"medium"), # R4
        (min(e["medium"], s["medium"], t["peak"]),   "medium"),  # R5
        (min(e["medium"], s["high"],   t["peak"]),   "high"),    # R6
        (min(e["high"],   s["low"]),                 "low"),     # R7
        (min(e["high"],   s["medium"], t["peak"]),   "high"),    # R8
        (min(e["high"],   s["high"],   t["peak"]),   "high"),    # R9
    ]

    # Aggregate: for each output label keep the max clipped curve
    agg = {lbl: [0.0] * len(_U) for lbl in ["none","low","medium","high"]}
    for strength, lbl in rules:
        if strength > 1e-9:
            for i, x in enumerate(_U):
                clipped = min(strength, _mf_discharge(x)[lbl])
                if clipped > agg[lbl][i]:
                    agg[lbl][i] = clipped

    # Union across labels
    combined = [max(agg[lbl][i] for lbl in agg) for i in range(len(_U))]

    # Centroid defuzzification
    num = sum(_U[i] * combined[i] for i in range(len(_U)))
    den = sum(combined)
    return (num / den) if den > 1e-9 else 0.0


# ════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ════════════════════════════════════════════════════════════

def is_peak(hour):
    return 16 <= hour <= 21

def get_tariff(hour):
    if 16 <= hour <= 21:  return TARIFF_PEAK
    if (7 <= hour <= 15) or (22 <= hour <= 23): return TARIFF_SHOULDER
    return TARIFF_OFFPEAK

def get_grid_limit(hour):
    return GRID_LIMIT_PEAK_KW if is_peak(hour) else GRID_LIMIT_NORMAL_KW

def norm_excess(excess_kw):
    """Normalise excess above grid limit to [0, 1]."""
    return min(max(excess_kw / EV_MAX_POWER_KW, 0.0), 1.0)

def norm_soc(soc_pct):
    """Normalise usable SOC above reserve to [0, 1]."""
    margin = max(soc_pct - SOC_RESERVE_PCT, 0.0)
    return min(margin / (100.0 - SOC_RESERVE_PCT), 1.0)

def norm_tariff(hour):
    """Normalise TOU tariff to [0, 1]."""
    r = get_tariff(hour)
    return (r - TARIFF_OFFPEAK) / (TARIFF_PEAK - TARIFF_OFFPEAK)

def update_soc(soc, ev_kw):
    drop = (ev_kw / EV_CAPACITY_KWH) * 100.0
    return max(soc - drop, SOC_MIN_PCT)

def fmt_hours(hlist):
    return ", ".join(f"{h:02d}:00" for h in hlist) if hlist else "None"


# ════════════════════════════════════════════════════════════
#  FUZZY PEAK DEMAND CONTROLLER
# ════════════════════════════════════════════════════════════

def fuzzy_controller(hour, load, pv, soc, available):
    pv_to_home = min(load, pv)
    pv_surplus = max(pv - load, 0.0)
    grid_before = max(load - pv, 0.0)
    grid_limit  = get_grid_limit(hour)
    excess      = max(grid_before - grid_limit, 0.0)

    # Hard safety guards evaluated BEFORE fuzzy engine
    if available == 0:
        return dict(decision="EV_NOT_AVAILABLE", ev_kw=0.0, relay=False,
                    grid_after=grid_before, fuzzy_raw=0.0,
                    excess_n=0.0, soc_n=0.0, tariff_n=0.0,
                    pv_to_home=pv_to_home, pv_surplus=pv_surplus,
                    grid_before=grid_before, grid_limit=grid_limit, excess=excess)

    if soc <= SOC_MIN_PCT:
        return dict(decision="HARD_SOC_FLOOR", ev_kw=0.0, relay=False,
                    grid_after=grid_before, fuzzy_raw=0.0,
                    excess_n=0.0, soc_n=0.0, tariff_n=0.0,
                    pv_to_home=pv_to_home, pv_surplus=pv_surplus,
                    grid_before=grid_before, grid_limit=grid_limit, excess=excess)

    if soc <= SOC_RESERVE_PCT:
        return dict(decision="SOC_RESERVE_PROTECTION", ev_kw=0.0, relay=False,
                    grid_after=grid_before, fuzzy_raw=0.0,
                    excess_n=0.0, soc_n=0.0, tariff_n=0.0,
                    pv_to_home=pv_to_home, pv_surplus=pv_surplus,
                    grid_before=grid_before, grid_limit=grid_limit, excess=excess)

    # No excess and not in peak window → no action needed
    if excess == 0.0 and not is_peak(hour):
        return dict(decision="HOLD", ev_kw=0.0, relay=False,
                    grid_after=grid_before, fuzzy_raw=0.0,
                    excess_n=0.0, soc_n=norm_soc(soc), tariff_n=norm_tariff(hour),
                    pv_to_home=pv_to_home, pv_surplus=pv_surplus,
                    grid_before=grid_before, grid_limit=grid_limit, excess=excess)

    # ── Run Mamdani FIS ──────────────────────────────────────
    en = norm_excess(excess)
    sn = norm_soc(soc)
    tn = norm_tariff(hour)
    fuzzy_out = mamdani_fis(en, sn, tn)

    if fuzzy_out < FUZZY_RELAY_THRESHOLD:
        return dict(decision="FUZZY_HOLD", ev_kw=0.0, relay=False,
                    grid_after=grid_before, fuzzy_raw=fuzzy_out,
                    excess_n=en, soc_n=sn, tariff_n=tn,
                    pv_to_home=pv_to_home, pv_surplus=pv_surplus,
                    grid_before=grid_before, grid_limit=grid_limit, excess=excess)

    # Map fuzzy output → EV dispatch power
    # headroom = what the EV needs to supply to bring grid to limit
    headroom = excess if excess > 0 else max(grid_before - GRID_LIMIT_PEAK_KW, 0.0)
    ev_kw    = min(fuzzy_out * EV_MAX_POWER_KW, headroom, EV_MAX_POWER_KW)
    ev_kw    = round(ev_kw, 3)
    grid_after = round(max(grid_before - ev_kw, 0.0), 3)

    if   fuzzy_out >= 0.72: decision = "FUZZY_HIGH_DISCHARGE"
    elif fuzzy_out >= 0.46: decision = "FUZZY_MEDIUM_DISCHARGE"
    else:                   decision = "FUZZY_LOW_DISCHARGE"

    return dict(decision=decision, ev_kw=ev_kw, relay=True,
                grid_after=grid_after, fuzzy_raw=fuzzy_out,
                excess_n=en, soc_n=sn, tariff_n=tn,
                pv_to_home=pv_to_home, pv_surplus=pv_surplus,
                grid_before=grid_before, grid_limit=grid_limit, excess=excess)


# ════════════════════════════════════════════════════════════
#  SUMMARY MATRIX BUILDER
# ════════════════════════════════════════════════════════════

def build_summary(results):
    def peak_rows(): return [r for r in results if is_peak(r["hour"])]
    def relay_rows(): return [r for r in results if r["relay_state"] == "ON"]

    # ── Energy flows ─────────────────────────────────────────
    total_load       = sum(r["home_load_kw"] for r in results)
    total_pv         = sum(r["pv_generation_kw"] for r in results)
    total_pv_used    = sum(r["pv_to_home_kw"] for r in results)
    total_pv_surplus = sum(r["pv_surplus_kw"] for r in results)
    pv_sc_pct        = total_pv_used / total_pv * 100 if total_pv > 0 else 0

    # ── WITHOUT V2H baseline (grid = load - PV, no EV) ───────
    # These represent what the household would have experienced
    # with no V2H at all — grid absorbs everything PV doesn't cover
    baseline_grid_hourly  = [r["grid_before_kw"] for r in results]
    baseline_total_grid   = sum(baseline_grid_hourly)
    baseline_peak_kw      = max(baseline_grid_hourly)
    baseline_peak_hour    = results[baseline_grid_hourly.index(baseline_peak_kw)]["hour"]
    baseline_peak_energy  = sum(r["grid_before_kw"] for r in peak_rows())
    baseline_cost_total   = sum(r["baseline_cost_aud"] for r in results)
    baseline_peak_cost    = sum(r["baseline_cost_aud"] for r in peak_rows())

    # ── WITH V2H (managed) ───────────────────────────────────
    managed_grid_hourly   = [r["grid_after_kw"] for r in results]
    managed_total_grid    = sum(managed_grid_hourly)
    managed_peak_kw       = max(managed_grid_hourly)
    managed_peak_energy   = sum(r["grid_after_kw"] for r in peak_rows())
    managed_cost_total    = sum(r["managed_cost_aud"] for r in results)
    managed_peak_cost     = sum(r["managed_cost_aud"] for r in peak_rows())

    # ── Reductions ───────────────────────────────────────────
    grid_red_kwh  = baseline_total_grid - managed_total_grid
    grid_red_pct  = grid_red_kwh / baseline_total_grid * 100 if baseline_total_grid > 0 else 0
    peak_kw_red   = baseline_peak_kw - managed_peak_kw
    peak_kw_pct   = peak_kw_red / baseline_peak_kw * 100 if baseline_peak_kw > 0 else 0
    pk_e_red      = baseline_peak_energy - managed_peak_energy
    pk_e_pct      = pk_e_red / baseline_peak_energy * 100 if baseline_peak_energy > 0 else 0
    cost_saving   = baseline_cost_total - managed_cost_total
    cost_pct      = cost_saving / baseline_cost_total * 100 if baseline_cost_total > 0 else 0
    pk_cost_save  = baseline_peak_cost - managed_peak_cost
    pk_cost_pct   = pk_cost_save / baseline_peak_cost * 100 if baseline_peak_cost > 0 else 0

    # ── EV / SOC ─────────────────────────────────────────────
    total_ev_dis   = sum(r["ev_power_kw"] for r in results)
    peak_ev_dis    = sum(r["ev_power_kw"] for r in peak_rows())
    offpeak_ev_dis = total_ev_dis - peak_ev_dis
    final_soc      = results[-1]["soc_after_pct"]
    min_soc        = min(r["soc_after_pct"] for r in results)
    soc_drop       = initial_soc - final_soc
    soc_margin_end = final_soc - SOC_RESERVE_PCT

    # ── Fuzzy controller analytics ────────────────────────────
    active = [r for r in results if r["fuzzy_score"] > 0]
    avg_fuzzy = sum(r["fuzzy_score"] for r in active) / len(active) if active else 0
    high_d  = [r["hour"] for r in results if r["decision"] == "FUZZY_HIGH_DISCHARGE"]
    med_d   = [r["hour"] for r in results if r["decision"] == "FUZZY_MEDIUM_DISCHARGE"]
    low_d   = [r["hour"] for r in results if r["decision"] == "FUZZY_LOW_DISCHARGE"]

    # ── Relay and stress ─────────────────────────────────────
    relay_on_hrs    = [r["hour"] for r in relay_rows()]
    pk_support_hrs  = [r["hour"] for r in peak_rows() if r["relay_state"] == "ON"]
    stress_before   = [r["hour"] for r in results if r["grid_before_kw"] > r["grid_limit_kw"]]
    stress_after    = [r["hour"] for r in results if r["grid_after_kw"]  > r["grid_limit_kw"]]
    low_soc_hrs     = [r["hour"] for r in results if "SOC" in r["decision"]]
    unavail_hrs     = [r["hour"] for r in results
                       if r["decision"] == "EV_NOT_AVAILABLE" and r["excess_kw"] > 0]

    # ── Why V2H? evening grid stress without it ───────────────
    # Hours where, without V2H, a household would exceed the
    # AusNet 1.5 kW peak target AND pay peak-rate electricity
    hours_over_limit = len(stress_before)
    kwh_over_limit   = sum(max(r["grid_before_kw"] - r["grid_limit_kw"], 0)
                           for r in results)
    extra_cost_no_v2h = sum(
        max(r["grid_before_kw"] - r["grid_limit_kw"], 0) * r["tariff_aud"]
        for r in results
    )

    matrix = []
    def row(k, v): matrix.append((k, v))
    def blank(): matrix.append(("", ""))
    def section(s): matrix.append((f"{'─'*3} {s} {'─'*3}", ""))

    row("Scenario",         "Scenario 4 — TOU-aware peak demand mitigation with PV + V2H")
    row("Controller",       "Mamdani Fuzzy Inference System (3 inputs, 9 rules, centroid defuzz)")
    row("Load data source", "Ausgrid Open Data 2013-14 — scaled to AusNet VIC autumn 2024")
    row("PV data source",   "NREL PVWATTS v8 — 6.6 kW, north-facing, Melbourne lat -37.8°")
    row("Tariff source",    "AusNet Services Schedule of Network Tariffs 2023-24 (Residential TOU)")
    row("EV model",         "Nissan Leaf 62 kWh equiv., AEMO EV Uptake Model 2023 commuter profile")
    row("Simulation date",  datetime.now().strftime("%Y-%m-%d"))
    blank()

    section("ENERGY FLOWS — FULL DAY")
    row("Total home energy demand (kWh)",            round(total_load, 2))
    row("Total PV generation (kWh)",                 round(total_pv, 2))
    row("PV energy used directly by home (kWh)",     round(total_pv_used, 2))
    row("PV surplus exported to grid (kWh)",         round(total_pv_surplus, 2))
    row("PV self-consumption rate (%)",              round(pv_sc_pct, 1))
    row("PV peak output (kW) and hour",              f"{max(pv_generation_kw):.2f} kW at 12:00")
    blank()

    section("GRID IMPORT — WITHOUT V2H (BASELINE)")
    row("Baseline total grid import (kWh)",          round(baseline_total_grid, 2))
    row("Baseline peak grid import — instantaneous (kW)", round(baseline_peak_kw, 2))
    row("Hour of peak grid demand",                  f"{baseline_peak_hour:02d}:00")
    row("Baseline grid import — peak window 16-21 (kWh)", round(baseline_peak_energy, 2))
    row("Hours grid exceeded AusNet 1.5 kW target",  hours_over_limit)
    row("Total kWh drawn above AusNet target (kWh)", round(kwh_over_limit, 2))
    row("Baseline total energy cost (AUD)",          f"${round(baseline_cost_total, 2)}")
    row("Baseline peak-window energy cost (AUD)",    f"${round(baseline_peak_cost, 2)}")
    row("Peak-window cost as % of daily bill (%)",   round(baseline_peak_cost / baseline_cost_total * 100, 1))
    blank()

    section("GRID IMPORT — WITH V2H (MANAGED)")
    row("Managed total grid import (kWh)",           round(managed_total_grid, 2))
    row("Managed peak grid import — instantaneous (kW)", round(managed_peak_kw, 2))
    row("Managed grid import — peak window 16-21 (kWh)", round(managed_peak_energy, 2))
    row("Grid stress hours remaining after V2H",     len(stress_after) if stress_after else "None — fully resolved")
    row("Managed total energy cost (AUD)",           f"${round(managed_cost_total, 2)}")
    row("Managed peak-window energy cost (AUD)",     f"${round(managed_peak_cost, 2)}")
    blank()

    section("V2H BENEFIT — WHAT CHANGED AND WHY IT MATTERS")
    row("Grid import reduction — total (kWh)",       round(grid_red_kwh, 2))
    row("Grid import reduction — total (%)",         round(grid_red_pct, 1))
    row("Peak demand reduction — instantaneous (kW)",round(peak_kw_red, 2))
    row("Peak demand reduction (%)",                 round(peak_kw_pct, 1))
    row("Peak-window energy reduction (kWh)",        round(pk_e_red, 2))
    row("Peak-window energy reduction (%)",          round(pk_e_pct, 1))
    row("Daily cost saving (AUD)",                   f"${round(cost_saving, 2)}")
    row("Daily cost saving (%)",                     round(cost_pct, 1))
    row("Peak-window cost saving (AUD)",             f"${round(pk_cost_save, 2)}")
    row("Peak-window cost saving (%)",               round(pk_cost_pct, 1))
    row("Grid stress hours resolved by V2H",         f"{hours_over_limit} → {len(stress_after)} (fully resolved)" if not stress_after else f"{hours_over_limit} → {len(stress_after)}")
    row("Annualised cost saving estimate (AUD/year)",f"~${round(cost_saving * 250, 0):.0f}  (250 weekday-equivalent days)")
    blank()

    section("EV BATTERY USAGE")
    row("EV discharge energy — total (kWh)",         round(total_ev_dis, 2))
    row("EV discharge — peak window 16-21 (kWh)",    round(peak_ev_dis, 2))
    row("EV discharge — outside peak window (kWh)",  round(offpeak_ev_dis, 2))
    row("EV discharge efficiency note",              "Relay-based prototype — converter losses not modelled")
    blank()

    section("EV SOC — BATTERY STATE")
    row("Initial SOC on arrival (%) — AEMO commuter", round(initial_soc, 1))
    row("Final SOC at end of day (%)",               round(final_soc, 2))
    row("Minimum SOC recorded during simulation (%)",round(min_soc, 2))
    row("SOC consumed by V2H discharge (%)",         round(soc_drop, 2))
    row("SOC energy consumed by V2H (kWh equiv.)",   round(total_ev_dis, 2))
    row("Mobility reserve floor (%)",                SOC_RESERVE_PCT)
    row("Hard cutoff floor (%)",                     SOC_MIN_PCT)
    row("SOC margin above reserve at end of day (%)",round(soc_margin_end, 2))
    row("SOC sufficient for next day commute?",      "Yes" if final_soc >= SOC_RESERVE_PCT + 15 else "Marginal — consider overnight charge")
    blank()

    section("FUZZY CONTROLLER — DECISION ANALYTICS")
    row("Fuzzy engine active hours",                 len(active))
    row("Average fuzzy output score (active hours)", round(avg_fuzzy, 1))
    row("FUZZY_HIGH_DISCHARGE hours (score ≥ 72)",   fmt_hours(high_d))
    row("FUZZY_MEDIUM_DISCHARGE hours (46–71)",      fmt_hours(med_d))
    row("FUZZY_LOW_DISCHARGE hours (25–45)",         fmt_hours(low_d))
    row("Controller behaviour",                      "Proportional — discharge scales with excess, SOC, and tariff")
    blank()

    section("RELAY & OPERATIONAL LOG")
    row("Relay ON hours (total)",                    len(relay_on_hrs))
    row("V2H active periods",                        fmt_hours(relay_on_hrs))
    row("Peak-window support periods",               fmt_hours(pk_support_hrs))
    row("Grid stress periods — BEFORE V2H",          fmt_hours(stress_before))
    row("Grid stress periods — AFTER V2H",           fmt_hours(stress_after) if stress_after else "None")
    row("EV unavailable during demand events",       fmt_hours(unavail_hrs) if unavail_hrs else "None")
    row("SOC reserve protection activations",        fmt_hours(low_soc_hrs) if low_soc_hrs else "None")
    blank()

    section("TARIFF CONTEXT (AusNet VIC 2023-24)")
    row("Off-peak rate 00:00–06:59 (AUD/kWh)",       f"${TARIFF_OFFPEAK}")
    row("Shoulder rate 07:00–15:59 & 22:00–23:59",   f"${TARIFF_SHOULDER}")
    row("Peak rate 16:00–21:59 (AUD/kWh)",           f"${TARIFF_PEAK}")
    row("Peak/off-peak tariff ratio",                f"{TARIFF_PEAK/TARIFF_OFFPEAK:.1f}x — strong incentive to avoid peak import")
    row("AusNet managed peak target (kW/household)", GRID_LIMIT_PEAK_KW)
    blank()

    section("WHY V2H IS NEEDED — PROBLEM STATEMENT METRICS")
    row("Without V2H: hours breaching AusNet 1.5 kW target", hours_over_limit)
    row("Without V2H: excess energy drawn above target (kWh)", round(kwh_over_limit, 2))
    row("Without V2H: extra cost from above-target demand (AUD)", f"${round(extra_cost_no_v2h, 2)}")
    row("Without V2H: peak demand creates grid stress", "Evening peak coincides with network-wide maximum demand")
    row("PV generation timing mismatch",              "Solar peaks 10:00–14:00; household demand peaks 17:00–20:00")
    row("PV surplus going to grid (kWh)",             round(total_pv_surplus, 2))
    row("V2H role",                                   "Bridges solar-to-evening gap; displaces peak grid import with stored PV energy")
    blank()

    section("TRADE-OFFS AND LIMITATIONS")
    row("Main benefit",   "Evening peak demand reduced by " + str(round(peak_kw_pct, 1)) + "%; daily bill reduced by " + str(round(cost_pct, 1)) + "%")
    row("Main trade-off", f"EV SOC reduced by {round(soc_drop, 2)}% ({round(total_ev_dis, 2)} kWh); remains {round(soc_margin_end, 2)}% above reserve")
    row("Hardware limitation", "Relay is binary (ON/OFF); real V2H requires proportional bidirectional converter")
    row("Data limitation",     "Synthetic 24-hr profile — real households vary day-to-day")
    row("Model limitation",    "Converter efficiency losses not modelled (typically 5–8% round-trip)")
    row("SOC limitation",      "No departure-time constraint enforced — future work to add 'ready by 07:00' constraint")

    return matrix


# ════════════════════════════════════════════════════════════
#  MAIN SIMULATION LOOP
# ════════════════════════════════════════════════════════════

def main():
    global ev_soc

    os.makedirs("logs", exist_ok=True)
    log_file     = "logs/scenario4_fuzzy_v2h_log.csv"
    summary_file = "logs/scenario4_fuzzy_v2h_summary.csv"

    results = []

    print("=" * 68)
    print("  SCENARIO 4 — FUZZY V2H PEAK DEMAND MITIGATION")
    print("  AusNet VIC 2023-24 | Mamdani FIS | 6.6 kW PV | 62 kWh EV")
    print(f"  Peak limit = {GRID_LIMIT_PEAK_KW} kW | "
          f"Peak tariff = ${TARIFF_PEAK}/kWh | "
          f"Starting SOC = {ev_soc:.0f}%")
    print("=" * 68)
    print(f"{'Hr':>4}  {'Load':>5}  {'PV':>5}  {'Grid→':>6}  {'→Grid':>6}  "
          f"{'EV':>5}  {'SOC':>11}  {'Fuzzy':>6}  {'Save$':>6}  {'Relay':>5}  Decision")
    print("-" * 100)

    try:
        for hour in hours:
            load      = home_load_kw[hour]
            pv        = pv_generation_kw[hour]
            available = ev_available[hour]
            soc_before = ev_soc

            r = fuzzy_controller(hour, load, pv, ev_soc, available)

            tariff        = get_tariff(hour)
            baseline_cost = round(r["grid_before"] * tariff, 4)
            managed_cost  = round(r["grid_after"]  * tariff, 4)
            cost_saving   = round(baseline_cost - managed_cost, 4)
            fuzzy_score   = round(r["fuzzy_raw"] * 100, 1)

            if r["relay"]:
                relay.on()
            else:
                relay.off()

            ev_soc    = update_soc(ev_soc, r["ev_kw"])
            soc_after = ev_soc

            print(f"{hour:02d}:00  "
                  f"{load:5.2f}  {pv:5.2f}  "
                  f"{r['grid_before']:6.2f}  {r['grid_after']:6.2f}  "
                  f"{r['ev_kw']:5.2f}  "
                  f"{soc_before:5.1f}→{soc_after:5.1f}%  "
                  f"{fuzzy_score:6.1f}  "
                  f"{cost_saving:6.3f}  "
                  f"{'ON ' if r['relay'] else 'OFF':>5}  "
                  f"{r['decision']}")

            results.append({
                "timestamp":          datetime.now().isoformat(timespec="seconds"),
                "hour":               hour,
                "home_load_kw":       load,
                "pv_generation_kw":   pv,
                "pv_to_home_kw":      round(r["pv_to_home"], 3),
                "pv_surplus_kw":      round(r["pv_surplus"], 3),
                "grid_before_kw":     round(r["grid_before"], 3),
                "grid_limit_kw":      r["grid_limit"],
                "excess_kw":          round(r["excess"], 3),
                "ev_available":       available,
                "soc_before_pct":     round(soc_before, 2),
                "soc_after_pct":      round(soc_after, 2),
                "excess_norm":        round(r["excess_n"], 3),
                "soc_margin_norm":    round(r["soc_n"], 3),
                "tariff_norm":        round(r["tariff_n"], 3),
                "fuzzy_score":        fuzzy_score,
                "decision":           r["decision"],
                "ev_power_kw":        r["ev_kw"],
                "grid_after_kw":      round(r["grid_after"], 3),
                "tariff_aud":         tariff,
                "baseline_cost_aud":  baseline_cost,
                "managed_cost_aud":   managed_cost,
                "cost_saving_aud":    cost_saving,
                "relay_state":        "ON" if r["relay"] else "OFF",
            })

            sleep(HOUR_DELAY_SEC)

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        relay.off()
        print("\nRelay OFF — safe state.")

        # ── Write hourly log CSV ─────────────────────────────
        fields = list(results[0].keys()) if results else []
        with open(log_file, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(results)

        # ── Build and print summary matrix ───────────────────
        summary = build_summary(results)

        print("\n" + "=" * 68)
        print("  SCENARIO 4 SUMMARY MATRIX")
        print("=" * 68)
        for k, v in summary:
            if k == "":
                print()
            elif k.startswith("─"):
                print(f"\n  {k}")
            else:
                print(f"  {k:<55} {v}")
        print("=" * 68)

        with open(summary_file, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Metric", "Value"])
            for k, v in summary:
                w.writerow([k, v])

        print(f"\nHourly log  → {log_file}")
        print(f"Summary     → {summary_file}")
        print("Simulation complete.")


if __name__ == "__main__":
    main()