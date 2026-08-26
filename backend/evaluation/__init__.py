"""Measurement harnesses that are not part of the serving path.

Story S4.4 (the judge evaluation) lives here, and story S7.2's RAGAS run will
too. Kept out of ``engines/`` deliberately: these modules *spend* money to
produce a number for the README, they are run from a command line rather than
from an endpoint, and nothing the API serves may import them.
"""
