"""Session / time-of-week context (UTC). See knowledge/core.md section 4."""

import pandas as pd

OPEN_HOURS_UTC = {7, 8, 13, 14, 15}  # London open, US open (covers DST shift)


def annotate(df: pd.DataFrame, tf_seconds: int) -> pd.DataFrame:
    out = df.copy()
    entry_time = pd.to_datetime(df["time"] + tf_seconds, unit="s", utc=True)  # we act at the next open
    out["session_open"] = entry_time.dt.hour.isin(OPEN_HOURS_UTC)
    out["weekend"] = entry_time.dt.weekday >= 5
    return out
