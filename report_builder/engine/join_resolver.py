# SPDX-License-Identifier: MIT
from dataclasses import dataclass
from typing import Optional

import frappe
from frappe.query_builder.terms import SubQuery
from report_builder.engine.meta_validator import resolve_join_match_path


@dataclass
class JoinSpec:
	join_type: str  # "left" or "inner"
	parent_table: object
	link_fieldname: str
	child_doctype: str
	child_alias: str
	child_table: object
	# When set, the engine uses this expression as the ON clause instead
	# of the default Link semantics (parent[link_fieldname] == child.name).
	# Used for child-table traversal where ON is parent.name == child.parent.
	on_clause: object = None


@dataclass
class RelatedJoinSpec:
	"""Free-form join with optional helper joins for child-table connections."""

	join_type: str  # "left" or "inner"
	right_table: object
	right_alias: str
	pre_helper_joins: list
	conditions: list  # list of pypika BasicCriterion expressions for the root join
	post_helper_joins: list


@dataclass
class AuxiliaryJoinSpec:
	join_type: str  # "left" or "inner"
	table: object
	alias: str
	on_clause: object


def _safe_alias(prefix: tuple[str, ...]) -> str:
	return "rs_" + "__".join(prefix).replace(" ", "_").lower()


def _related_alias(alias: str) -> str:
	return "rs_src_" + alias


def resolve(cfg, base_table, base_doctype: str, source_tables: dict[str, object]):
	"""Build joins for both link traversals and related-source matches.

	source_tables: pre-built dict of source_alias -> qb table
	  ("" maps to base_table). Required so the caller can build joins in the
	  correct order.

	Returns (link_joins, col_refs).
	  link_joins: list[JoinSpec]
	  col_refs: dict[(source, path) -> qb.Field]
	"""

	# Collect every (source, path) pair referenced anywhere.
	pairs: set[tuple[str, str]] = set()

	def _add(source: str, path: str):
		pairs.add((source or "", path))

	for c in cfg.columns:
		if c.is_calculation:
			continue
		_add(c.source, c.field_path)
	for f in cfg.filters:
		_add(f.source, f.field_path)
	for g in cfg.group_by:
		_add(g.source, g.field_path)
	for s in cfg.sort:
		_add(s.source, s.field_path)
	# Calculations: walk operands and add their fields.
	for calc in cfg.calculations:
		_walk_operands(calc.expression, _add)

	# Group pairs by source. For base, we may need link traversal joins.
	prefix_table: dict[tuple[str, ...], tuple[object, str]] = {
		(): (base_table, base_doctype),
	}
	link_joins: list[JoinSpec] = []

	def ensure_link_join(prefix: tuple[str, ...]) -> tuple[object, str]:
		if prefix in prefix_table:
			return prefix_table[prefix]
		parent_prefix = prefix[:-1]
		parent_table, parent_dt = ensure_link_join(parent_prefix)
		link_fname = prefix[-1]
		df = frappe.get_meta(parent_dt).get_field(link_fname)
		if not df or not df.options or df.fieldtype not in ("Link", "Table", "Table MultiSelect"):
			frappe.throw(
				frappe._("Cannot join through {0}; not a Link or child table.").format(link_fname)
			)
		child_dt = df.options
		alias = _safe_alias(prefix)
		child_table = frappe.qb.DocType(child_dt).as_(alias)
		# Child tables join on parent.name == child.parent (and we narrow
		# by parenttype so other parent doctypes' rows don't leak in).
		if df.fieldtype in ("Table", "Table MultiSelect"):
			on_clause = (parent_table.name == child_table.parent) & (
				child_table.parenttype == parent_dt
			)
		else:
			on_clause = parent_table[link_fname] == child_table.name
		link_joins.append(
			JoinSpec(
				join_type="left",
				parent_table=parent_table,
				link_fieldname=link_fname,
				child_doctype=child_dt,
				child_alias=alias,
				child_table=child_table,
				on_clause=on_clause,
			)
		)
		prefix_table[prefix] = (child_table, child_dt)
		return child_table, child_dt

	for source, path in sorted(pairs):
		if source:
			# Related-source child-table paths are resolved later as helper joins.
			continue
		segments = tuple(s for s in path.split(".") if s)
		for i in range(1, len(segments)):
			ensure_link_join(segments[:i])

	col_refs: dict[tuple[str, str], object] = {}
	for source, path in pairs:
		if source:
			if "." in path:
				continue
			tbl = source_tables.get(source)
			if tbl is None:
				frappe.throw(frappe._("Unknown source alias: {0}").format(source))
			# direct field access
			col_refs[(source, path)] = tbl[path]
			continue
		segments = tuple(s for s in path.split(".") if s)
		if len(segments) == 1:
			col_refs[("", path)] = base_table[segments[0]]
		else:
			parent_prefix = segments[:-1]
			parent_table, _ = prefix_table[parent_prefix]
			col_refs[("", path)] = parent_table[segments[-1]]

	return link_joins, col_refs


def build_related_joins(cfg, source_tables: dict[str, object], col_refs):
	"""Return RelatedJoinSpec entries, including child-table helper joins when a
	match condition uses a one-hop child-table path."""
	from report_builder.engine.schema import VALID_JOIN_OPS

	specs: list[RelatedJoinSpec] = []
	source_doctypes = {"": cfg.base_doctype}
	referenced_paths: dict[str, set[str]] = {}
	for other in cfg.related_sources:
		source_doctypes[other.alias] = other.related_doctype
	for source, path in _referenced_source_paths(cfg):
		referenced_paths.setdefault(source, set()).add(path)
	for rs in cfg.related_sources:
		right_tbl = source_tables[rs.alias]
		join_type = "inner" if rs.join_type == "Inner Join" else "left"
		pre_helper_joins: list[AuxiliaryJoinSpec] = []
		post_helper_joins: list[AuxiliaryJoinSpec] = []
		left_helpers: dict[tuple[str, str], object] = {}
		right_child_groups: dict[str, dict] = {}
		post_child_helpers: dict[str, object] = {}
		conds = []

		for path in sorted(referenced_paths.get(rs.alias, set())):
			_resolve_source_field_ref(
				rs.alias,
				path,
				rs.related_doctype,
				right_tbl,
				join_type,
				col_refs,
				post_child_helpers,
				post_helper_joins,
			)

		for cond in rs.conditions:
			if cond.operator not in VALID_JOIN_OPS:
				frappe.throw(frappe._("Unknown match operator: {0}").format(cond.operator))
			left_source = cond.left_source or ""
			left_field = _resolve_join_field(
				rs.alias,
				left_source,
				cond.left_path,
				source_tables,
				source_doctypes,
				join_type,
				col_refs,
				left_helpers,
				pre_helper_joins,
			)
			right_ref = resolve_join_match_path(rs.related_doctype, cond.right_path, rs.alias)
			if right_ref.is_child_table:
				group = right_child_groups.setdefault(
					right_ref.table_fieldname,
					{"table_doctype": right_ref.table_doctype, "conditions": []},
				)
				group["conditions"].append((left_field, cond.operator, right_ref.fieldname))
				continue
			right_field = right_tbl[right_ref.fieldname]
			col_refs[(rs.alias, cond.right_path)] = right_field
			conds.append(_apply_op(left_field, cond.operator, right_field))

		# When this related source IS a child doctype joined via parent.name =
		# child.parent, narrow the join by parenttype so children of other
		# parent doctypes don't leak in (they share the same DB table and
		# would otherwise match every base row via the parent linkage). This
		# fires for the explicit "Join Child Table" flow (is_child_table=True,
		# conditions[0] is always the linkage) AND when the user picks an
		# istable=1 doctype manually and supplies a `right_path = "parent"`
		# match — without this guard a manual pick produces cartesian-style
		# rows because every parent doctype's children satisfy the join.
		related_meta = frappe.get_meta(rs.related_doctype)
		is_table_doctype = bool(getattr(related_meta, "istable", 0))
		if rs.conditions and (rs.is_child_table or is_table_doctype):
			parent_alias = None
			for cond in rs.conditions:
				if cond.right_path == "parent":
					parent_alias = cond.left_source or ""
					break
			if parent_alias is None and rs.is_child_table:
				parent_alias = rs.conditions[0].left_source or ""
			if parent_alias is not None:
				parent_dt = source_doctypes.get(parent_alias)
				if parent_dt:
					conds.append(right_tbl.parenttype == parent_dt)

		for table_fieldname, group in right_child_groups.items():
			sub_parent = frappe.qb.DocType(group["table_doctype"]).as_(
				_safe_alias((rs.alias, table_fieldname, "match_parent"))
			)
			parent_query = frappe.qb.from_(sub_parent).select(sub_parent.parent).where(
				sub_parent.parenttype == rs.related_doctype
			)
			for left_field, op, fieldname in group["conditions"]:
				parent_query = parent_query.where(_apply_op(left_field, op, sub_parent[fieldname]))
			conds.append(right_tbl.name.isin(SubQuery(parent_query)))

			paths_for_table = [
				path
				for path in referenced_paths.get(rs.alias, set())
				if "." in path and path.split(".", 1)[0] == table_fieldname
			]
			if paths_for_table:
				helper_table = _ensure_child_pick_join(
					rs.alias,
					table_fieldname,
					group["table_doctype"],
					rs.related_doctype,
					right_tbl,
					join_type,
					post_child_helpers,
					post_helper_joins,
					filters=group["conditions"],
				)
				for path in paths_for_table:
					path_ref = resolve_join_match_path(rs.related_doctype, path, rs.alias)
					col_refs[(rs.alias, path)] = helper_table[path_ref.fieldname]
		specs.append(
			RelatedJoinSpec(
				join_type=join_type,
				right_table=right_tbl,
				right_alias=rs.alias,
				pre_helper_joins=pre_helper_joins,
				conditions=conds,
				post_helper_joins=post_helper_joins,
			)
		)
	return specs


def _apply_op(left, op: str, right):
	if op == "=":
		return left == right
	if op == "!=":
		return left != right
	if op == ">":
		return left > right
	if op == ">=":
		return left >= right
	if op == "<":
		return left < right
	if op == "<=":
		return left <= right
	frappe.throw(frappe._("Unknown match operator: {0}").format(op))


def build_source_tables(cfg, base_table) -> dict[str, object]:
	tables: dict[str, object] = {"": base_table}
	for rs in cfg.related_sources:
		tables[rs.alias] = frappe.qb.DocType(rs.related_doctype).as_(_related_alias(rs.alias))
	return tables


def _resolve_join_field(
	current_alias: str,
	source_alias: str,
	path: str,
	source_tables: dict[str, object],
	source_doctypes: dict[str, str],
	join_type: str,
	col_refs,
	helper_cache: dict[tuple[str, str], object],
	helper_joins: list[AuxiliaryJoinSpec],
):
	if (source_alias, path) in col_refs:
		return col_refs[(source_alias, path)]
	source_dt = source_doctypes[source_alias]
	ref = resolve_join_match_path(source_dt, path, source_alias)
	root_table = source_tables[source_alias]
	if not ref.is_child_table:
		col_refs[(source_alias, path)] = root_table[ref.fieldname]
		return col_refs[(source_alias, path)]

	key = (source_alias, ref.table_fieldname)
	child_table = helper_cache.get(key)
	if child_table is None:
		child_table = _ensure_child_pick_join(
			current_alias,
			f"{source_alias or 'base'}__{ref.table_fieldname}",
			ref.table_doctype,
			source_dt,
			root_table,
			join_type,
			helper_cache,
			helper_joins,
		)
		helper_cache[key] = child_table
	col_refs[(source_alias, path)] = child_table[ref.fieldname]
	return col_refs[(source_alias, path)]


def _resolve_source_field_ref(
	source_alias: str,
	path: str,
	source_doctype: str,
	root_table,
	join_type: str,
	col_refs,
	child_helpers: dict[str, object],
	helper_joins: list[AuxiliaryJoinSpec],
):
	if (source_alias, path) in col_refs:
		return col_refs[(source_alias, path)]
	ref = resolve_join_match_path(source_doctype, path, source_alias)
	if not ref.is_child_table:
		col_refs[(source_alias, path)] = root_table[ref.fieldname]
		return col_refs[(source_alias, path)]
	child_table = child_helpers.get(ref.table_fieldname)
	if child_table is None:
		child_table = _ensure_child_pick_join(
			source_alias,
			ref.table_fieldname,
			ref.table_doctype,
			source_doctype,
			root_table,
			join_type,
			child_helpers,
			helper_joins,
		)
		child_helpers[ref.table_fieldname] = child_table
	col_refs[(source_alias, path)] = child_table[ref.fieldname]
	return col_refs[(source_alias, path)]


def _ensure_child_pick_join(
	owner_alias: str,
	key: str,
	child_doctype: str,
	root_doctype: str,
	root_table,
	join_type: str,
	cache: dict,
	helper_joins: list[AuxiliaryJoinSpec],
	filters: Optional[list[tuple[object, str, str]]] = None,
):
	cache_key = (key, tuple((fieldname, op) for _, op, fieldname in (filters or [])))
	child_table = cache.get(cache_key)
	if child_table is not None:
		return child_table
	suffix = f"pick{len(filters or [])}"
	child_alias = _safe_alias((owner_alias or "base", key, suffix))
	child_table = frappe.qb.DocType(child_doctype).as_(child_alias)
	sub = frappe.qb.DocType(child_doctype).as_(_safe_alias((owner_alias or "base", key, f"{suffix}_sub")))
	query = (
		frappe.qb.from_(sub)
		.select(sub.name)
		.where(sub.parent == root_table.name)
		.where(sub.parenttype == root_doctype)
		.orderby(sub.idx)
		.orderby(sub.name)
		.limit(1)
	)
	for left_field, op, fieldname in filters or []:
		query = query.where(_apply_op(left_field, op, sub[fieldname]))
	helper_joins.append(
		AuxiliaryJoinSpec(
			join_type=join_type,
			table=child_table,
			alias=child_alias,
			on_clause=child_table.name == SubQuery(query),
		)
	)
	cache[cache_key] = child_table
	return child_table


def _referenced_source_paths(cfg):
	for rows_name in ("columns", "filters", "group_by", "sort"):
		for row in getattr(cfg, rows_name, []):
			source = row.source or ""
			if source and row.field_path:
				yield source, row.field_path
	for calc in cfg.calculations:
		for side in ("left", "right"):
			operand = calc.expression.get(side) or {}
			if operand.get("type") == "field" and (operand.get("source") or ""):
				yield operand.get("source") or "", operand.get("path") or ""


def _walk_operands(expr: dict, sink):
	for side in ("left", "right"):
		operand = expr.get(side) or {}
		if operand.get("type") == "field":
			sink(operand.get("source") or "", operand.get("path") or "")
