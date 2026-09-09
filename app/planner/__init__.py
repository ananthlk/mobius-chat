"""Planner package.

P2a (2026-09-09): emptied. This module used to re-export ``parse`` from
planner.parser and ``Plan``/``SubQuestion`` from planner.schemas.

The re-export is why the P1a sweep missed this whole subtree: asking "who
imports parser.py?" answered "__init__.py" and looked live, when in fact
nothing consumed the re-export. Import from the defining module directly
(``from app.planner.schemas import Plan``) so the import graph stays
readable to a grep.
"""
