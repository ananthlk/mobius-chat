"""Turn telemetry — per-module spans, counts and the wall/llm split.

See spans.py for the contract. The one rule this package exists to enforce:
a span that is written must be readable, and the reader ships with the
writer. A timing emitter nobody reads is the defect this package was built
to find, and it would be absurd for the instrument to be an instance of it.
"""
