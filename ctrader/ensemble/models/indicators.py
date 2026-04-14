"""
GlitchExecutor Ensemble Models
Pure NumPy technical indicators — no external dependencies.
"""
import numpy as np


def sma(prices: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average using numpy cumsum."""
    if len(prices) < period:
        return np.full(len(prices), np.mean(prices))
    
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    result = (cumsum[period:] - cumsum[:-period]) / period
    pad_cumsum = np.cumsum(prices[:period - 1])
    pad = pad_cumsum / np.arange(1, period)
    return np.concatenate([pad, result])


def ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average."""
    result = np.empty_like(prices, dtype=float)
    result[0] = prices[0]
    multiplier = 2.0 / (period + 1)
    
    for i in range(1, len(prices)):
        result[i] = (prices[i] - result[i-1]) * multiplier + result[i-1]
    
    return result


def rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index."""
    if len(prices) < period + 1:
        return np.full(len(prices), 50.0)  # Neutral RSI
    
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    rs_values = []
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 100
        rs_values.append(100 - (100 / (1 + rs)))
    
    # Pad with NaN for the first 'period' values
    padding = np.full(period, np.nan)
    return np.concatenate([padding, np.array(rs_values)])


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range — returns full series."""
    n = len(closes)
    if n < 2:
        return np.full(n, np.nan)
    
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0] if highs[0] != lows[0] else 0.0001
    
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
    
    atr_arr = np.empty(n)
    atr_arr[:period-1] = np.nan
    atr_arr[period-1] = np.mean(tr[:period])
    
    for i in range(period, n):
        atr_arr[i] = (atr_arr[i-1] * (period - 1) + tr[i]) / period
    
    return atr_arr


def adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Average Directional Index — returns ADX series."""
    n = len(closes)
    if n < period * 2 + 1:
        return np.zeros(n)
    
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    
    for i in range(1, n):
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0
    
    atr_vals = atr(highs, lows, closes, period)
    
    # Smooth DM and compute DI
    plus_di = np.zeros(n)
    minus_di = np.zeros(n)
    
    if period < n:
        smooth_plus = np.mean(plus_dm[1:period+1])
        smooth_minus = np.mean(minus_dm[1:period+1])
    else:
        smooth_plus = np.mean(plus_dm[1:])
        smooth_minus = np.mean(minus_dm[1:])
    
    for i in range(period, n):
        smooth_plus = smooth_plus - (smooth_plus / period) + plus_dm[i]
        smooth_minus = smooth_minus - (smooth_minus / period) + minus_dm[i]
        if atr_vals[i] > 0 and not np.isnan(atr_vals[i]):
            plus_di[i] = (smooth_plus / atr_vals[i]) * 100
            minus_di[i] = (smooth_minus / atr_vals[i]) * 100
    
    # DX and ADX
    dx = np.zeros(n)
    for i in range(period, n):
        denom = plus_di[i] + minus_di[i]
        dx[i] = abs(plus_di[i] - minus_di[i]) / denom * 100 if denom > 0 else 0
    
    adx_arr = np.zeros(n)
    start_idx = min(2*period-1, n-1)
    if start_idx < n and start_idx >= period:
        adx_arr[start_idx] = np.mean(dx[period:start_idx+1])
    
    for i in range(start_idx + 1, n):
        adx_arr[i] = (adx_arr[i-1] * (period - 1) + dx[i]) / period
    
    return adx_arr


def bollinger_bands(closes: np.ndarray, period: int = 20, num_std: float = 2.0) -> tuple:
    """Bollinger Bands — returns (upper, middle, lower) arrays."""
    middle = sma(closes, period)
    std = np.array([np.std(closes[max(0,i-period+1):i+1]) for i in range(len(closes))])
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def detect_crossover(fast_line: np.ndarray, slow_line: np.ndarray, lookback: int = 3) -> str:
    """
    Detect if a crossover happened within the last N bars.
    Returns: 'bullish', 'bearish', or 'none'
    """
    if len(fast_line) < lookback + 1 or len(slow_line) < lookback + 1:
        return 'none'
    
    for i in range(1, lookback + 1):
        idx = -i
        prev_idx = -i - 1
        
        if prev_idx < -len(fast_line) or prev_idx < -len(slow_line):
            break
        
        was_below = fast_line[prev_idx] < slow_line[prev_idx]
        is_above = fast_line[idx] > slow_line[idx]
        was_above = fast_line[prev_idx] > slow_line[prev_idx]
        is_below = fast_line[idx] < slow_line[idx]
        
        if was_below and is_above:
            return 'bullish'
        elif was_above and is_below:
            return 'bearish'
    
    return 'none'


def percentile_rank(arr: np.ndarray, value: float) -> float:
    """Calculate percentile rank of value in array (0-100)."""
    if arr is None or len(arr) == 0 or np.all(np.isnan(arr)):
        return 50.0
    clean_arr = arr[~np.isnan(arr)]
    if len(clean_arr) == 0:
        return 50.0
    return (np.sum(clean_arr < value) / len(clean_arr)) * 100


def get_ema_slope(ema_line: np.ndarray, lookback: int = 5) -> str:
    """Get direction of EMA slope."""
    if len(ema_line) < lookback:
        return 'flat'
    
    recent = ema_line[-lookback:]
    if len(recent) < 2:
        return 'flat'
    
    diff = recent[-1] - recent[0]
    threshold = recent[-1] * 0.0001  # 0.01% threshold
    
    if diff > threshold:
        return 'rising'
    elif diff < -threshold:
        return 'falling'
    return 'flat'
