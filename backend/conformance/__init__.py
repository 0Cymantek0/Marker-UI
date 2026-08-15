"""Standalone canonical-identity conformance suite.

Runs with nothing but the Python standard library and pytest: no app
dependencies, no database, no model downloads. Collected separately
from ``backend/tests`` so a lightweight cross-platform CI matrix can
execute it on every supported OS/runtime combination.
"""
