"""OTLP/HTTP ingest adapter package (spec §4.2.1, phase 5).

Submodules:
  rings   — bounded counted log ring (LogRing)
  config  — shared option validator (validate_otlp_options)
  parse   — OTLP/JSON log parser           (T2)
  logs    — OtlpLogSource                  (T2)
  receiver— ASGI receiver app              (T3)

All code in this package is pure and spawn-free (spec §4.7 tripwire).
No outbound HTTP calls are made from this package.
"""
