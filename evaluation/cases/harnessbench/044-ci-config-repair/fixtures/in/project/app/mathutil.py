def normalize_percent(value):
    if value < 0:
        raise ValueError("percent cannot be negative")
    return round(value / 100, 4)
