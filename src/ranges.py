"""Source-range overlap rules."""

def overlap(a, b):
    """Half-open spans; insertion points conflict at either span boundary."""
    if a['start'] == a['end']:
        return b['start'] <= a['start'] <= b['end']
    if b['start'] == b['end']:
        return a['start'] <= b['start'] <= a['end']
    return a['start'] < b['end'] and b['start'] < a['end']
