# SPDX-License-Identifier: MIT
from typing import Callable

import frappe
from frappe.utils import cint, cstr, flt, get_datetime, getdate

from report_builder.engine.schema import MAX_IN_VALUES

NUMERIC_FIELDTYPES = {"Int", "Float", "Currency", "Percent"}
INT_FIELDTYPES = {"Int", "Check"}
DATE_FIELDTYPES = {"Date", "Datetime"}


def _cast(value, fieldtype):
	if value is None:
		return None
	if fieldtype in INT_FIELDTYPES:
		return cint(value)
	if fieldtype in NUMERIC_FIELDTYPES:
		return flt(value)
	if fieldtype == "Date":
		return getdate(value)
	if fieldtype == "Datetime":
		return get_datetime(value)
	return cstr(value)


def _detect_value_gran(sample: str) -> str:
	"""Detect the date-bucket granularity from the user-provided value format.
	Returns "Year" (YYYY), "Month" (YYYY-MM), "Date" (YYYY-MM-DD), or "" if
	neither matched. Used by the "All" filter granularity which lets a single
	row accept any of the three shapes."""
	s = (sample or "").strip()
	if not s:
		return ""
	if len(s) == 4 and s.isdigit():
		return "Year"
	if (
		len(s) == 7
		and s[4] == "-"
		and s[:4].isdigit()
		and s[5:].isdigit()
	):
		return "Month"
	if (
		len(s) == 10
		and s[4] == "-"
		and s[7] == "-"
		and s[:4].isdigit()
		and s[5:7].isdigit()
		and s[8:].isdigit()
	):
		return "Date"
	return ""


def _cast_for_filter(value, fc):
	"""Cast a filter value for comparison.

	If `fc.granularity` is set on a Date/Datetime field, the field expression
	will be wrapped in `date_bucket(...)` (see `to_predicate`), which produces
	either an integer (Year → YEAR()) or a string (Month/Date → DATE_FORMAT).
	The value must be cast to match.
	"""
	gran = getattr(fc, "granularity", "") or ""
	if gran == "All" and fc.fieldtype in DATE_FIELDTYPES:
		# `to_predicate` resolves "All" → a concrete granularity before this
		# is reached, but if a caller hits this path directly we fall back to
		# detecting from the value itself so a stray "All" doesn't crash.
		gran = _detect_value_gran(value) or ""
	if gran and (fc.fieldtype in DATE_FIELDTYPES):
		if value is None:
			return None
		if gran == "Year":
			return cint(value)
		return cstr(value)
	return _cast(value, fc.fieldtype or "Data")


def _escape_like(value: str) -> str:
	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _split_list_for_filter(value_list: str, fc) -> list:
	if not value_list:
		return []
	parts = [p.strip() for p in value_list.split(",") if p.strip()]
	if len(parts) > MAX_IN_VALUES:
		frappe.throw(frappe._("Too many values in filter (max {0}).").format(MAX_IN_VALUES))
	return [_cast_for_filter(p, fc) for p in parts]


def _equals(field, f, fieldtype):
	return field == _cast_for_filter(f.value, f)


def _not_equals(field, f, fieldtype):
	return field != _cast_for_filter(f.value, f)


def _contains(field, f, fieldtype):
	pattern = "%" + _escape_like(cstr(f.value or "")) + "%"
	return field.like(pattern)


def _not_contains(field, f, fieldtype):
	pattern = "%" + _escape_like(cstr(f.value or "")) + "%"
	return field.not_like(pattern)


def _gt(field, f, fieldtype):
	return field > _cast_for_filter(f.value, f)


def _lt(field, f, fieldtype):
	return field < _cast_for_filter(f.value, f)


def _between(field, f, fieldtype):
	low = _cast_for_filter(f.value, f)
	high = _cast_for_filter(f.value_to, f)
	return field.between(low, high)


def _in(field, f, fieldtype):
	values = _split_list_for_filter(f.value_list or "", f)
	if not values:
		frappe.throw(frappe._("Filter has no values."))
	return field.isin(values)


def _not_in(field, f, fieldtype):
	values = _split_list_for_filter(f.value_list or "", f)
	if not values:
		frappe.throw(frappe._("Filter has no values."))
	return field.notin(values)


def _is_set(field, f, fieldtype):
	return field.isnotnull() & (field != "")


def _is_not_set(field, f, fieldtype):
	return field.isnull() | (field == "")


OPERATORS: dict[str, Callable] = {
	"Equals": _equals,
	"Not Equals": _not_equals,
	"Contains": _contains,
	"Does Not Contain": _not_contains,
	"Greater Than": _gt,
	"Less Than": _lt,
	"Between": _between,
	"In": _in,
	"Not In": _not_in,
	"Is Set": _is_set,
	"Is Not Set": _is_not_set,
}


def to_predicate(filter_cfg, col_refs, key=None):
	op = OPERATORS.get(filter_cfg.operator)
	if op is None:
		frappe.throw(frappe._("Unknown filter condition: {0}").format(filter_cfg.operator))
	if key is None:
		key = (getattr(filter_cfg, "source", "") or "", filter_cfg.field_path)
	field = col_refs[key]

	# Date granularity: wrap the field in DATE_FORMAT/YEAR so the comparison
	# happens against the bucketed value. Only meaningful for Date/Datetime
	# fields and only when filter_cfg.granularity is non-empty.
	gran = getattr(filter_cfg, "granularity", "") or ""
	if gran == "All" and filter_cfg.fieldtype in DATE_FIELDTYPES:
		# "All" lets one filter row accept any of YYYY / YYYY-MM / YYYY-MM-DD.
		# Pick the bucket from whatever the user typed, then mutate
		# filter_cfg.granularity so _cast_for_filter casts every value to the
		# same shape (the field wrap and the value cast must agree).
		sample = filter_cfg.value or filter_cfg.value_to or ""
		if not sample and filter_cfg.value_list:
			parts = [p.strip() for p in (filter_cfg.value_list or "").split(",") if p.strip()]
			sample = parts[0] if parts else ""
		detected = _detect_value_gran(sample)
		# Empty / unrecognised value → silently skip the filter. The UI shows
		# the expected format under the input box, so we don't pop a notif.
		if filter_cfg.operator not in ("Is Set", "Is Not Set") and not detected:
			return None
		gran = detected or ""
		filter_cfg.granularity = gran  # so _cast_for_filter sees the resolved gran
	if gran and (filter_cfg.fieldtype in DATE_FIELDTYPES):
		# Contains / Does Not Contain don't make sense on bucketed dates;
		# fall through with the unwrapped field and let the user choose
		# Equals/Between for date bucketing.
		if filter_cfg.operator not in ("Contains", "Does Not Contain"):
			from report_builder.engine.aggregations import date_bucket

			field = date_bucket(field, gran)

	return op(field, filter_cfg, filter_cfg.fieldtype or "Data")
