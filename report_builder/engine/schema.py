# SPDX-License-Identifier: MIT
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import frappe

VALID_OPERATORS = {
	"Equals",
	"Not Equals",
	"Contains",
	"Does Not Contain",
	"Greater Than",
	"Less Than",
	"Between",
	"In",
	"Not In",
	"Is Set",
	"Is Not Set",
}

VALID_AGGREGATES = {"", "Count", "Sum", "Avg", "Min", "Max"}
VALID_GRANULARITIES = {"", "Day", "Week", "Month", "Quarter", "Year"}
# Filters use a smaller set; "Date" maps to "Day" SQL semantics.
VALID_FILTER_GRANULARITIES = {"", "Date", "Month", "Year", "All"}
VALID_DIRECTIONS = {"Ascending", "Descending"}
VALID_JOIN_TYPES = {"Left Join", "Inner Join"}
VALID_JOIN_OPS = {"=", "!=", ">", ">=", "<", "<="}
VALID_CALC_OPS = {"+", "-", "*", "/"}
VALID_FORMAT_TYPES = {"Number", "Integer", "Currency", "Percent"}

MAX_JOIN_DEPTH = 2
MAX_IN_VALUES = 1000
MAX_PAGE_SIZE = 500
MAX_EXPORT_ROWS = 10000


@dataclass
class JoinConditionCfg:
	left_source: str
	left_path: str
	operator: str
	right_path: str  # evaluated against the related-source root or one child-table hop


@dataclass
class RelatedSourceCfg:
	alias: str
	related_doctype: str
	join_type: str
	conditions: list[JoinConditionCfg] = field(default_factory=list)
	# When True, this related source IS a child doctype joined via
	# parent.name = child.parent. The engine appends a parenttype filter
	# automatically using the source identified by the first condition's
	# left_source. `child_parent_field` carries the Table fieldname on
	# the parent doctype (e.g. "items") for traceability/UI.
	is_child_table: bool = False
	child_parent_field: str = ""


@dataclass
class CalculationCfg:
	alias: str
	label: str
	format_type: str
	expression: dict  # validated structure: {"op": "+|-|*|/", "left": Operand, "right": Operand}


@dataclass
class ColumnCfg:
	field_path: Optional[str] = None
	source: str = ""
	calculation_alias: Optional[str] = None
	label: Optional[str] = None
	fieldtype: Optional[str] = None
	aggregate: str = ""
	width: Optional[int] = None

	@property
	def is_calculation(self) -> bool:
		return bool(self.calculation_alias)


@dataclass
class FilterCfg:
	field_path: str
	operator: str
	source: str = ""
	fieldtype: Optional[str] = None
	value: Optional[str] = None
	value_to: Optional[str] = None
	value_list: Optional[str] = None
	granularity: str = ""
	is_runtime: bool = False


@dataclass
class GroupByCfg:
	field_path: str
	source: str = ""
	fieldtype: Optional[str] = None
	granularity: str = ""


@dataclass
class SortCfg:
	field_path: str
	source: str = ""
	direction: str = "Ascending"


@dataclass
class Config:
	base_doctype: str
	related_sources: list[RelatedSourceCfg] = field(default_factory=list)
	calculations: list[CalculationCfg] = field(default_factory=list)
	columns: list[ColumnCfg] = field(default_factory=list)
	filters: list[FilterCfg] = field(default_factory=list)
	group_by: list[GroupByCfg] = field(default_factory=list)
	sort: list[SortCfg] = field(default_factory=list)


def _coerce_str(val) -> str:
	if val is None:
		return ""
	return str(val).strip()


def _parse_json_field(raw, default):
	if raw in (None, ""):
		return default
	if isinstance(raw, (list, dict)):
		return raw
	if isinstance(raw, str):
		try:
			return json.loads(raw)
		except json.JSONDecodeError:
			return default
	return default


def parse(config) -> Config:
	if isinstance(config, str):
		try:
			config = json.loads(config)
		except json.JSONDecodeError:
			frappe.throw(frappe._("Report configuration is not valid JSON."))

	if not isinstance(config, dict):
		frappe.throw(frappe._("Report configuration must be an object."))

	base_doctype = _coerce_str(config.get("base_doctype"))
	if not base_doctype:
		frappe.throw(frappe._("Please choose a Data Source."))

	related = [
		_parse_related_source(rs) for rs in (config.get("related_sources") or []) if rs
	]
	_check_unique_aliases(related)

	calculations = [
		_parse_calculation(c) for c in (config.get("calculations") or []) if c
	]
	_check_unique_calc_aliases(calculations)

	columns = [_parse_column(c) for c in (config.get("columns") or []) if c]
	filters = [_parse_filter(f) for f in (config.get("filters") or []) if f]
	group_by = [_parse_group(g) for g in (config.get("group_by") or []) if g]
	sort = [_parse_sort(s) for s in (config.get("sort") or []) if s]

	return Config(
		base_doctype=base_doctype,
		related_sources=related,
		calculations=calculations,
		columns=columns,
		filters=filters,
		group_by=group_by,
		sort=sort,
	)


def _check_unique_aliases(related: list[RelatedSourceCfg]) -> None:
	seen: set[str] = set()
	for rs in related:
		if rs.alias in seen:
			frappe.throw(frappe._("Duplicate related-source alias: {0}").format(rs.alias))
		seen.add(rs.alias)


def _check_unique_calc_aliases(calcs: list[CalculationCfg]) -> None:
	seen: set[str] = set()
	for c in calcs:
		if c.alias in seen:
			frappe.throw(frappe._("Duplicate calculation alias: {0}").format(c.alias))
		seen.add(c.alias)


def _parse_related_source(raw: dict) -> RelatedSourceCfg:
	alias = _coerce_str(raw.get("alias"))
	if not alias:
		frappe.throw(frappe._("A related DocType is missing its alias."))
	if not _is_safe_alias(alias):
		frappe.throw(
			frappe._("Alias {0} is invalid. Use letters, numbers, and underscores only.").format(alias)
		)
	related_dt = _coerce_str(raw.get("related_doctype"))
	if not related_dt:
		frappe.throw(frappe._("Related source {0} is missing its DocType.").format(alias))
	join_type = _coerce_str(raw.get("join_type")) or "Left Join"
	if join_type not in VALID_JOIN_TYPES:
		frappe.throw(frappe._("Unknown join type: {0}").format(join_type))

	conditions_raw = raw.get("conditions")
	if isinstance(conditions_raw, str):
		conditions_raw = _parse_json_field(conditions_raw, [])
	conditions_raw = conditions_raw or []
	if not isinstance(conditions_raw, list):
		frappe.throw(
			frappe._("Conditions for {0} must be a list of match rules.").format(alias)
		)
	conditions = [_parse_join_condition(alias, c) for c in conditions_raw if c]
	if not conditions:
		frappe.throw(
			frappe._("Add at least one match condition for related DocType {0}.").format(alias)
		)
	return RelatedSourceCfg(
		alias=alias,
		related_doctype=related_dt,
		join_type=join_type,
		conditions=conditions,
		is_child_table=bool(raw.get("is_child_table")),
		child_parent_field=_coerce_str(raw.get("child_parent_field")),
	)


def _parse_join_condition(alias: str, raw: dict) -> JoinConditionCfg:
	if not isinstance(raw, dict):
		frappe.throw(frappe._("A match condition for {0} is malformed.").format(alias))
	op = _coerce_str(raw.get("operator")) or "="
	if op not in VALID_JOIN_OPS:
		frappe.throw(frappe._("Unknown match operator: {0}").format(op))
	left_path = _coerce_str(raw.get("left_path"))
	right_path = _coerce_str(raw.get("right_path"))
	if not left_path or not right_path:
		frappe.throw(
			frappe._("A match condition for {0} is missing its fields.").format(alias)
		)
	return JoinConditionCfg(
		left_source=_coerce_str(raw.get("left_source")),
		left_path=left_path,
		operator=op,
		right_path=right_path,
	)


def _parse_calculation(raw: dict) -> CalculationCfg:
	alias = _coerce_str(raw.get("alias"))
	if not alias:
		frappe.throw(frappe._("A calculation is missing its name."))
	if not _is_safe_alias(alias):
		frappe.throw(
			frappe._("Calculation name {0} is invalid. Use letters, numbers, and underscores.").format(alias)
		)
	label = _coerce_str(raw.get("label")) or alias
	fmt = _coerce_str(raw.get("format_type")) or "Number"
	if fmt not in VALID_FORMAT_TYPES:
		frappe.throw(frappe._("Unknown calculation format: {0}").format(fmt))

	expr = raw.get("expression")
	if isinstance(expr, str):
		expr = _parse_json_field(expr, None)
	if not isinstance(expr, dict):
		frappe.throw(
			frappe._("Calculation {0} has no expression.").format(alias)
		)
	expression = _validate_expression(expr, alias)
	return CalculationCfg(
		alias=alias,
		label=label,
		format_type=fmt,
		expression=expression,
	)


def _validate_expression(expr: dict, calc_alias: str) -> dict:
	op = _coerce_str(expr.get("op"))
	if op not in VALID_CALC_OPS:
		frappe.throw(
			frappe._("Calculation {0} uses an unknown operator: {1}").format(calc_alias, op)
		)
	left = _validate_operand(expr.get("left"), calc_alias)
	right = _validate_operand(expr.get("right"), calc_alias)
	return {"op": op, "left": left, "right": right}


def _validate_operand(raw, calc_alias: str) -> dict:
	if not isinstance(raw, dict):
		frappe.throw(frappe._("Calculation {0} has a malformed operand.").format(calc_alias))
	t = _coerce_str(raw.get("type"))
	if t == "field":
		path = _coerce_str(raw.get("path"))
		if not path:
			frappe.throw(frappe._("Calculation {0} references a missing field.").format(calc_alias))
		return {
			"type": "field",
			"source": _coerce_str(raw.get("source")),
			"path": path,
		}
	if t == "const":
		try:
			value = float(raw.get("value"))
		except (TypeError, ValueError):
			frappe.throw(
				frappe._("Calculation {0} has a non-numeric constant.").format(calc_alias)
			)
		return {"type": "const", "value": value}
	frappe.throw(frappe._("Calculation {0} uses an unknown operand.").format(calc_alias))


def _parse_column(raw: dict) -> ColumnCfg:
	visibility = _parse_json_field(raw.get("visibility_rule"), None) if raw.get("visibility_rule") else None
	formatting = _parse_json_field(raw.get("format_rules"), []) if raw.get("format_rules") else []

	calc_alias = _coerce_str(raw.get("calculation_alias")) or None
	if calc_alias:
		col = ColumnCfg(
			calculation_alias=calc_alias,
			label=_coerce_str(raw.get("label")) or None,
			fieldtype=_coerce_str(raw.get("fieldtype")) or None,
			aggregate="",
			width=_safe_int(raw.get("width")),
		)
		col._visibility_rule = visibility
		col._format_rules = formatting
		return col
	path = _coerce_str(raw.get("field_path"))
	if not path:
		frappe.throw(frappe._("A column is missing its field."))
	aggregate = _coerce_str(raw.get("aggregate"))
	if aggregate not in VALID_AGGREGATES:
		frappe.throw(frappe._("Unknown summary type: {0}").format(aggregate))
	col = ColumnCfg(
		field_path=path,
		source=_coerce_str(raw.get("source")),
		label=_coerce_str(raw.get("label")) or None,
		fieldtype=_coerce_str(raw.get("fieldtype")) or None,
		aggregate=aggregate,
		width=_safe_int(raw.get("width")),
	)
	col._visibility_rule = visibility
	col._format_rules = formatting
	return col


def _parse_filter(raw: dict) -> FilterCfg:
	path = _coerce_str(raw.get("field_path"))
	if not path:
		frappe.throw(frappe._("A filter is missing its field."))
	op = _coerce_str(raw.get("operator"))
	if op not in VALID_OPERATORS:
		frappe.throw(frappe._("Unknown filter condition: {0}").format(op))
	gran = _coerce_str(raw.get("granularity"))
	if gran not in VALID_FILTER_GRANULARITIES:
		frappe.throw(frappe._("Unknown granularity: {0}").format(gran))
	return FilterCfg(
		field_path=path,
		operator=op,
		source=_coerce_str(raw.get("source")),
		fieldtype=_coerce_str(raw.get("fieldtype")) or None,
		value=_coerce_str(raw.get("value")) if raw.get("value") is not None else None,
		value_to=_coerce_str(raw.get("value_to")) if raw.get("value_to") is not None else None,
		value_list=_coerce_str(raw.get("value_list")) if raw.get("value_list") is not None else None,
		granularity=gran,
		is_runtime=bool(raw.get("is_runtime")),
	)


def _parse_group(raw: dict) -> GroupByCfg:
	path = _coerce_str(raw.get("field_path"))
	if not path:
		frappe.throw(frappe._("A group has no field."))
	gran = _coerce_str(raw.get("granularity"))
	if gran not in VALID_GRANULARITIES:
		frappe.throw(frappe._("Unknown granularity: {0}").format(gran))
	return GroupByCfg(
		field_path=path,
		source=_coerce_str(raw.get("source")),
		fieldtype=_coerce_str(raw.get("fieldtype")) or None,
		granularity=gran,
	)


def _parse_sort(raw: dict) -> SortCfg:
	path = _coerce_str(raw.get("field_path"))
	if not path:
		frappe.throw(frappe._("A sort entry is missing its field."))
	direction = _coerce_str(raw.get("direction")) or "Ascending"
	if direction not in VALID_DIRECTIONS:
		frappe.throw(frappe._("Unknown sort direction: {0}").format(direction))
	return SortCfg(
		field_path=path,
		source=_coerce_str(raw.get("source")),
		direction=direction,
	)


def _safe_int(value):
	if value in (None, "", 0):
		return None
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


_SAFE_ALIAS_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _is_safe_alias(alias: str) -> bool:
	if not alias:
		return False
	if alias[0].isdigit():
		return False
	return all(ch in _SAFE_ALIAS_CHARS for ch in alias)
