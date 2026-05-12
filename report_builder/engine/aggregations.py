# SPDX-License-Identifier: MIT
import frappe
from frappe.query_builder.functions import Avg, Count, Max, Min, Sum
from pypika import CustomFunction
from pypika.terms import LiteralValue

DateFormat = CustomFunction("DATE_FORMAT", ["date", "format"])
YearFn = CustomFunction("YEAR", ["date"])
QuarterFn = CustomFunction("QUARTER", ["date"])
YearWeekFn = CustomFunction("YEARWEEK", ["date", "mode"])
ConcatFn = CustomFunction("CONCAT", ["a", "b", "c"])

AGGREGATES = {
	"Count": Count,
	"Sum": Sum,
	"Avg": Avg,
	"Min": Min,
	"Max": Max,
}


def aggregate_expr(name: str, field):
	fn = AGGREGATES.get(name)
	if fn is None:
		frappe.throw(frappe._("Unknown summary type: {0}").format(name))
	if name == "Count":
		return Count(field)
	return fn(field)


def date_bucket(field, granularity: str):
	# Note: %% is required to escape percent through pymysql's argument formatter.
	# "Date" is a UI alias for "Day" — same SQL semantics. Group By stores
	# "Day" historically; Filters store "Date".
	if granularity in ("Day", "Date"):
		return DateFormat(field, LiteralValue("'%%Y-%%m-%%d'"))
	if granularity == "Week":
		return YearWeekFn(field, LiteralValue("3"))
	if granularity == "Month":
		return DateFormat(field, LiteralValue("'%%Y-%%m'"))
	if granularity == "Quarter":
		return ConcatFn(YearFn(field), LiteralValue("'-Q'"), QuarterFn(field))
	if granularity == "Year":
		return YearFn(field)
	return field


def group_expr(group_cfg, col_refs):
	key = (getattr(group_cfg, "source", "") or "", group_cfg.field_path)
	field = col_refs[key]
	if group_cfg.granularity:
		return date_bucket(field, group_cfg.granularity)
	return field


def column_select(col, col_refs, group_buckets):
	"""Build the SELECT expression for a column.

	group_buckets: dict[path -> granularity] for group-by entries; columns whose
	path matches must use the same bucket so the SELECT and GROUP BY agree.
	"""
	field = col_refs[col.field_path]
	if col.aggregate:
		return aggregate_expr(col.aggregate, field)
	if col.field_path in group_buckets and group_buckets[col.field_path]:
		return date_bucket(field, group_buckets[col.field_path])
	return field
