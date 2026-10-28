# ParkAI

import pandas as pd
from database import get_conn


def predict_occupancy(lot_id, hour):
    """Predict occupancy using historical average data."""

    conn = get_conn()

    df = pd.read_sql_query(
        """
        SELECT occupancy_percent
        FROM history
        WHERE lot_id = ? AND hour = ?
        """,
        conn,
        params=(lot_id, hour)
    )

    conn.close()

    if df.empty:
        return 50  # Neutral fallback if no history exists

    return int(round(df["occupancy_percent"].mean()))


if __name__ == "__main__":
    print("12 baje:", predict_occupancy(1, 12))
    print("8 baje:", predict_occupancy(1, 8))
