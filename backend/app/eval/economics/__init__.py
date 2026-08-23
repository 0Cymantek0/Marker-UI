"""Economics envelope evidence (masterplan invariants 57/58).

Machine-readable scale/cost envelopes for local and industrial profiles
plus the visual OFF/ON operational-load comparison. The contract in
``contract.py`` separates measured values from unavailable and
not-applicable dimensions; ``validate.py`` fails closed on the evidence
honesty rules (zero-as-unavailable, unitless numbers, ratios without
matched-window raw counters, percentiles without sample counts).
"""
