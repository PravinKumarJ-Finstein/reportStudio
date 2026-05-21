# SPDX-License-Identifier: MIT
import json

import frappe

from report_builder.engine.meta_validator import (
	LAYOUT_FIELDTYPES,
	assert_doctype_readable,
	resolve_join_match_path,
	resolve_path,
)
from report_builder.engine.schema import MAX_JOIN_DEPTH

EXCLUDED_DOCTYPES = {"DocType", "DocField", "DocPerm"}


@frappe.whitelist()
def get_allowed_doctypes(search: str = "", limit: int = 50) -> list[dict]:
	limit = max(1, min(int(limit or 50), 200))
	search = (search or "").strip()

	doctypes = frappe.get_all(
		"DocType",
		fields=["name", "module"],
		order_by="name asc",
	)

	out = []
	for dt in doctypes:
		if dt.name in EXCLUDED_DOCTYPES:
			continue
		if search and search.lower() not in dt.name.lower():
			continue
		if not frappe.has_permission(dt.name, "read"):
			continue
		out.append({"name": dt.name, "label": dt.name, "module": dt.module})
		if len(out) >= limit:
			break
	return out


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def search_doctypes(doctype, txt, searchfield, start, page_len, filters):
	"""Autocomplete callback for the Link control on the Report Studio page.

	Frappe's search_widget calls this with the standard positional signature.
	Returns list of (name, module) tuples — the Link autocomplete renders the
	first as the value and the second as the description.
	"""
	txt = (txt or "").strip()
	page_length = int(page_len or 20)
	start = int(start or 0)

	or_filters = None
	if txt:
		like = f"%{txt}%"
		or_filters = [
			["DocType", "name", "like", like],
			["DocType", "module", "like", like],
		]

	# Pull more than page_length so per-permission filtering still leaves a full page.
	rows = frappe.get_all(
		"DocType",
		or_filters=or_filters,
		fields=["name", "module"],
		order_by="name asc",
		limit_page_length=page_length * 4,
		limit_start=start,
	)

	out = []
	for r in rows:
		if r.name in EXCLUDED_DOCTYPES:
			continue
		if not frappe.has_permission(r.name, "read"):
			continue
		out.append((r.name, r.module))
		if len(out) >= page_length:
			break
	return out


def _serialize_field(df, base_path: str = "", include_link_target: bool = True) -> dict:
	path = f"{base_path}.{df.fieldname}" if base_path else df.fieldname
	# Show the raw DB column name (df.fieldname) rather than the human label,
	# so the picker mirrors the actual columns in the underlying tables.
	entry = {
		"fieldname": df.fieldname,
		"label": df.fieldname,
		"fieldtype": df.fieldtype,
		"options": df.options,
		"path": path,
	}
	if df.fieldtype in ("Table", "Table MultiSelect"):
		entry["is_child_table"] = True
	return entry


@frappe.whitelist()
def get_fields(doctype: str, depth: int = 1) -> list[dict]:
	doctype = (doctype or "").strip()
	if not doctype:
		return []

	depth = max(0, min(int(depth or 1), MAX_JOIN_DEPTH))
	assert_doctype_readable(doctype)

	return _get_fields_recursive(doctype, "", depth)


def _get_fields_recursive(doctype: str, base_path: str, depth: int) -> list[dict]:
	meta = frappe.get_meta(doctype)
	user_permlevels = _user_permlevels(doctype, "read")
	out: list[dict] = []
	seen_fieldnames: set[str] = set()

	if not base_path:
		out.append(
			{
				"fieldname": "name",
				"label": "name",
				"fieldtype": "Data",
				"options": None,
				"path": "name",
			}
		)
		seen_fieldnames.add("name")

	for df in meta.fields:
		if df.fieldtype in LAYOUT_FIELDTYPES:
			continue
		if int(df.permlevel or 0) > 0 and df.permlevel not in user_permlevels:
			continue

		entry = _serialize_field(df, base_path)
		seen_fieldnames.add(df.fieldname)

		# Child table: surface as a sub-tree. Uses the same dotted path
		# scheme as Link traversal — engine joins child.parent = base.name.
		# Pass full `depth` (not depth-1) so Link fields inside the child
		# table can still drill down one level — users want every field
		# inside the table reachable, not a truncated subset.
		if df.fieldtype in ("Table", "Table MultiSelect") and df.options and depth > 0:
			if frappe.db.exists("DocType", df.options) and frappe.has_permission(
				df.options, "read"
			):
				children = _get_fields_recursive(df.options, entry["path"], depth)
				# Flag children so the palette can show "needs Join Child Table".
				for ch in children:
					ch["requires_child_join"] = True
					ch["child_parent_field"] = df.fieldname
					ch["child_doctype"] = df.options
				entry["children"] = children
			out.append(entry)
			continue

		if df.fieldtype == "Link" and df.options and depth > 0:
			if frappe.db.exists("DocType", df.options) and frappe.has_permission(
				df.options, "read"
			):
				children = _get_fields_recursive(df.options, entry["path"], depth - 1)
				entry["children"] = children
		out.append(entry)

	# Surface raw DB columns that aren't in DocFields (creation, modified, owner,
	# docstatus, idx, _user_tags, _comments, _assign, _liked_by, parent/parenttype
	# on child tables, plus any custom columns added directly at the DB level).
	for col_name, fieldtype in get_db_columns(doctype).items():
		if col_name in seen_fieldnames:
			continue
		path = f"{base_path}.{col_name}" if base_path else col_name
		out.append(
			{
				"fieldname": col_name,
				"label": col_name,
				"fieldtype": fieldtype,
				"options": None,
				"path": path,
			}
		)
	return out


# Map MySQL/MariaDB types to the closest Frappe fieldtype. Used both by the
# field-tree builder and by meta_validator when a path resolves to a DB-only
# column. Anything we don't recognise falls back to Data.
_DB_TYPE_TO_FIELDTYPE = {
	"tinyint": "Check",
	"smallint": "Int",
	"mediumint": "Int",
	"int": "Int",
	"bigint": "Int",
	"decimal": "Float",
	"numeric": "Float",
	"float": "Float",
	"double": "Float",
	"date": "Date",
	"datetime": "Datetime",
	"timestamp": "Datetime",
	"time": "Time",
	"json": "Code",
	"text": "Long Text",
	"mediumtext": "Long Text",
	"longtext": "Long Text",
	"tinytext": "Small Text",
	"varchar": "Data",
	"char": "Data",
}


def get_db_columns(doctype: str) -> dict[str, str]:
	"""Return {column_name: fieldtype} for the actual DB table backing `doctype`.

	Empty dict if the table doesn't exist (virtual/single doctypes). Cached on
	frappe.local because INFORMATION_SCHEMA lookups during a request are cheap
	but not free, and `_get_fields_recursive` revisits the same doctype across
	link/child traversals.
	"""
	cache = getattr(frappe.local, "_rb_db_columns", None)
	if cache is None:
		cache = {}
		frappe.local._rb_db_columns = cache
	if doctype in cache:
		return cache[doctype]

	# Defence-in-depth: only consult INFORMATION_SCHEMA for doctypes that
	# actually exist. Callers all run `assert_doctype_readable` upstream, but
	# re-checking here makes this function safe to call directly and stops a
	# scanner from worrying about the table name flowing into the query.
	if not frappe.db.exists("DocType", doctype):
		cache[doctype] = {}
		return cache[doctype]

	# Build the table name without f-string interpolation — the value is fully
	# parameterized via `%s` below, but pattern-based scanners flag any
	# f-string flowing into a `frappe.db.sql` call regardless. INFORMATION_SCHEMA
	# is not exposed through Frappe's ORM, so a raw query is necessary; both
	# parameters (db_name and table_name) are passed as bound `%s` values.
	table_name = "tab" + doctype
	rows = frappe.db.sql(  # nosemgrep: frappe-dont-use-frappe-db-sql
		"""
		SELECT COLUMN_NAME, DATA_TYPE
		FROM INFORMATION_SCHEMA.COLUMNS
		WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
		ORDER BY ORDINAL_POSITION
		""",
		(frappe.conf.db_name, table_name),
		as_dict=True,
	) or []

	out: dict[str, str] = {}
	for row in rows:
		name = row["COLUMN_NAME"]
		ftype = _DB_TYPE_TO_FIELDTYPE.get((row["DATA_TYPE"] or "").lower(), "Data")
		out[name] = ftype
	cache[doctype] = out
	return out


def _user_permlevels(doctype: str, permission_type: str) -> set[int]:
	roles = set(frappe.get_roles(frappe.session.user))
	levels = set()
	for perm in frappe.get_meta(doctype).permissions:
		if perm.role in roles and perm.get(permission_type):
			levels.add(int(perm.permlevel or 0))
	return levels


@frappe.whitelist()
def get_link_path(base_doctype: str, field_path: str) -> dict:
	ref = resolve_path(base_doctype, field_path)
	return {
		"target_doctype": ref.terminal_doctype,
		"target_fieldname": ref.fieldname,
		"fieldtype": ref.fieldtype,
		"options": ref.options,
		"label": ref.label,
	}


@frappe.whitelist()
def get_connection_candidates(
	base_doctype: str,
	related_doctype: str,
	related_sources=None,
	exclude_alias: str = "",
) -> dict:
	base_doctype = (base_doctype or "").strip()
	related_doctype = (related_doctype or "").strip()
	if not base_doctype or not related_doctype:
		return {"candidates": [], "default_candidate_id": None}

	assert_doctype_readable(base_doctype)
	assert_doctype_readable(related_doctype)

	parsed_sources = _coerce_related_sources(related_sources)
	source_entries = [{"alias": "", "doctype": base_doctype, "label": base_doctype}]
	for row in parsed_sources:
		alias = (row.get("alias") or "").strip()
		doctype = (row.get("related_doctype") or "").strip()
		if not alias or not doctype or alias == exclude_alias:
			continue
		assert_doctype_readable(doctype)
		source_entries.append(
			{"alias": alias, "doctype": doctype, "label": f"{alias} ({doctype})"}
		)

	seen: set[tuple[str, str, str]] = set()
	candidates: list[dict] = []
	for source_rank, source in enumerate(source_entries):
		for cand in _discover_candidates_between(source, related_doctype):
			key = (cand["left_source"], cand["left_path"], cand["right_path"])
			if key in seen:
				continue
			seen.add(key)
			cand["source_rank"] = source_rank
			cand["id"] = _candidate_id(cand)
			cand["label"] = _candidate_label(source, related_doctype, cand)
			cand["conditions"] = [
				{
					"left_source": cand["left_source"],
					"left_path": cand["left_path"],
					"operator": "=",
					"right_path": cand["right_path"],
				}
			]
			candidates.append(cand)

	candidates.sort(key=lambda d: (d["score"], d["source_rank"], d["label"]))
	default_candidate_id = candidates[0]["id"] if candidates else None
	for cand in candidates:
		cand.pop("score", None)
		cand.pop("source_rank", None)
	return {"candidates": candidates, "default_candidate_id": default_candidate_id}


def _coerce_related_sources(raw) -> list[dict]:
	if raw in (None, ""):
		return []
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except json.JSONDecodeError:
			return []
	if not isinstance(raw, list):
		return []
	return [row for row in raw if isinstance(row, dict)]


def _discover_candidates_between(source: dict, related_doctype: str) -> list[dict]:
	source_doctype = source["doctype"]
	if source_doctype == related_doctype:
		return []

	seen: set[tuple[str, str, str]] = set()
	out: list[dict] = []

	def add(left_path: str, right_path: str, connection_type: str, score: int) -> None:
		# Mute messages around speculative validation: resolve_join_match_path
		# calls frappe.throw on a miss, which (even when the exception is
		# caught) leaves the rejected "Field X not found on Y" notice in
		# frappe.local.message_log and surfaces it to the client.
		saved_mute = frappe.flags.mute_messages
		saved_log = list(frappe.local.message_log or [])
		frappe.flags.mute_messages = True
		try:
			try:
				resolve_join_match_path(source_doctype, left_path, source["alias"])
				resolve_join_match_path(related_doctype, right_path)
			except Exception:
				return
		finally:
			frappe.flags.mute_messages = saved_mute
			frappe.local.message_log = saved_log
		key = (source["alias"], left_path, right_path)
		if key in seen:
			return
		seen.add(key)
		out.append(
			{
				"left_source": source["alias"],
				"left_path": left_path,
				"right_path": right_path,
				"connection_type": connection_type,
				"score": score,
			}
		)

	related_meta = frappe.get_meta(related_doctype)
	for df in related_meta.fields:
		if df.fieldtype == "Link" and df.options == source_doctype:
			add("name", df.fieldname, "related_link", 20)

	source_meta = frappe.get_meta(source_doctype)
	for df in source_meta.fields:
		if df.fieldtype == "Link" and df.options == related_doctype:
			add(df.fieldname, "name", "source_link", 30)

	for path in _dashboard_root_matches(source_doctype, related_doctype):
		add("name", path, "dashboard_field", 40)
	for path in _dashboard_root_matches(related_doctype, source_doctype):
		add(path, "name", "dashboard_field", 45)

	# NOTE: Child-table-mediated join candidates are no longer auto-suggested
	# here — users now opt in explicitly via the "Join via Child Tables"
	# section in the Add Related DocType dialog.

	return out


def _dashboard_root_matches(base_doctype: str, target_doctype: str) -> list[str]:
	data = frappe.get_meta(base_doctype).get_dashboard_data() or {}
	fieldname = (data.get("non_standard_fieldnames") or {}).get(target_doctype)
	if not fieldname:
		default_field = data.get("fieldname")
		if default_field and target_doctype in _dashboard_items(data):
			fieldname = default_field
	if not fieldname:
		return []
	saved_mute = frappe.flags.mute_messages
	saved_log = list(frappe.local.message_log or [])
	frappe.flags.mute_messages = True
	try:
		try:
			resolve_join_match_path(target_doctype, fieldname)
		except Exception:
			return []
	finally:
		frappe.flags.mute_messages = saved_mute
		frappe.local.message_log = saved_log
	return [fieldname]


def _dashboard_items(data: dict) -> set[str]:
	items: set[str] = set()
	for group in data.get("transactions") or []:
		for item in group.get("items") or []:
			items.add(item)
	return items


def _candidate_id(cand: dict) -> str:
	left_source = cand.get("left_source") or "base"
	return f"{left_source}|{cand['left_path']}|{cand['right_path']}"


def _candidate_label(source: dict, related_doctype: str, cand: dict) -> str:
	left_prefix = source["label"]
	return f"{left_prefix}.{cand['left_path']} = {related_doctype}.{cand['right_path']}"


@frappe.whitelist()
def get_child_tables(doctype: str) -> list[dict]:
	"""Return Table / Table MultiSelect fields on `doctype` so the Studio can
	show a 'Join Child Table' checkbox per child. Each entry exposes the table
	fieldname (e.g. 'items'), the child doctype (e.g. 'Sales Invoice Item'),
	and a label."""
	doctype = (doctype or "").strip()
	if not doctype:
		return []
	assert_doctype_readable(doctype)
	out: list[dict] = []
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype not in ("Table", "Table MultiSelect"):
			continue
		if not df.options:
			continue
		if not frappe.db.exists("DocType", df.options):
			continue
		if not frappe.has_permission(df.options, "read"):
			continue
		out.append(
			{
				"fieldname": df.fieldname,
				"child_doctype": df.options,
				"label": df.label or df.fieldname,
			}
		)
	return out


@frappe.whitelist()
def detect_child_join(child_doctype: str, target_doctype: str) -> dict:
	"""Best-effort: find a Link field on `child_doctype` pointing to
	`target_doctype` so the Studio can pre-fill the right-side join
	condition when a 'Join Child Table' checkbox is ticked. Returns
	{"left_path": "<child_link_field>", "right_path": "name"} or {} if
	no candidate exists."""
	child_doctype = (child_doctype or "").strip()
	target_doctype = (target_doctype or "").strip()
	if not child_doctype or not target_doctype:
		return {}
	assert_doctype_readable(child_doctype)
	for df in frappe.get_meta(child_doctype).fields:
		if df.fieldtype == "Link" and df.options == target_doctype:
			return {"left_path": df.fieldname, "right_path": "name"}
	return {}
