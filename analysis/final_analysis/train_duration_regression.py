import os
import json
import argparse
import warnings
from typing import List

import numpy as np
import pandas as pd

# Fallback import for joblib
try:
    import joblib  # type: ignore
except ImportError:  # pragma: no cover
    from sklearn.externals import joblib  # type: ignore

from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.impute import SimpleImputer
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
try:
    from sklearn.inspection import PartialDependenceDisplay
except Exception:
    PartialDependenceDisplay = None

warnings.filterwarnings("ignore", category=FutureWarning)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def haversine_distance(lat1, lon1, lat2, lon2):
    """Vectorized haversine distance in kilometers; returns np.ndarray."""
    # Convert degrees to radians
    lat1 = np.radians(lat1)
    lon1 = np.radians(lon1)
    lat2 = np.radians(lat2)
    lon2 = np.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    c = 2 * np.arcsin(np.sqrt(a))
    R = 6371.0  # km
    return R * c


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Create modeling features, including distance_km and time components."""
    # If coordinates are present, compute haversine distance; else set NaN
    cols_needed = ["start_lat", "start_lng", "end_lat", "end_lng"]
    has_coords = all(col in df.columns for col in cols_needed)

    if has_coords:
        # Replace 0 coordinates with NaN to avoid unrealistic distances
        coord_df = df[cols_needed].replace({0: np.nan})
        df["distance_km"] = haversine_distance(
            coord_df["start_lat"], coord_df["start_lng"], coord_df["end_lat"], coord_df["end_lng"]
        )
    else:
        df["distance_km"] = np.nan

    # Ensure key temporal numeric columns exist; if not, derive from started_at
    if "started_at" in df.columns and not {"hour_of_day", "day_of_week", "month"}.issubset(df.columns):
        try:
            dt = pd.to_datetime(df["started_at"], errors="coerce")
            df["hour_of_day"] = df.get("hour_of_day", dt.dt.hour)
            df["day_of_week"] = df.get("day_of_week", dt.dt.dayofweek)  # 0=Mon..6=Sun
            df["month"] = df.get("month", dt.dt.month)
            df["is_weekend"] = df.get("is_weekend", dt.dt.dayofweek.isin([5, 6]).astype(int))
        except Exception:
            # If parsing fails, keep existing columns if present
            pass

    # Normalize boolean-like is_weekend to int
    if "is_weekend" in df.columns:
        if df["is_weekend"].dtype == bool:
            df["is_weekend"] = df["is_weekend"].astype(int)

    # Derive time_of_day and day_type if possible
    try:
        if "time_of_day" not in df.columns and "hour_of_day" in df.columns:
            bins = [0, 6, 12, 18, 24]
            labels = ["Night", "Morning", "Afternoon", "Evening"]
            # Use right=False so that 0-5 Night, 6-11 Morning, etc.
            df["time_of_day"] = pd.cut(df["hour_of_day"], bins=bins, labels=labels, right=False, include_lowest=True).astype(str)
        if "day_type" not in df.columns and "is_weekend" in df.columns:
            df["day_type"] = np.where(df["is_weekend"].astype(int) == 1, "Weekend", "Weekday")
    except Exception:
        pass

    return df


def add_station_features(df: pd.DataFrame, top_pairs: int = 50) -> pd.DataFrame:
    """Add station popularity and station-pair frequency features.
    Chooses station ID if available, else station name.
    Creates numeric features: start_station_popularity, end_station_popularity, station_pair_freq
    Creates low-card categorical: station_pair_top (top N pairs, others->"Other")
    """
    start_col = None
    end_col = None
    for cand in ["start_station_id", "start_station_name"]:
        if cand in df.columns:
            start_col = cand
            break
    for cand in ["end_station_id", "end_station_name"]:
        if cand in df.columns:
            end_col = cand
            break
    if start_col is None or end_col is None:
        # If station info unavailable, just return
        return df

    # Popularity counts
    try:
        start_counts = df[start_col].value_counts()
        end_counts = df[end_col].value_counts()
        df["start_station_popularity"] = df[start_col].map(start_counts).astype(float)
        df["end_station_popularity"] = df[end_col].map(end_counts).astype(float)
    except Exception:
        df["start_station_popularity"] = np.nan
        df["end_station_popularity"] = np.nan

    # Station pair frequency (string key)
    try:
        pair_key = df[start_col].astype(str) + "|" + df[end_col].astype(str)
        pair_counts = pair_key.value_counts()
        df["station_pair_freq"] = pair_key.map(pair_counts).astype(float)
        # Top N pairs
        top_keys = set(pair_counts.head(max(1, int(top_pairs))).index.tolist())
        df["station_pair_top"] = np.where(pair_key.isin(top_keys), pair_key, "Other")
    except Exception:
        df["station_pair_freq"] = np.nan
        df["station_pair_top"] = "Other"

    return df


def merge_weather(df: pd.DataFrame, weather_csv: str, dt_col: str = "datetime", prefix: str = "wx_") -> pd.DataFrame:
    """Merge hourly weather features into the ride dataframe.
    Expects weather CSV with a datetime-like column (dt_col). Parses to local date+hour and merges on those.
    Numeric columns from weather are prefixed with `prefix`.
    """
    try:
        wx = pd.read_csv(weather_csv)
    except Exception as e:
        print(f"Warning: could not read weather CSV: {e}")
        return df

    # Find a datetime column
    possible_dt_cols = [dt_col, "date_time", "timestamp", "time", "valid_time", "Datetime", "datetime_utc"]
    dt_found = None
    for c in possible_dt_cols:
        if c in wx.columns:
            dt_found = c
            break
    if dt_found is None:
        print("Warning: no datetime-like column found in weather CSV; skipping merge")
        return df

    try:
        wx_dt = pd.to_datetime(wx[dt_found], errors="coerce")
        wx["date"] = wx_dt.dt.date.astype(str)
        wx["hour"] = wx_dt.dt.hour
    except Exception as e:
        print(f"Warning: could not parse weather datetime: {e}")
        return df

    # Derive date/hour on rides
    if "started_at" in df.columns:
        dt = pd.to_datetime(df["started_at"], errors="coerce")
        df["date"] = dt.dt.date.astype(str)
        df["hour"] = dt.dt.hour
    elif {"date", "hour"}.issubset(df.columns):
        # already present
        pass
    else:
        print("Warning: cannot derive date/hour from rides; skipping weather merge")
        return df

    # Select numeric weather columns
    wx_num_cols = [c for c in wx.columns if c not in [dt_found, "date", "hour"] and pd.api.types.is_numeric_dtype(wx[c])]
    if not wx_num_cols:
        print("Warning: no numeric weather columns found; skipping weather merge")
        return df

    wx_renamed = wx[["date", "hour"] + wx_num_cols].copy()
    wx_renamed.columns = ["date", "hour"] + [prefix + c for c in wx_num_cols]

    merged = df.merge(wx_renamed, on=["date", "hour"], how="left")
    return merged


def train_model(df: pd.DataFrame, outdir: str, test_size: float = 0.2, random_state: int = 42,
                n_estimators: int = 100, max_depth: int | None = 25, n_jobs: int = 1,
                max_samples: float | int | None = 100_000, min_samples_leaf: int = 3,
                max_train_rows: int | None = 300_000,
                top_station_pairs: int = 50,
                weather_csv: str | None = None,
                weather_dt_col: str = "datetime",
                compare_models: bool = False):
    ensure_dir(outdir)
    figdir = os.path.join(outdir, "figures")
    ensure_dir(figdir)

    # Prepare target
    if "ride_duration_minutes" not in df.columns:
        raise ValueError("ride_duration_minutes column not found in input data.")

    # Basic filtering to remove invalid durations (mirror cleaning rules)
    df = df[(df["ride_duration_minutes"] > 1) & (df["ride_duration_minutes"] < 1440)]

    # Build/augment features
    df = build_features(df)
    df = add_station_features(df, top_pairs=top_station_pairs)
    if weather_csv:
        df = merge_weather(df, weather_csv=weather_csv, dt_col=weather_dt_col)

    target = df["ride_duration_minutes"].astype(float)

    # Candidate features
    numeric_features: List[str] = [
        "hour_of_day",
        "day_of_week",
        "month",
        "is_weekend",
        "start_lat",
        "start_lng",
        "end_lat",
        "end_lng",
        "distance_km",
        # station-level numeric features
        "start_station_popularity",
        "end_station_popularity",
        "station_pair_freq",
    ]

    # Include weather numeric features if present
    wx_numeric = [c for c in df.columns if c.startswith("wx_") and pd.api.types.is_numeric_dtype(df[c])]
    numeric_features += wx_numeric

    categorical_features: List[str] = [
        "member_casual",
        "rideable_type",
        "time_of_day",  # e.g., Morning/Afternoon/Evening/Night
        "day_type",     # Weekday/Weekend
        "station_pair_top",  # limited top pairs
    ]

    # Keep only columns that exist in df
    numeric_features = [c for c in numeric_features if c in df.columns]
    categorical_features = [c for c in categorical_features if c in df.columns]

    feature_cols = numeric_features + categorical_features
    if not feature_cols:
        raise ValueError("No usable feature columns found in input data.")

    X = df[feature_cols].copy()

    # Preprocessor
    numeric_transformer = Pipeline(steps=[
        ("imputer", SimpleImputer(strategy="median")),
    ])

    categorical_transformer = Pipeline(steps=[
        ("imputer", SimpleImputer(strategy="most_frequent")),
        ("onehot", OneHotEncoder(handle_unknown="ignore")),
    ])

    preprocessor = ColumnTransformer(
        transformers=[
            ("num", numeric_transformer, numeric_features),
            ("cat", categorical_transformer, categorical_features),
        ]
    )

    # Model (lighter defaults to avoid MemoryError on large datasets)
    model = RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        n_jobs=n_jobs,
        random_state=random_state,
        max_samples=max_samples,
        min_samples_leaf=min_samples_leaf,
    )

    pipeline = Pipeline(steps=[("preprocessor", preprocessor), ("model", model)])

    # Split
    X_train, X_test, y_train, y_test = train_test_split(
        X, target, test_size=test_size, random_state=random_state
    )

    # Optional downsampling of training set to reduce memory footprint
    if max_train_rows is not None and len(X_train) > max_train_rows:
        sample_idx = np.random.RandomState(random_state).choice(len(X_train), size=max_train_rows, replace=False)
        X_train = X_train.iloc[sample_idx]
        y_train = y_train.iloc[sample_idx]

    # Fit
    pipeline.fit(X_train, y_train)

    # Predict and metrics
    y_pred = pipeline.predict(X_test)
    mae = mean_absolute_error(y_test, y_pred)
    # Compute RMSE without using squared=False for compatibility
    mse = mean_squared_error(y_test, y_pred)
    rmse = float(np.sqrt(mse))
    r2 = r2_score(y_test, y_pred)

    metrics = {
        "samples": int(len(df)),
        "train_size": int(len(X_train)),
        "test_size": int(len(X_test)),
        "mae": round(float(mae), 3),
        "rmse": round(float(rmse), 3),
        "r2": round(float(r2), 4),
        "features": feature_cols,
        "numeric_features": numeric_features,
        "categorical_features": categorical_features,
    }

    # Save metrics
    with open(os.path.join(outdir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    # Save model
    model_path = os.path.join(outdir, "duration_rf_pipeline.joblib")
    joblib.dump(pipeline, model_path)

    # Predicted vs actual plot (SVG)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.figure(figsize=(6, 6))
        plt.scatter(y_test, y_pred, s=10, alpha=0.5)
        plt.plot([y_test.min(), y_test.max()], [y_test.min(), y_test.max()], "r--", lw=2)
        plt.xlabel("Actual Duration (min)")
        plt.ylabel("Predicted Duration (min)")
        plt.title("Predicted vs Actual Ride Duration")
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        pvap = os.path.join(figdir, "pred_vs_actual.svg")
        plt.savefig(pvap, format="svg")
        plt.close()
    except Exception as e:
        print(f"Warning: could not generate plot: {e}")

    # Feature importances
    try:
        # Get transformed feature names
        ohe = pipeline.named_steps["preprocessor"].named_transformers_["cat"].named_steps.get("onehot")
        cat_names = list(ohe.get_feature_names_out(categorical_features)) if ohe and categorical_features else []
        feature_names = numeric_features + cat_names

        importances = pipeline.named_steps["model"].feature_importances_
        fi_df = pd.DataFrame({"feature": feature_names, "importance": importances})
        fi_df = fi_df.sort_values(by="importance", ascending=False)
        fi_path = os.path.join(outdir, "feature_importances.csv")
        fi_df.to_csv(fi_path, index=False)

        # Bar chart (SVG)
        import matplotlib.pyplot as plt
        top_n = min(30, len(fi_df))
        plt.figure(figsize=(8, max(4, top_n * 0.25)))
        plt.barh(fi_df["feature"].head(top_n)[::-1], fi_df["importance"].head(top_n)[::-1])
        plt.xlabel("Importance")
        plt.title("Top Feature Importances (RandomForest)")
        plt.tight_layout()
        fi_svg = os.path.join(figdir, "feature_importances.svg")
        plt.savefig(fi_svg, format="svg")
        plt.close()
    except Exception as e:
        print(f"Warning: could not compute feature importances: {e}")

    # Save sample predictions
    err_df = pd.DataFrame({"actual": y_test, "pred": y_pred})
    err_df.to_csv(os.path.join(outdir, "predictions_sample.csv"), index=False)

    # Error anatomy by segments
    try:
        seg_cols = [c for c in ["member_casual", "time_of_day", "station_pair_top", "day_type"] if c in X_test.columns]
        err_df = pd.DataFrame({"actual": y_test, "pred": y_pred})
        err_df = pd.concat([err_df.reset_index(drop=True), X_test[seg_cols].reset_index(drop=True)], axis=1)
        err_df["error"] = err_df["actual"] - err_df["pred"]
        rows = []
        for seg in seg_cols:
            grp = err_df.groupby(seg)
            mae_seg = grp.apply(lambda g: float(mean_absolute_error(g["actual"], g["pred"])) )
            rmse_seg = grp.apply(lambda g: float(np.sqrt(mean_squared_error(g["actual"], g["pred"])) ))
            for k in mae_seg.index:
                rows.append({"segment": seg, "value": str(k), "mae": round(mae_seg[k], 3), "rmse": round(rmse_seg[k], 3), "count": int(len(grp.get_group(k)))})
        ea_df = pd.DataFrame(rows)
        ea_path = os.path.join(outdir, "error_anatomy.csv")
        ea_df.to_csv(ea_path, index=False)
    except Exception as e:
        print(f"Warning: could not compute error anatomy: {e}")

    # Partial dependence plots (for a few numeric features)
    try:
        if PartialDependenceDisplay is not None:
            import matplotlib.pyplot as plt
            pdp_features = [f for f in ["hour_of_day", "distance_km", "start_station_popularity"] if f in numeric_features]
            pdp_features = pdp_features[:3]
            if pdp_features:
                fig, ax = plt.subplots(nrows=len(pdp_features), ncols=1, figsize=(6, 3*len(pdp_features)))
                PartialDependenceDisplay.from_estimator(pipeline, X_test, pdp_features, ax=ax)
                plt.tight_layout()
                pdp_svg = os.path.join(figdir, "partial_dependence_rf.svg")
                plt.savefig(pdp_svg, format="svg")
                plt.close()
    except Exception as e:
        print(f"Warning: could not generate partial dependence plots: {e}")

    # Model comparison: RF vs HistGradientBoosting
    if compare_models:
        try:
            hgb = HistGradientBoostingRegressor(max_depth=7, learning_rate=0.06, max_iter=200, random_state=random_state)
            hgb_pipe = Pipeline(steps=[("preprocessor", preprocessor), ("model", hgb)])
            hgb_pipe.fit(X_train, y_train)
            hgb_pred = hgb_pipe.predict(X_test)
            mae_h = mean_absolute_error(y_test, hgb_pred)
            rmse_h = float(np.sqrt(mean_squared_error(y_test, hgb_pred)))
            r2_h = r2_score(y_test, hgb_pred)
            metrics_h = {
                "samples": int(len(df)),
                "train_size": int(len(X_train)),
                "test_size": int(len(X_test)),
                "mae": round(float(mae_h), 3),
                "rmse": round(float(rmse_h), 3),
                "r2": round(float(r2_h), 4),
                "features": feature_cols,
            }
            with open(os.path.join(outdir, "metrics_hgb.json"), "w", encoding="utf-8") as f:
                json.dump(metrics_h, f, indent=2)
            # Permutation importance for HGB
            try:
                perm = permutation_importance(hgb_pipe, X_test, y_test, n_repeats=5, random_state=random_state, n_jobs=n_jobs)
                ohe = hgb_pipe.named_steps["preprocessor"].named_transformers_["cat"].named_steps.get("onehot")
                cat_names = list(ohe.get_feature_names_out(categorical_features)) if ohe and categorical_features else []
                hgb_feature_names = numeric_features + cat_names
                pi_df = pd.DataFrame({"feature": hgb_feature_names, "importance_mean": perm.importances_mean, "importance_std": perm.importances_std})
                pi_df = pi_df.sort_values(by="importance_mean", ascending=False)
                pi_path = os.path.join(outdir, "feature_importances_hgb_permutation.csv")
                pi_df.to_csv(pi_path, index=False)
                import matplotlib.pyplot as plt
                top_n = min(30, len(pi_df))
                plt.figure(figsize=(8, max(4, top_n * 0.25)))
                plt.barh(pi_df["feature"].head(top_n)[::-1], pi_df["importance_mean"].head(top_n)[::-1])
                plt.xlabel("Permutation Importance (mean)")
                plt.title("Top Features (HistGradientBoosting)")
                plt.tight_layout()
                pi_svg = os.path.join(figdir, "feature_importances_hgb_permutation.svg")
                plt.savefig(pi_svg, format="svg")
                plt.close()
            except Exception as e:
                print(f"Warning: could not compute permutation importances for HGB: {e}")
        except Exception as e:
            print(f"Warning: model comparison failed: {e}")
    print("Training complete. Metrics:")
    print(json.dumps(metrics, indent=2))
    print(f"Model saved to: {model_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train a regression model for bike-share trip duration.")
    parser.add_argument("--input", type=str, default=os.path.join("data", "processed", "cyclistic_cleaned.csv"),
                        help="Path to cleaned input CSV")
    parser.add_argument("--outdir", type=str, default=os.path.join("analysis", "final_analysis", "outputs"),
                        help="Directory to write outputs")
    parser.add_argument("--test_size", type=float, default=0.2, help="Test size fraction")
    # Lightweight RF defaults to avoid MemoryError; override as needed
    parser.add_argument("--n_estimators", type=int, default=100)
    parser.add_argument("--max_depth", type=int, default=25)
    parser.add_argument("--n_jobs", type=int, default=1)
    parser.add_argument("--max_samples", type=int, default=100000,
                        help="Max samples per tree (int or float fraction). Use smaller values for large datasets")
    parser.add_argument("--min_samples_leaf", type=int, default=3)
    parser.add_argument("--max_train_rows", type=int, default=300000,
                        help="Optional cap on training rows to reduce memory usage")
    parser.add_argument("--top_station_pairs", type=int, default=50, help="Number of top station pairs to keep as categorical")
    parser.add_argument("--weather_csv", type=str, default=None, help="Optional path to hourly weather CSV to merge")
    parser.add_argument("--weather_dt_col", type=str, default="datetime", help="Datetime column name in weather CSV")
    parser.add_argument("--compare_models", action="store_true", help="Run model comparison between RF and HistGradientBoosting")
    args = parser.parse_args()

    ensure_dir(args.outdir)

    # Load data
    df = pd.read_csv(args.input)

    train_model(df, outdir=args.outdir, test_size=args.test_size,
                n_estimators=args.n_estimators, max_depth=args.max_depth,
                n_jobs=args.n_jobs, max_samples=args.max_samples,
                min_samples_leaf=args.min_samples_leaf,
                max_train_rows=args.max_train_rows,
                top_station_pairs=args.top_station_pairs,
                weather_csv=args.weather_csv,
                weather_dt_col=args.weather_dt_col,
                compare_models=args.compare_models)