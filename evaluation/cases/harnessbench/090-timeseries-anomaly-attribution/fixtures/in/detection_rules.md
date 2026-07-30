# Detection rules

Use the provided baseline and z-score columns. Suppress low-volume rows because they are known to create false positives.
If a single row crosses thresholds for more than one metric, report one anomaly per metric.

Attribution priority:
1. Matching service and region deployment overlap.
2. Matching third-party incident overlap.
3. Matching marketing/calendar overlap.
4. Unattributed.

Overlapping windows are only evidence for likely attribution, not proof of causation.
