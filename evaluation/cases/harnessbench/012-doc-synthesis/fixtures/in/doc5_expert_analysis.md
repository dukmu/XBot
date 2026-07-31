# Independent security expert analysis

**Author: Dr. Alice Chen, independent researcher**  
**Date: 2026-03-17**

## Summary
Using public indicators, the official statement, and third-party telemetry, I assess that **real-world affected users likely fall between 50k–150k**, and **payment-card leakage is not substantiated**.

## Timeline
- March 14: earliest exploitation seen in logs  
- March 15: internal anomaly detected  
- March 16: official statement published  

## Volume
NewsSite A’s “half billion” likely confuses total registered users with affected users. NewsSite B’s “5,000” is likely too low: dark-web samples show **~80k distinct real-looking emails**, so a conservative estimate is **~100k affected accounts**.

## Data types
No verified payment-card dumps; mostly emails and bcrypt hashes. Social claims of “plaintext passwords everywhere” look unreliable or mixed with other dumps.

## Credibility ranking
1. Official statement (doc4) — authoritative but may undercount  
2. Expert analysis (doc5) — data-grounded  
3. NewsSite A (doc1) — partly accurate, sensationalized  
4. NewsSite B (doc2) — company-friendly, weaker sourcing  
5. Social media (doc3) — mostly noise  

## Recommendation
Rotate passwords and enable 2FA as hygiene; avoid panic. Regulators should demand an independent audit.
