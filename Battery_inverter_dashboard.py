import time
import threading
import random
from dataclasses import dataclass

import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt

from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.datastore import ModbusSequentialDataBlock
from pymodbus.server.sync import StartTcpServer

# =========================
# Battery model
# =========================

@dataclass
class BatteryParams:
    capacity_kwh: float = 10.0
    soc_init: float = 50.0      # %
    v_nominal: float = 400.0    # V
    p_max_kw: float = 5.0       # kW charge/discharge
    eta_charge: float = 0.95
    eta_discharge: float = 0.95

class BatteryInverter:
    def __init__(self, params: BatteryParams):
        self.params = params
        self.soc = params.soc_init  # %
        self.p_set_kw = 0.0         # + discharge, - charge
        self.v_dc = params.v_nominal
        self.i_dc = 0.0
        self.mode = 1               # 0=Off, 1=On
        self.alarm = 0

    def step(self, dt_s: float):
        if self.mode == 0:
            self.i_dc = 0.0
            return

        p = max(-self.params.p_max_kw,
                min(self.params.p_max_kw, self.p_set_kw))

        if p > 0:
            eff_p = p / self.params.eta_discharge
        else:
            eff_p = p * self.params.eta_charge

        delta_kwh = eff_p * (dt_s / 3600.0)
        delta_soc = (delta_kwh / self.params.capacity_kwh) * 100.0
        self.soc += delta_soc

        if self.soc > 100.0:
            self.soc = 100.0
            self.alarm = 1
        elif self.soc < 0.0:
            self.soc = 0.0
            self.alarm = 2
        else:
            self.alarm = 0

        self.v_dc = self.params.v_nominal * (0.95 + 0.1 * (self.soc / 100.0))
        if self.v_dc > 0:
            self.i_dc = (p * 1000.0) / self.v_dc
        else:
            self.i_dc = 0.0

# =========================
# Modbus helpers
# =========================

def create_modbus_context():
    store = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(0, [0]*100)
    )
    return ModbusServerContext(slaves=store, single=True)

def write_float_to_hr(context, address, value, scale=1.0):
    context[0x00].setValues(3, address, [int(value * scale) & 0xFFFF])

def read_float_from_hr(context, address, scale=1.0):
    hr = context[0x00].getValues(3, address, count=1)
    return hr[0] / scale

def write_int_to_hr(context, address, value):
    context[0x00].setValues(3, address, [int(value) & 0xFFFF])

def read_int_from_hr(context, address):
    hr = context[0x00].getValues(3, address, count=1)
    return int(hr[0])

def modbus_server_thread(context, address="0.0.0.0", port=5020):
    StartTcpServer(context, address=(address, port))

# =========================
# Simulator loop (background)
# =========================

def simulator_loop(batt: BatteryInverter, context, dt_s=1.0):
    while True:
        p_set = read_float_from_hr(context, 1, scale=10.0)
        mode = read_int_from_hr(context, 4)

        batt.p_set_kw = p_set
        batt.mode = mode

        batt.step(dt_s)

        write_float_to_hr(context, 0, batt.soc, scale=100.0)
        write_float_to_hr(context, 2, batt.v_dc, scale=10.0)
        write_float_to_hr(context, 3, batt.i_dc, scale=10.0)
        write_int_to_hr(context, 5, batt.alarm)

        time.sleep(dt_s)

# =========================
# Streamlit app
# =========================

st.set_page_config(page_title="Battery Inverter Simulator", layout="wide")

if "modbus_started" not in st.session_state:
    st.session_state.modbus_started = False
if "data" not in st.session_state:
    st.session_state.data = []

st.sidebar.title("Battery Inverter Simulator")

capacity = st.sidebar.number_input("Capacity (kWh)", 1.0, 1000.0, 10.0)
pmax = st.sidebar.number_input("Max Power (kW)", 0.1, 100.0, 5.0)
soc_init = st.sidebar.number_input("Initial SOC (%)", 0.0, 100.0, 50.0)
v_nom = st.sidebar.number_input("Nominal DC Voltage (V)", 100.0, 1000.0, 400.0)

st.sidebar.markdown("**Modbus TCP** (port 5020)")
st.sidebar.write("HR0: SOC*100 (R)")
st.sidebar.write("HR1: P_SET*10 (R/W)")
st.sidebar.write("HR2: V_DC*10 (R)")
st.sidebar.write("HR3: I_DC*10 (R)")
st.sidebar.write("HR4: MODE (0/1) (R/W)")
st.sidebar.write("HR5: ALARM (R)")

start_sim = st.sidebar.button("Start Simulator")

if start_sim and not st.session_state.modbus_started:
    params = BatteryParams(
        capacity_kwh=capacity,
        soc_init=soc_init,
        v_nominal=v_nom,
        p_max_kw=pmax
    )
    batt = BatteryInverter(params)
    context = create_modbus_context()

    write_float_to_hr(context, 0, batt.soc, scale=100.0)
    write_float_to_hr(context, 1, batt.p_set_kw, scale=10.0)
    write_float_to_hr(context, 2, batt.v_dc, scale=10.0)
    write_float_to_hr(context, 3, batt.i_dc, scale=10.0)
    write_int_to_hr(context, 4, batt.mode)
    write_int_to_hr(context, 5, batt.alarm)

    t_modbus = threading.Thread(target=modbus_server_thread, args=(context,), daemon=True)
    t_modbus.start()

    t_sim = threading.Thread(target=simulator_loop, args=(batt, context), daemon=True)
    t_sim.start()

    st.session_state.modbus_started = True
    st.session_state.context = context
    st.session_state.batt = batt
    st.session_state.data = []

st.subheader("Live Battery Inverter Data")

if st.session_state.modbus_started:
    ctx = st.session_state.context

    # Real‑time control from dashboard (also visible to external Modbus clients)
    current_pmax = pmax
    ui_p_set = st.sidebar.slider("Setpoint (kW)", -current_pmax, current_pmax, 0.0, 0.1)
    ui_mode = st.sidebar.selectbox("Mode", ["On", "Off"])
    mode_val = 1 if ui_mode == "On" else 0

    write_float_to_hr(ctx, 1, ui_p_set, scale=10.0)
    write_int_to_hr(ctx, 4, mode_val)

    # Read back latest values
    soc = read_float_from_hr(ctx, 0, scale=100.0)
    p_set = read_float_from_hr(ctx, 1, scale=10.0)
    v_dc = read_float_from_hr(ctx, 2, scale=10.0)
    i_dc = read_float_from_hr(ctx, 3, scale=10.0)
    mode = read_int_from_hr(ctx, 4)
    alarm = read_int_from_hr(ctx, 5)

    st.session_state.data.append({
        "time": time.time(),
        "soc": soc,
        "p_set_kw": p_set,
        "v_dc": v_dc,
        "i_dc": i_dc,
        "mode": mode,
        "alarm": alarm
    })

    df = pd.DataFrame(st.session_state.data).tail(300)

    col1, col2 = st.columns([2, 1])

    with col1:
        fig, ax = plt.subplots(3, 1, figsize=(8, 8), sharex=True)
        if not df.empty:
            ax[0].plot(df["time"], df["soc"])
            ax[0].set_ylabel("SOC (%)")
            ax[1].plot(df["time"], df["p_set_kw"])
            ax[1].set_ylabel("P_set (kW)")
            ax[2].plot(df["time"], df["v_dc"])
            ax[2].set_ylabel("V_dc (V)")
            ax[2].set_xlabel("Time")
        st.pyplot(fig)

    with col2:
        st.write("Latest values")
        st.metric("SOC (%)", f"{soc:.2f}")
        st.metric("P_set (kW)", f"{p_set:.2f}")
        st.metric("V_dc (V)", f"{v_dc:.1f}")
        st.metric("I_dc (A)", f"{i_dc:.1f}")
        st.metric("Mode", "On" if mode == 1 else "Off")
        st.metric("Alarm", alarm)

    # Light auto‑refresh
    time.sleep(1)
    st.experimental_rerun()
else:
    st.info("Click **Start Simulator** in the sidebar to launch the Modbus server and battery model.")

