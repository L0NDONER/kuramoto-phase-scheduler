"""grid_freq_telemetry.py — real UK national grid frequency, read from
grid_twin's live_ingest.py output (~/claude/grid_twin/data/live/freq.csv,
appended every 5 min by grid-live-ingest.timer against the real live
Elexon/BMRS API -- confirmed live 2026-08-17, ~30s behind wall-clock at
fetch time).

Reads the file's last row, not a live API call of its own -- live_ingest
already does the real fetching responsibly (checkpointed, deduped); a
0.5s-tick oscillator polling the same API directly would just duplicate
that work for data that only changes every 5 minutes at best (freq
itself publishes at 15s resolution, but a fresh CSV read here still only
picks up whatever the last completed live_ingest run wrote). Between
writes, this correctly reports the same last-known value -- honest
zero-order-hold, not fabricated sub-5-min variation.
"""
import csv
import os

FREQ_CSV = os.path.expanduser("~/claude/grid_twin/data/live/freq.csv")


def read_last_frequency():
    """Returns (frequency_hz, timestamp_str) from the last row of
    freq.csv, or (None, None) if the file doesn't exist yet or is
    empty -- caller must treat that as "no reading," not as 50.0Hz."""
    if not os.path.exists(FREQ_CSV):
        return None, None
    last_row = None
    with open(FREQ_CSV) as f:
        for row in csv.DictReader(f):
            last_row = row
    if last_row is None:
        return None, None
    return float(last_row["frequency_hz"]), last_row["timestamp"]
