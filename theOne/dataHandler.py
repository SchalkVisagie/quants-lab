import os
import sys
from decimal import Decimal
import pandas as pd
import numpy as np
from core.data_sources.clob import CLOBDataSource
from core.data_structures.candles import Candles

def load_candles_and_orderbook(CONNECTOR_NAME, INTERVALS, TRADING_PAIR, root_path=None):
    """
    Loads and merges candles and order book data for a given connector, interval, and trading pair.
    Returns a DataFrame similar to candles_and_ob_df in the notebook.
    """
    if root_path is None:
        root_path = os.path.abspath(os.path.join(os.getcwd(), '../'))

    # Load candles
    clob = CLOBDataSource()
    clob.load_candles_cache(root_path)
    all_candles = clob.get_candles_from_cache(CONNECTOR_NAME, TRADING_PAIR, INTERVALS)
    candles = all_candles
    candlesdf = candles.data.copy()
    candlesdf['timestamp'] = candlesdf['timestamp'].astype(int)

    # Load order book
    folder = os.path.join(root_path, "data/order_book/")
    pattern = "order_book_snapshots"
    files = [
        file for file in os.listdir(folder)
        if CONNECTOR_NAME in file and TRADING_PAIR in file and pattern in file
    ]
    if not files:
        raise FileNotFoundError(f"No order_book files found for {CONNECTOR_NAME} {TRADING_PAIR}")
    dfs = [pd.read_json(os.path.join(folder, file), lines=True) for file in files]
    order_book_df = pd.concat(dfs, ignore_index=True)
    order_book_df.rename(columns={"ts": "timestamp"}, inplace=True)
    order_book_df['timestamp'] = order_book_df['timestamp'].astype(int)

    # Merge on 'timestamp'
    candles_and_ob_df = pd.merge(
        candlesdf,
        order_book_df,
        on='timestamp',
        how='inner',
        suffixes=('_candle', '_orderbook')
    )
    candles_and_ob_df['datetime'] = pd.to_datetime(candles_and_ob_df['timestamp'], unit='s')

    # Add taker_sell_base_volume column
    candles_and_ob_df['taker_sell_base_volume'] = (
        candles_and_ob_df['volume'] - candles_and_ob_df['taker_buy_base_volume']
    )

    check_for_missing_entries(candles_and_ob_df)

    return candles_and_ob_df

def check_for_missing_entries(candles_and_ob_df):
    # Get the range of timestamps
    min_ts = candles_and_ob_df['timestamp'].min()
    max_ts = candles_and_ob_df['timestamp'].max()

    # Create a set of all expected timestamps
    expected_timestamps = set(range(min_ts, max_ts + 1))
    actual_timestamps = set(candles_and_ob_df['timestamp'])

    # Find missing timestamps
    missing_timestamps = expected_timestamps - actual_timestamps

    print(f"Number of missing seconds: {len(missing_timestamps)}")
    if missing_timestamps:
        print(f"Example missing timestamps: {sorted(list(missing_timestamps))[:10]}")
    else:
        print("No missing seconds. Every second has an entry.")