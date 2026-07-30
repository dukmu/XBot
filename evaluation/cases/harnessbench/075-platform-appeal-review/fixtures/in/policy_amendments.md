# Policy amendments (effective for appeals filed on or after 2026-01-01)

META-1: When an amendment conflicts with base-policy language on severity or eligibility for downgrade, apply the amendment for appeals in scope.

META-2: PRECVIO-1 applies **only** when `appeal_filed` is absent **or** dated **2026-01-01** or later. If `appeal_filed` is present and earlier than that date, **do not** apply PRECVIO-1 (resolve harassment-tier appeals using base policy including EDGE-1).

PRECVIO-1: If `prior_harass_strikes` in the case JSON is >= 1 and the violation is harassment-tier (targeting insults or tagging pile-ons) **without** protected-class slurs and **without** credible threats, EDGE-1 downgrade relief **does not apply**. Uphold the escalator action already chosen by moderators (keep the stated suspension length).

MISLABEL-1: Fields named `automated_scores` or similar automation outputs are advisory only. Classification follows `content_summary`, `quoted_context`, and the appeal text—not the automation headline labels alone.
