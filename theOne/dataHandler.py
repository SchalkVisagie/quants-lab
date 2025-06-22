import os
import sys
from decimal import Decimal
import pandas as pd
import numpy as np
from core.data_sources.clob import CLOBDataSource
from core.data_structures.candles import Candles

def load_candles_and_orderbook(CONNECTOR_NAME, INTERVALS, TRADING_PAIR, start_time=None, end_time=None, root_path=None):
    """
    Loads and merges candles and order book data for a given connector, interval, and trading pair.
    
    Args:
        CONNECTOR_NAME: Name of the connector (e.g., 'binance')
        INTERVALS: Time interval (e.g., '1s')
        TRADING_PAIR: Trading pair (e.g., 'POL-USDT')
        start_time: Unix timestamp for start time (optional)
        end_time: Unix timestamp for end time (optional)
        root_path: Root path for data files (optional)
    
    Returns:
        DataFrame with merged candles and orderbook data for the specified time range
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

    # Filter candles by time range if specified
    if start_time is not None or end_time is not None:
        if start_time is not None:
            candlesdf = candlesdf[candlesdf['timestamp'] >= start_time]
        if end_time is not None:
            candlesdf = candlesdf[candlesdf['timestamp'] <= end_time]
        
        print(f"Filtered candles to time range: {start_time} to {end_time}")
        print(f"Candles data shape after filtering: {candlesdf.shape}")

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

    # Filter orderbook by time range if specified
    if start_time is not None or end_time is not None:
        if start_time is not None:
            order_book_df = order_book_df[order_book_df['timestamp'] >= start_time]
        if end_time is not None:
            order_book_df = order_book_df[order_book_df['timestamp'] <= end_time]
        
        print(f"Filtered orderbook to time range: {start_time} to {end_time}")
        print(f"Orderbook data shape after filtering: {order_book_df.shape}")

    # Use left join to keep all candle timestamps
    candles_and_ob_df = pd.merge(
        candlesdf,
        order_book_df,
        on='timestamp',
        how='left',
        suffixes=('_candle', '_orderbook')
    )
    
    # Forward fill missing orderbook data (bids and asks)
    candles_and_ob_df['bids'] = candles_and_ob_df['bids'].fillna(method='ffill')
    candles_and_ob_df['asks'] = candles_and_ob_df['asks'].fillna(method='ffill')
    
    candles_and_ob_df['datetime'] = pd.to_datetime(candles_and_ob_df['timestamp'], unit='s')

    # Add taker_sell_base_volume column
    candles_and_ob_df['taker_sell_base_volume'] = (
        candles_and_ob_df['volume'] - candles_and_ob_df['taker_buy_base_volume']
    )

    print(f"Final merged data shape: {candles_and_ob_df.shape}")
    check_for_missing_entries_v2(candles_and_ob_df)

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

def check_for_missing_entries_v2(candles_and_ob_df):
    """
    Comprehensive data integrity check for candles and orderbook data.
    Checks for missing timestamps, duplicate timestamps, and missing orderbook data.
    """
    print("=== DATA INTEGRITY CHECK ===")
    
    # Basic validation
    if candles_and_ob_df.empty:
        print("❌ DataFrame is empty!")
        return None
    
    # Basic info
    total_rows = len(candles_and_ob_df)
    min_ts = candles_and_ob_df['timestamp'].min()
    max_ts = candles_and_ob_df['timestamp'].max()
    time_span = max_ts - min_ts + 1  # +1 because we include both endpoints
    
    print(f"Total rows: {total_rows}")
    print(f"Time range: {min_ts} to {max_ts}")
    print(f"Expected time span: {time_span} seconds")
    
    # Check for missing timestamps (gaps in sequence)
    expected_timestamps = set(range(min_ts, max_ts + 1))
    actual_timestamps = set(candles_and_ob_df['timestamp'])
    missing_timestamps = expected_timestamps - actual_timestamps
    
    print(f"\n--- TIMESTAMP INTEGRITY ---")
    print(f"Missing timestamps: {len(missing_timestamps)}")
    if missing_timestamps:
        sorted_missing = sorted(list(missing_timestamps))
        print(f"First 10 missing: {sorted_missing[:10]}")
        if len(missing_timestamps) > 10:
            print(f"Last 10 missing: {sorted_missing[-10:]}")
        
        # Check for gaps (consecutive missing timestamps)
        gaps = []
        current_gap = []
        for ts in sorted_missing:
            if not current_gap or ts == current_gap[-1] + 1:
                current_gap.append(ts)
            else:
                if len(current_gap) > 1:
                    gaps.append((current_gap[0], current_gap[-1], len(current_gap)))
                current_gap = [ts]
        if len(current_gap) > 1:
            gaps.append((current_gap[0], current_gap[-1], len(current_gap)))
        
        if gaps:
            print(f"Found {len(gaps)} gaps of consecutive missing timestamps:")
            for start, end, length in gaps[:5]:  # Show first 5 gaps
                print(f"  Gap: {start} to {end} ({length} seconds)")
        else:
            print("Missing timestamps are isolated (no consecutive gaps)")
    
    # Check for duplicate timestamps
    duplicates = candles_and_ob_df['timestamp'].duplicated().sum()
    print(f"Duplicate timestamps: {duplicates}")
    
    # Check for missing orderbook data
    print(f"\n--- ORDERBOOK DATA INTEGRITY ---")
    missing_bids = candles_and_ob_df['bids'].isna().sum()
    missing_asks = candles_and_ob_df['asks'].isna().sum()
    print(f"Missing bids: {missing_bids}")
    print(f"Missing asks: {missing_asks}")
    
    # Check candle data completeness
    print(f"\n--- CANDLE DATA INTEGRITY ---")
    candle_columns = ['open', 'high', 'low', 'close', 'volume']
    for col in candle_columns:
        if col in candles_and_ob_df.columns:
            missing = candles_and_ob_df[col].isna().sum()
            print(f"Missing {col}: {missing}")
    
    # Summary
    print(f"\n--- SUMMARY ---")
    if len(missing_timestamps) == 0 and duplicates == 0:
        print("✅ Timestamp integrity: PERFECT")
    else:
        print(f"❌ Timestamp integrity: {len(missing_timestamps)} missing, {duplicates} duplicates")
    
    if missing_bids == 0 and missing_asks == 0:
        print("✅ Orderbook integrity: PERFECT")
    else:
        print(f"❌ Orderbook integrity: {missing_bids} missing bids, {missing_asks} missing asks")
    
    print("=" * 30)
    
    return {
        'missing_timestamps': missing_timestamps,
        'duplicate_timestamps': duplicates,
        'missing_bids': missing_bids,
        'missing_asks': missing_asks
    }