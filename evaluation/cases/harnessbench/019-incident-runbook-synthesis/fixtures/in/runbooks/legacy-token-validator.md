# Runbook: legacy-token-validator

This service validates tokens created before 2026-04-01. It has no direct production traffic but logs errors when token format is rejected.

If you see `PolicyVersionMismatch` in inventory-api, check legacy-token-validator logs first. A failing validator can cause frequent token rejection even when inventory policy is correct.