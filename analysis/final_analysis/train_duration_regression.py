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

    return df


def train_model(df: pd.DataFrame, outdir: str, test_size: float = 0.2, random_state: int = 42,
                n_estimators: int = 100, max_depth: int | None = 25, n_jobs: int = 1,
                max_samples: float | int | None = 100_000, min_samples_leaf: int = 3,
                max_train_rows: int | None = 300_000):
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
    ]

    categorical_features: List[str] = [
        "member_casual",
        "rideable_type",
        "time_of_day",  # e.g., Morning/Afternoon/Evening/Night
        "day_type",     # Weekday/Weekend
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
    preds_df = pd.DataFrame({"actual": y_test, "pred": y_pred})
    preds_df.to_csv(os.path.join(outdir, "predictions_sample.csv"), index=False)

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
    args = parser.parse_args()

    ensure_dir(args.outdir)

    # Load data
    df = pd.read_csv(args.input)

    train_model(df, outdir=args.outdir, test_size=args.test_size,
                n_estimators=args.n_estimators, max_depth=args.max_depth,
                n_jobs=args.n_jobs, max_samples=args.max_samples,
                min_samples_leaf=args.min_samples_leaf,
                max_train_rows=args.max_train_rows)