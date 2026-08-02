"""Assertions that gate a build, and the reports a build emits.

Expectations are code, checked automatically instead of relying on someone to
remember a checklist. Two parser defects hid for months because nothing
compared a count to a known-good number; here a mismatch stops the run before
anything is written.
"""

from data_merge.validate.checks import CheckError, CheckResult, Checks
from data_merge.validate.expectations import check_year

__all__ = ["CheckError", "CheckResult", "Checks", "check_year"]
