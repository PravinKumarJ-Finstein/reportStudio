# SPDX-License-Identifier: MIT
from dataclasses import dataclass
from typing import Optional

import frappe

from report_builder.engine.schema import MAX_JOIN_DEPTH, Config

LAYOUT_FIELDTYPES = {
	"Section Break",
	"Column Break",
	"Tab Break",
	"HTML",
	"Button",
	"Heading",
	"Fold",
	"Image",
}

NUMERIC_FIELDTYPES = {"Int", "Float", "Currency", "Percent"}
DATE_FIELDTYPES = {"Date"}
DATETIME_FIELDTYPES = {"Datetime"}
TIME_FIELDTYPES = {"Time"}
TEXT_FIELDTYPES = {
	"Data",
	"Small Text",
	"Long Text",
	"Text",
	"Text Editor",
	"Code",
	"Read Only",
	"Markdown Editor",
}


@dataclass
class FieldRef:
	source: str  # "" for base, otherwise a related-source alias
	path: str
	segments: tuple[str, ...]
	terminal_doctype: str
	fieldname: str
	fieldtype: str
	options: Optional[str]
	label: str

	@property
	def is_link_traversal(self) -> bool:
		return len(self.segments) > 1


@dataclass
class JoinPathRef:
	source: str
	path: str
	segments: tuple[str, ...]
	base_doctype: str
	terminal_doctype: str
	fieldname: str
	fieldtype: str
	options: Optional[str]
	label: str
	table_fieldname: str = ""
	table_doctype: str = ""

	@property
	def is_child_table(self) -> bool:
		return bool(self.table_fieldname)


def assert_doctype_readable(doctype: str) -> None:
	if not doctype:
		frappe.throw(frappe._("DocType name is missing."))
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(frappe._("Unknown DocType: {0}").format(doctype))

	meta = frappe.get_meta(doctype)
	if getattr(meta, "is_virtual", 0):
		frappe.throw(frappe._("Virtual DocTypes are not supported."))

	if not frappe.has_permission(doctype, "read"):
		raise frappe.PermissionError(
			frappe._("You do not have permission to read {0}.").format(doctype)
		)


def resolve_path(base_doctype: str, path: str, source: str = "") -> FieldRef:
	"""Walk the dotted path against meta. Each non-terminal segment must be Link.

	Performs permission checks on every linked DocType encountered. `source`
	is informational — the caller passes the alias the path is anchored on.
	"""

	if not path:
		frappe.throw(frappe._("A field is missing."))

	segments = tuple(s.strip() for s in path.split(".") if s.strip())
	if not segments:
		frappe.throw(frappe._("Empty field path."))

	if len(segments) - 1 > MAX_JOIN_DEPTH:
		frappe.throw(
			frappe._("Field {0} traverses too many links (max {1}).").format(
				path, MAX_JOIN_DEPTH
			)
		)

	current_dt = base_doctype
	df = None
	for i, segment in enumerate(segments):
		assert_doctype_readable(current_dt)
		meta = frappe.get_meta(current_dt)
		is_last = i == len(segments) - 1

		if segment == "name":
			df = frappe._dict(
				{
					"fieldname": "name",
					"fieldtype": "Data",
					"label": "ID",
					"options": None,
					"permlevel": 0,
				}
			)
		else:
			df = meta.get_field(segment)
			if not df and is_last:
				# Fall back to a real DB column (e.g. creation, modified, owner,
				# docstatus, _user_tags, custom DB-level columns). Only the
				# terminal segment is allowed — non-Link columns can't be drilled.
				from report_builder.api.metadata import get_db_columns

				db_cols = get_db_columns(current_dt)
				if segment in db_cols:
					df = frappe._dict(
						{
							"fieldname": segment,
							"fieldtype": db_cols[segment],
							"label": segment,
							"options": None,
							"permlevel": 0,
						}
					)
			if not df:
				frappe.throw(
					frappe._("Field {0} not found on {1}.").format(segment, current_dt)
				)
			if df.fieldtype in LAYOUT_FIELDTYPES:
				frappe.throw(frappe._("Field {0} is a layout element, not data.").format(segment))

		if int(getattr(df, "permlevel", 0) or 0) > 0:
			user_permlevels = _user_permlevels(current_dt, "read")
			if df.permlevel not in user_permlevels:
				raise frappe.PermissionError(
					frappe._("You do not have access to field {0}.").format(df.label or df.fieldname)
				)

		if not is_last:
			if df.fieldtype not in ("Link", "Table", "Table MultiSelect"):
				frappe.throw(
					frappe._(
						"Field {0} is not a link or child table; cannot drill further into it."
					).format(segment)
				)
			current_dt = df.options
			if not current_dt:
				frappe.throw(frappe._("Field {0} has no target DocType.").format(segment))

	return FieldRef(
		source=source,
		path=".".join(segments),
		segments=segments,
		terminal_doctype=current_dt,
		fieldname=df.fieldname,
		fieldtype=df.fieldtype,
		options=df.options,
		label=df.label or df.fieldname,
	)


def resolve_join_match_path(base_doctype: str, path: str, source: str = "") -> JoinPathRef:
	"""Resolve a join-condition path.

	Join conditions support either a direct field on the root DocType or a
	one-hop child-table field (`table_field.child_field`). Link traversal is not
	supported here because connection discovery in ERPNext primarily exposes
	root fields and child-table links.
	"""

	if not path:
		frappe.throw(frappe._("A field is missing."))

	segments = tuple(s.strip() for s in path.split(".") if s.strip())
	if not segments:
		frappe.throw(frappe._("Empty field path."))

	if len(segments) == 1:
		ref = resolve_path(base_doctype, path, source)
		return JoinPathRef(
			source=source,
			path=ref.path,
			segments=ref.segments,
			base_doctype=base_doctype,
			terminal_doctype=ref.terminal_doctype,
			fieldname=ref.fieldname,
			fieldtype=ref.fieldtype,
			options=ref.options,
			label=ref.label,
		)

	if len(segments) != 2:
		frappe.throw(
			frappe._("Join field {0} must be a root field or a one-hop child-table field.").format(path)
		)

	assert_doctype_readable(base_doctype)
	meta = frappe.get_meta(base_doctype)
	table_df = meta.get_field(segments[0])
	if not table_df or table_df.fieldtype not in ("Table", "Table MultiSelect") or not table_df.options:
		frappe.throw(
			frappe._("Field {0} is not a child table; join conditions cannot drill into it.").format(segments[0])
		)
	child_dt = table_df.options
	assert_doctype_readable(child_dt)
	child_meta = frappe.get_meta(child_dt)
	child_df = child_meta.get_field(segments[1])
	if segments[1] == "name":
		child_df = frappe._dict(
			{
				"fieldname": "name",
				"fieldtype": "Data",
				"label": "ID",
				"options": None,
				"permlevel": 0,
			}
		)
	if not child_df:
		from report_builder.api.metadata import get_db_columns

		db_cols = get_db_columns(child_dt)
		if segments[1] in db_cols:
			child_df = frappe._dict(
				{
					"fieldname": segments[1],
					"fieldtype": db_cols[segments[1]],
					"label": segments[1],
					"options": None,
					"permlevel": 0,
				}
			)
	if not child_df:
		frappe.throw(frappe._("Field {0} not found on {1}.").format(segments[1], child_dt))
	if child_df.fieldtype in LAYOUT_FIELDTYPES:
		frappe.throw(frappe._("Field {0} is a layout element, not data.").format(segments[1]))
	if int(getattr(child_df, "permlevel", 0) or 0) > 0:
		user_permlevels = _user_permlevels(child_dt, "read")
		if child_df.permlevel not in user_permlevels:
			raise frappe.PermissionError(
				frappe._("You do not have access to field {0}.").format(child_df.label or child_df.fieldname)
			)

	return JoinPathRef(
		source=source,
		path=".".join(segments),
		segments=segments,
		base_doctype=base_doctype,
		terminal_doctype=child_dt,
		fieldname=child_df.fieldname,
		fieldtype=child_df.fieldtype,
		options=child_df.options,
		label=child_df.label or child_df.fieldname,
		table_fieldname=table_df.fieldname,
		table_doctype=child_dt,
	)


def _user_permlevels(doctype: str, permission_type: str) -> set[int]:
	roles = set(frappe.get_roles(frappe.session.user))
	levels = set()
	for perm in frappe.get_meta(doctype).permissions:
		if perm.role in roles and perm.get(permission_type):
			levels.add(int(perm.permlevel or 0))
	return levels


def _source_doctype_map(cfg: Config) -> dict[str, str]:
	"""Return a mapping of source alias -> base/related DocType."""
	mapping: dict[str, str] = {"": cfg.base_doctype}
	for rs in cfg.related_sources:
		mapping[rs.alias] = rs.related_doctype
	return mapping


def _validate_related_sources(cfg: Config, source_map: dict[str, str]) -> None:
	for rs in cfg.related_sources:
		assert_doctype_readable(rs.related_doctype)

	# Validate join conditions against the actual fields on each side.
	known_sources_so_far: set[str] = {""}
	for rs in cfg.related_sources:
		for cond in rs.conditions:
			if cond.left_source not in known_sources_so_far:
				frappe.throw(
					frappe._(
						"Match condition for {0} references unknown or later source {1}."
					).format(rs.alias, cond.left_source or "(base)")
				)
			# left side resolves on the alias's doctype
			left_dt = source_map.get(cond.left_source, cfg.base_doctype)
			resolve_join_match_path(left_dt, cond.left_path, cond.left_source)
			# right side resolves on this related source
			resolve_join_match_path(rs.related_doctype, cond.right_path, rs.alias)
		known_sources_so_far.add(rs.alias)


def validate(cfg: Config) -> dict[tuple[str, str], FieldRef]:
	"""Validate every field reference in the config against meta + permissions.

	Returns a map of `(source, field_path) -> FieldRef`.
	"""

	assert_doctype_readable(cfg.base_doctype)
	source_map = _source_doctype_map(cfg)
	_validate_related_sources(cfg, source_map)

	resolved: dict[tuple[str, str], FieldRef] = {}

	def _resolve(source: str, path: str) -> FieldRef:
		key = (source or "", path)
		if key in resolved:
			return resolved[key]
		dt = source_map.get(source or "")
		if not dt:
			frappe.throw(frappe._("Unknown source alias: {0}").format(source))
		ref = resolve_join_match_path(dt, path, source or "") if source else resolve_path(dt, path, source or "")
		resolved[key] = ref
		return ref

	if not cfg.columns and not cfg.group_by:
		frappe.throw(frappe._("Add at least one column to your report."))

	calc_aliases = {c.alias for c in cfg.calculations}

	for col in cfg.columns:
		if col.is_calculation:
			if col.calculation_alias not in calc_aliases:
				frappe.throw(
					frappe._("Calculation {0} is not defined.").format(col.calculation_alias)
				)
			continue
		ref = _resolve(col.source, col.field_path)
		col.fieldtype = ref.fieldtype

	for f in cfg.filters:
		ref = _resolve(f.source, f.field_path)
		f.fieldtype = ref.fieldtype
		_validate_filter_value(f, ref)

	for g in cfg.group_by:
		ref = _resolve(g.source, g.field_path)
		g.fieldtype = ref.fieldtype
		if g.granularity and ref.fieldtype not in (DATE_FIELDTYPES | DATETIME_FIELDTYPES):
			g.granularity = ""

	for s in cfg.sort:
		_resolve(s.source, s.field_path)

	# Calculations: validate every field reference inside the expression tree.
	for calc in cfg.calculations:
		_validate_calc_expression(calc.expression, _resolve, source_map)

	if cfg.group_by:
		group_keys = {(g.source or "", g.field_path) for g in cfg.group_by}
		for col in cfg.columns:
			if col.is_calculation:
				continue
			if (col.source or "", col.field_path) in group_keys:
				continue
			if not col.aggregate:
				frappe.throw(
					frappe._(
						"Column {0} needs a Summary (Sum/Count/...) when grouping data."
					).format(col.label or col.field_path)
				)

	return resolved


def _validate_calc_expression(expr: dict, resolver, source_map: dict[str, str]):
	"""Walk a calculation expression; verify field operands resolve."""
	for side in ("left", "right"):
		operand = expr.get(side, {})
		if operand.get("type") == "field":
			source = operand.get("source") or ""
			path = operand.get("path") or ""
			ref = resolver(source, path)
			if ref.fieldtype not in NUMERIC_FIELDTYPES and ref.fieldtype not in {"Int"}:
				frappe.throw(
					frappe._("Calculation field {0} must be numeric (got {1}).").format(
						f"{source + ':' if source else ''}{path}", ref.fieldtype
					)
				)


def _validate_filter_value(f, ref: FieldRef) -> None:
	op = f.operator
	if op in ("Is Set", "Is Not Set"):
		return
	# Runtime filters get their value from the query-report filter bar at run
	# time. The Studio config holds an optional default, so empty is OK.
	if getattr(f, "is_runtime", False):
		return
	if op == "Between":
		if f.value in (None, "") or f.value_to in (None, ""):
			frappe.throw(
				frappe._("Filter on {0} needs both From and To values.").format(ref.label)
			)
		return
	if op in ("In", "Not In"):
		if not f.value_list:
			frappe.throw(frappe._("Filter on {0} needs at least one value.").format(ref.label))
		return
	if f.value in (None, ""):
		frappe.throw(frappe._("Filter on {0} needs a value.").format(ref.label))
