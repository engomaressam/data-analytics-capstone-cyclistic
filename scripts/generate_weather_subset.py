import os
import sys
from datetime import datetime
import numpy as np
import pandas as pd

from datetime import timedelta

def main():
    # Defaults: last 7 days of September 2025
    start_str = os.environ.get("WX_START", "2025-09-24")
    end_str = os.environ.get("WX_END", "2025-09-30")
    out_path = os.environ.get("WX_OUT", "data/external/weather_subset_hourly.csv")

    start = datetime.fromisoformat(start_str)
    # inclusive end at 23:00 to cover full days
    end = datetime.fromisoformat(end_str) + timedelta(hours=23)
    hrs = pd.date_range(start, end, freq="H")
    np.random.seed(42)

    # Synthetic but plausible weather variables
    base_temp = 20 + 5*np.sin(2*np.pi*(hrs.hour/24))  # diurnal cycle
    seasonal_adjust = -3  # early fall
    temperature_c = base_temp + seasonal_adjust + np.random.normal(0, 2, len(hrs))
    precip_mm = np.clip(np.random.exponential(0.2, len(hrs)) - 0.1, 0, None)
    wind_speed_ms = np.clip(np.random.normal(3, 1.5, len(hrs)), 0, None)
    humidity_pct = np.clip(65 + 15*np.sin(2*np.pi*(hrs.hour/24 + 0.25)) + np.random.normal(0, 5, len(hrs)), 20, 100)
    pressure_hpa = 1013 + np.random.normal(0, 5, len(hrs))

    wx = pd.DataFrame({
        'datetime': hrs,
        'temperature_c': np.round(temperature_c, 2),
        'precip_mm': np.round(precip_mm, 3),
        'wind_speed_ms': np.round(wind_speed_ms, 2),
        'humidity_pct': np.round(humidity_pct, 1),
        'pressure_hpa': np.round(pressure_hpa, 1),
    })

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    wx.to_csv(out_path, index=False)
    print(f"Wrote {out_path} rows: {len(wx)}")

if __name__ == "__main__":
    main()