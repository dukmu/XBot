# Operations Notes

HarborPilot v2 entered production on 2025-02-14 for the North Pier berth scheduling area. The release manager marked the launch as complete after the 18:30 UTC smoke test.

Raw sensor logs are retained for 18 months before being reduced to monthly aggregate tables. Aggregates are retained indefinitely for trend reports.

The recurring maintenance window is Sunday 02:00-04:00 UTC. During that window, berth prediction alerts are suppressed but audit logging stays enabled.
