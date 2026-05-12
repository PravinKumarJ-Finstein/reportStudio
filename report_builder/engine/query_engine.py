# SPDX-License-Identifier: MIT
import time
from dataclasses import dataclass

import frappe
from frappe.query_builder.functions import Count
from frappe.utils import cint, flt
from pypika import Order
from pypika.terms import LiteralValue

from report_builder.engine import (
	aggregations,
	filter_ops,
	join_resolver,
	meta_validator,
	pagination,
	schema,
)


@dataclass
class QueryResult:
	columns: list[dict]
	rows: list[list]
	total: int
	page: int
	page_size: int
	execution_ms: float


def _column_meta(cfg, refs, calc_index) -> list[dict]:
	out = []
	for col in cfg.columns:
		visibility_rule = getattr(col, "_visibility_rule", None)
		format_rules = getattr(col, "_format_rules", None) or []
		if col.is_calculation:
			calc = calc_index.get(col.calculation_alias)
			fieldtype_map = {
				"Currency": "Currency",
				"Percent": "Percent",
				"Integer": "Int",
				"Number": "Float",
			}
			out.append(
				{
					"fieldname": f"calc:{col.calculation_alias}",
					"label": col.label or (calc.label if calc else col.calculation_alias),
					"fieldtype": fieldtype_map.get(
						getattr(calc, "format_type", "Number"), "Float"
					),
					"aggregate": None,
					"width": col.width or None,
					"visibility_rule": visibility_rule,
					"format_rules": format_rules,
				}
			)
			continue
		ref = refs[(col.source or "", col.field_path)]
		label = col.label or _humanize_label(ref.label, col)
		fieldtype = col.fieldtype or ref.fieldtype
		if col.aggregate == "Count":
			fieldtype = "Int"
		elif col.aggregate in ("Sum", "Avg") and fieldtype not in ("Currency",):
			fieldtype = "Float"
		fname = (
			f"{col.source}:{col.field_path}"
			if col.source
			else col.field_path
		)
		# `link_doctype` powers click-to-open-form on the frontend.
		# - Link fieldtype: link to its target doctype.
		# - The terminal segment is "name": link to the doctype owning that name.
		link_doctype = None
		if not col.aggregate:
			if ref.fieldtype == "Link" and ref.options:
				link_doctype = ref.options
			elif ref.fieldname == "name" and ref.terminal_doctype:
				link_doctype = ref.terminal_doctype
		out.append(
			{
				"fieldname": fname,
				"label": label,
				"fieldtype": fieldtype,
				"options": ref.options,
				"link_doctype": link_doctype,
				"aggregate": col.aggregate or None,
				"width": col.width or None,
				"visibility_rule": visibility_rule,
				"format_rules": format_rules,
			}
		)
	return out


def _humanize_label(meta_label: str, col) -> str:
	if col.aggregate:
		return f"{meta_label} ({col.aggregate})"
	return meta_label


def _permission_match_conditions(doctype: str):
	if frappe.session.user == "Administrator":
		return None
	try:
		cond = frappe.build_match_conditions(doctype, as_condition=True) or ""
	except Exception:
		return None
	cond = cond.strip()
	if not cond:
		return None
	return cond


def _apply_link_joins(q, link_joins):
	for j in link_joins:
		on_clause = j.on_clause if j.on_clause is not None else (
			j.parent_table[j.link_fieldname] == j.child_table.name
		)
		q = q.left_join(j.child_table).on(on_clause)
	return q


def _apply_related_joins(q, related_joins):
	for spec in related_joins:
		for helper in spec.pre_helper_joins:
			if helper.join_type == "inner":
				q = q.inner_join(helper.table).on(helper.on_clause)
			else:
				q = q.left_join(helper.table).on(helper.on_clause)
		on_clause = None
		for cond in spec.conditions:
			on_clause = cond if on_clause is None else on_clause & cond
		if on_clause is None:
			continue
		if spec.join_type == "inner":
			q = q.inner_join(spec.right_table).on(on_clause)
		else:
			q = q.left_join(spec.right_table).on(on_clause)
		for helper in spec.post_helper_joins:
			if helper.join_type == "inner":
				q = q.inner_join(helper.table).on(helper.on_clause)
			else:
				q = q.left_join(helper.table).on(helper.on_clause)
	return q


def _runtime_filter_empty(f) -> bool:
	op = f.operator
	if op in ("Is Set", "Is Not Set"):
		return False
	if op in ("In", "Not In"):
		return not f.value_list
	if op == "Between":
		return f.value in (None, "") or f.value_to in (None, "")
	return f.value in (None, "")


def _apply_filters_and_perms(q, cfg, col_refs):
	for f in cfg.filters:
		# Runtime filters with no value (and no `Is Set` / `Is Not Set`
		# operator that doesn't need one) are skipped so an unset
		# runtime filter doesn't accidentally filter rows out.
		if getattr(f, "is_runtime", False) and _runtime_filter_empty(f):
			continue
		key = (f.source or "", f.field_path)
		predicate = filter_ops.to_predicate(f, col_refs, key=key)
		# `to_predicate` returns None when an "All"-granularity row was left
		# blank or has an unrecognised shape — silently skip rather than throw.
		if predicate is None:
			continue
		q = q.where(predicate)

	# Base permission scope; for related sources we trust read-perm on the
	# DocType itself (already enforced in meta_validator).
	match_cond = _permission_match_conditions(cfg.base_doctype)
	if match_cond:
		q = q.where(LiteralValue(f"({match_cond})"))
	return q


def _apply_group(q, cfg, col_refs):
	if cfg.group_by:
		for g in cfg.group_by:
			q = q.groupby(aggregations.group_expr(g, col_refs))
	return q


def _select_field_for(col, col_refs, group_buckets):
	key = (col.source or "", col.field_path)
	field = col_refs[key]
	if col.aggregate:
		return aggregations.aggregate_expr(col.aggregate, field)
	if (col.source or "", col.field_path) in group_buckets and group_buckets[(col.source or "", col.field_path)]:
		return aggregations.date_bucket(field, group_buckets[(col.source or "", col.field_path)])
	return field


def _calc_field_refs(cfg) -> list[tuple[str, str]]:
	out: list[tuple[str, str]] = []
	for calc in cfg.calculations:
		_collect_field_refs(calc.expression, out)
	return out


def _collect_field_refs(expr: dict, sink: list):
	for side in ("left", "right"):
		operand = expr.get(side) or {}
		if operand.get("type") == "field":
			sink.append((operand.get("source") or "", operand.get("path") or ""))


def _build_select_plan(cfg, col_refs, group_buckets):
	"""Return (select_exprs, hidden_keys) where hidden_keys is the list of
	(source, path) tuples appended after the user's real columns."""
	real_cols = [c for c in cfg.columns if not c.is_calculation]
	select_exprs = [_select_field_for(c, col_refs, group_buckets) for c in real_cols]
	included_keys = {(c.source or "", c.field_path) for c in real_cols}

	hidden_keys: list[tuple[str, str]] = []
	if cfg.calculations:
		# Add fields referenced by calculations that aren't already in SELECT.
		# Under group_by, hidden refs must be aggregated (raw fields would
		# violate ONLY_FULL_GROUP_BY and the calc would evaluate to None).
		# Group keys stay raw; everything else defaults to SUM since
		# calc operands are numeric.
		group_keys = {(g.source or "", g.field_path) for g in cfg.group_by}
		for key in _calc_field_refs(cfg):
			if key in included_keys:
				continue
			field = col_refs[key]
			if cfg.group_by and key not in group_keys:
				expr = aggregations.aggregate_expr("Sum", field)
			else:
				expr = field
			select_exprs.append(expr)
			included_keys.add(key)
			hidden_keys.append(key)

	if not select_exprs:
		select_exprs = [aggregations.group_expr(g, col_refs) for g in cfg.group_by]

	return select_exprs, hidden_keys


def _build_data_query(cfg, base_table, link_joins, related_joins, col_refs):
	q = frappe.qb.from_(base_table)
	q = _apply_link_joins(q, link_joins)
	q = _apply_related_joins(q, related_joins)
	q = _apply_filters_and_perms(q, cfg, col_refs)
	q = _apply_group(q, cfg, col_refs)

	group_buckets = {(g.source or "", g.field_path): g.granularity for g in cfg.group_by}
	select_exprs, hidden_keys = _build_select_plan(cfg, col_refs, group_buckets)
	q = q.select(*select_exprs)

	for s in cfg.sort:
		key = (s.source or "", s.field_path)
		ref_field = col_refs[key]
		order = Order.asc if s.direction == "Ascending" else Order.desc
		q = q.orderby(ref_field, order=order)
	return q, hidden_keys


def _build_count_query(cfg, base_table, link_joins, related_joins, col_refs):
	if cfg.group_by:
		inner = frappe.qb.from_(base_table)
		inner = _apply_link_joins(inner, link_joins)
		inner = _apply_related_joins(inner, related_joins)
		inner = _apply_filters_and_perms(inner, cfg, col_refs)
		inner = _apply_group(inner, cfg, col_refs)
		inner = inner.select(LiteralValue("1"))
		return frappe.qb.from_(inner.as_("rs_sub")).select(Count("*"))

	q = frappe.qb.from_(base_table)
	q = _apply_link_joins(q, link_joins)
	q = _apply_related_joins(q, related_joins)
	q = _apply_filters_and_perms(q, cfg, col_refs)
	return q.select(Count("*"))


def _row_lookup(real_cols, hidden_keys, raw_row) -> dict:
	"""Map (source, path) -> value from a SELECT-ordered row.

	The select plan is: [real_cols..., hidden_keys...].
	"""
	out: dict[tuple[str, str], object] = {}
	keys_in_order = [(c.source or "", c.field_path) for c in real_cols] + list(hidden_keys)
	for key, value in zip(keys_in_order, raw_row, strict=False):
		out[key] = value
	return out


def _eval_calculation(expression: dict, lookup: dict):
	op = expression["op"]
	left = _eval_operand(expression["left"], lookup)
	right = _eval_operand(expression["right"], lookup)
	if left is None or right is None:
		return None
	left = flt(left)
	right = flt(right)
	if op == "+":
		return left + right
	if op == "-":
		return left - right
	if op == "*":
		return left * right
	if op == "/":
		return None if right == 0 else left / right
	return None


def _eval_operand(operand: dict, lookup: dict):
	if operand.get("type") == "const":
		return operand.get("value")
	source = operand.get("source") or ""
	path = operand.get("path") or ""
	return lookup.get((source, path))


def _shape_rows(cfg, rows: list[list], real_cols, hidden_keys, calc_index) -> list[list]:
	"""Project SQL rows into the column order requested, computing calculations.

	Each raw row is [real_cols..., hidden_keys...]. We use the lookup for both
	calculations and to drop hidden values from the output.
	"""
	out: list[list] = []
	real_count = len(real_cols)
	for raw_row in rows:
		raw_row = list(raw_row)
		lookup = _row_lookup(real_cols, hidden_keys, raw_row)
		real_values = raw_row[:real_count]
		row_out: list = []
		real_iter = iter(real_values)
		for col in cfg.columns:
			if col.is_calculation:
				calc = calc_index[col.calculation_alias]
				row_out.append(_eval_calculation(calc.expression, lookup))
			else:
				row_out.append(next(real_iter))
		out.append(row_out)
	return out


def build_and_run(config, page: int = 1, page_size: int = 20) -> QueryResult:
	cfg = schema.parse(config)
	refs = meta_validator.validate(cfg)

	page = pagination.clamp_page(page)
	page_size = pagination.clamp_page_size(page_size)

	base_table = frappe.qb.DocType(cfg.base_doctype)
	source_tables = join_resolver.build_source_tables(cfg, base_table)
	link_joins, col_refs = join_resolver.resolve(
		cfg, base_table, cfg.base_doctype, source_tables
	)
	related_joins = join_resolver.build_related_joins(cfg, source_tables, col_refs)

	data_q, hidden_keys = _build_data_query(
		cfg, base_table, link_joins, related_joins, col_refs
	)
	count_q = _build_count_query(cfg, base_table, link_joins, related_joins, col_refs)

	start = time.perf_counter()

	paged = pagination.apply(data_q, page, page_size)
	rows = paged.run(as_dict=False) or []

	total_rows = count_q.run() or [(0,)]
	total = int(total_rows[0][0]) if total_rows and total_rows[0] else 0

	calc_index = {c.alias: c for c in cfg.calculations}
	real_cols = [c for c in cfg.columns if not c.is_calculation]
	rows = _shape_rows(cfg, rows, real_cols, hidden_keys, calc_index)

	elapsed_ms = (time.perf_counter() - start) * 1000.0

	return QueryResult(
		columns=_column_meta(cfg, refs, calc_index),
		rows=rows,
		total=total,
		page=page,
		page_size=page_size,
		execution_ms=round(elapsed_ms, 2),
	)


def run_full(config, max_rows: int) -> QueryResult:
	max_rows = max(1, min(int(max_rows), schema.MAX_EXPORT_ROWS))
	return build_and_run(config, page=1, page_size=max_rows)
