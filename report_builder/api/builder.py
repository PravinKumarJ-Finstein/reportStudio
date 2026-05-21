# SPDX-License-Identifier: MIT
import json
from typing import Optional

import frappe
from frappe.utils import now

from report_builder.engine import query_engine

REPORT_DOCTYPE = "Report Studio Report"


@frappe.whitelist()
def preview(config, page: int = 1, page_size: int = 20) -> dict:
	result = query_engine.build_and_run(config, page=int(page or 1), page_size=int(page_size or 20))
	return {
		"columns": result.columns,
		"rows": result.rows,
		"total": result.total,
		"page": result.page,
		"page_size": result.page_size,
		"execution_ms": result.execution_ms,
	}


def _safe_json(raw, default):
	if raw in (None, ""):
		return default
	if isinstance(raw, (list, dict)):
		return raw
	try:
		return json.loads(raw)
	except (TypeError, json.JSONDecodeError):
		return default


def _coerce_config(config) -> dict:
	if isinstance(config, str):
		try:
			return json.loads(config)
		except json.JSONDecodeError:
			frappe.throw(frappe._("Report configuration is not valid JSON."))
	if isinstance(config, dict):
		return config
	frappe.throw(frappe._("Report configuration must be an object."))


def doc_to_config(doc) -> dict:
	"""Project a Report Studio Report doc into the JSON-serializable config
	shape that load_report / preview / run_for_standard_report all consume."""
	return {
		"base_doctype": doc.base_doctype,
		"related_sources": [
			{
				"alias": rs.alias,
				"related_doctype": rs.related_doctype,
				"join_type": rs.join_type,
				"conditions": _safe_json(rs.conditions, []),
				"is_child_table": bool(getattr(rs, "is_child_table", 0)),
				"child_parent_field": getattr(rs, "child_parent_field", "") or "",
			}
			for rs in doc.related_sources
		],
		"calculations": [
			{
				"alias": c.alias,
				"label": c.label,
				"format_type": c.format_type or "Number",
				"expression": _safe_json(c.expression, {}),
			}
			for c in doc.calculations
		],
		"columns": [
			{
				"source": c.source or "",
				"field_path": c.field_path,
				"calculation_alias": c.calculation_alias,
				"label": c.label,
				"fieldtype": c.fieldtype,
				"aggregate": c.aggregate,
				"width": c.width,
				"visibility_rule": _safe_json(c.visibility_rule, None),
				"format_rules": _safe_json(c.format_rules, []),
			}
			for c in doc.columns
		],
		"filters": [
			{
				"source": f.source or "",
				"field_path": f.field_path,
				"fieldtype": f.fieldtype,
				"operator": f.operator,
				"value": f.value,
				"value_to": f.value_to,
				"value_list": f.value_list,
				"granularity": getattr(f, "granularity", "") or "",
				"is_runtime": bool(getattr(f, "is_runtime", 0)),
			}
			for f in doc.filters
		],
		"group_by": [
			{
				"source": g.source or "",
				"field_path": g.field_path,
				"fieldtype": g.fieldtype,
				"granularity": g.granularity,
			}
			for g in doc.group_by
		],
		"sort": [
			{"source": s.source or "", "field_path": s.field_path, "direction": s.direction}
			for s in doc.sort
		],
	}


def _coerce_roles(shared_roles) -> list[dict]:
	if shared_roles in (None, ""):
		return []
	if isinstance(shared_roles, str):
		try:
			shared_roles = json.loads(shared_roles)
		except json.JSONDecodeError:
			return []
	if not isinstance(shared_roles, list):
		return []
	out = []
	for r in shared_roles:
		if isinstance(r, str):
			out.append({"role": r, "can_edit": 0})
		elif isinstance(r, dict) and r.get("role"):
			out.append({"role": r["role"], "can_edit": 1 if r.get("can_edit") else 0})
	return out


@frappe.whitelist()
def save_report(
	title: str,
	base_doctype: str,
	config,
	visibility: str = "Private",
	shared_roles=None,
	name: Optional[str] = None,
	description: str = "",
	page_size: int = 20,
) -> str:
	cfg = _coerce_config(config)
	roles = _coerce_roles(shared_roles)

	# Validate the structural shape of the config (operators, alias chars,
	# duplicate aliases, etc.) before persisting. This catches user errors
	# fast at save time instead of letting them slip through and fail
	# silently in the auto-publish step. Field/perm checks still happen at
	# preview time via meta_validator, so empty drafts (no columns) still
	# pass here.
	from report_builder.engine import schema as _schema_mod

	_schema_mod.parse({**cfg, "base_doctype": base_doctype or cfg.get("base_doctype")})

	if name and frappe.db.exists(REPORT_DOCTYPE, name):
		doc = frappe.get_doc(REPORT_DOCTYPE, name)
		if not frappe.has_permission(REPORT_DOCTYPE, "write", doc=doc):
			raise frappe.PermissionError
	else:
		if not frappe.has_permission(REPORT_DOCTYPE, "create"):
			raise frappe.PermissionError
		doc = frappe.new_doc(REPORT_DOCTYPE)

	doc.title = title
	doc.base_doctype = base_doctype
	doc.description = description or ""
	doc.visibility = visibility or "Private"
	doc.page_size = int(page_size or 20)

	doc.set("related_sources", [])
	for rs in cfg.get("related_sources") or []:
		doc.append(
			"related_sources",
			{
				"alias": rs.get("alias"),
				"related_doctype": rs.get("related_doctype"),
				"join_type": rs.get("join_type") or "Left Join",
				"conditions": json.dumps(rs.get("conditions") or []),
				"is_child_table": 1 if rs.get("is_child_table") else 0,
				"child_parent_field": rs.get("child_parent_field") or "",
			},
		)

	doc.set("calculations", [])
	for c in cfg.get("calculations") or []:
		doc.append(
			"calculations",
			{
				"alias": c.get("alias"),
				"label": c.get("label"),
				"format_type": c.get("format_type") or "Number",
				"expression": json.dumps(c.get("expression") or {}),
			},
		)

	doc.set("columns", [])
	for c in cfg.get("columns") or []:
		doc.append(
			"columns",
			{
				"source": c.get("source") or "",
				"field_path": c.get("field_path"),
				"calculation_alias": c.get("calculation_alias"),
				"label": c.get("label"),
				"fieldtype": c.get("fieldtype"),
				"aggregate": c.get("aggregate") or "",
				"width": c.get("width"),
				"visibility_rule": json.dumps(c.get("visibility_rule")) if c.get("visibility_rule") else None,
				"format_rules": json.dumps(c.get("format_rules") or []) if c.get("format_rules") else None,
			},
		)

	doc.set("filters", [])
	for f in cfg.get("filters") or []:
		doc.append(
			"filters",
			{
				"source": f.get("source") or "",
				"field_path": f.get("field_path"),
				"fieldtype": f.get("fieldtype"),
				"operator": f.get("operator"),
				"value": f.get("value"),
				"value_to": f.get("value_to"),
				"value_list": f.get("value_list"),
				"granularity": f.get("granularity") or "",
				"is_runtime": 1 if f.get("is_runtime") else 0,
			},
		)

	doc.set("group_by", [])
	for g in cfg.get("group_by") or []:
		doc.append(
			"group_by",
			{
				"source": g.get("source") or "",
				"field_path": g.get("field_path"),
				"fieldtype": g.get("fieldtype"),
				"granularity": g.get("granularity") or "",
			},
		)

	doc.set("sort", [])
	for s in cfg.get("sort") or []:
		doc.append(
			"sort",
			{
				"source": s.get("source") or "",
				"field_path": s.get("field_path"),
				"direction": s.get("direction") or "Ascending",
			},
		)

	doc.set("shared_roles", [])
	for r in roles:
		doc.append("shared_roles", r)

	doc.save()

	# Auto-mirror the saved report into the standard Report doctype so it
	# appears under /app/report. Failures here must not block save itself —
	# the Studio doc is the source of truth, the mirror is derived.
	try:
		publish_to_standard_report(doc.name)
	except Exception:
		frappe.log_error(
			title=f"Report Studio: auto-publish failed for {doc.name}",
			message=frappe.get_traceback(),
		)

	return doc.name


@frappe.whitelist()
def load_report(name: str) -> dict:
	doc = frappe.get_doc(REPORT_DOCTYPE, name)
	if not frappe.has_permission(REPORT_DOCTYPE, "read", doc=doc):
		raise frappe.PermissionError

	return {
		"name": doc.name,
		"title": doc.title,
		"description": doc.description or "",
		"base_doctype": doc.base_doctype,
		"visibility": doc.visibility,
		"page_size": doc.page_size or 20,
		"config": doc_to_config(doc),
		"shared_roles": [
			{"role": r.role, "can_edit": bool(r.can_edit)} for r in doc.shared_roles
		],
		"is_published": bool(doc.is_published),
		"linked_report_name": doc.linked_report_name or "",
		"owner": doc.owner,
		"modified": str(doc.modified),
	}


@frappe.whitelist()
def list_reports(search: str = "", mine_only: int = 0, limit: int = 50) -> list[dict]:
	limit = max(1, min(int(limit or 50), 200))
	mine_only = bool(int(mine_only or 0))

	filters = {}
	if search:
		filters["title"] = ["like", f"%{search}%"]
	if mine_only:
		filters["owner"] = frappe.session.user

	rows = frappe.get_list(
		REPORT_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"title",
			"base_doctype",
			"visibility",
			"owner",
			"modified",
		],
		order_by="modified desc",
		limit=limit,
	)
	return rows


@frappe.whitelist()
def delete_report(name: str) -> None:
	if not frappe.db.exists(REPORT_DOCTYPE, name):
		return
	doc = frappe.get_doc(REPORT_DOCTYPE, name)
	if not frappe.has_permission(REPORT_DOCTYPE, "delete", doc=doc):
		raise frappe.PermissionError
	frappe.delete_doc(REPORT_DOCTYPE, name)


@frappe.whitelist()
def touch_last_run(name: str) -> None:
	if not frappe.db.exists(REPORT_DOCTYPE, name):
		return
	doc = frappe.get_doc(REPORT_DOCTYPE, name)
	if frappe.has_permission(REPORT_DOCTYPE, "read", doc=doc):
		frappe.db.set_value(REPORT_DOCTYPE, name, "last_run_at", now())


# ---------------------------------------------------------------------------
# Phase G: Mirror saved reports into Frappe's standard `Report` doctype so they
# show up under /app/report and run via /app/query-report/<name>.
# ---------------------------------------------------------------------------

REPORT_BUILDER_MODULE = "Report Builder"


def _mirror_name_for(doc) -> str:
	# Deterministic, unique: "Title RPT-XXXXX" (no parentheses around the id).
	# Reason: published reports run as is_standard=Yes, which means Frappe
	# writes a Python module on disk named scrub(name). Parentheses are not
	# legal in Python identifiers, so any "(RPT-...)" suffix would produce an
	# unimportable module path. The sanitizer below also strips them out of
	# whatever the user typed in the title.
	title = (doc.title or doc.name).strip()
	title = "".join(ch for ch in title if ch.isalnum() or ch in " -_,.")
	title = " ".join(title.split())
	return f"{title} {doc.name}"


def _validate_studio_name(name: str) -> str:
	if not isinstance(name, str) or not name:
		frappe.throw(frappe._("Report Studio Report name is required."))
	# Studio names are auto-named "RPT-XXXXX" — keep the runner narrow so the
	# report_script can never embed an arbitrary string.
	import re

	if not re.match(r"^[A-Z]{2,10}-[0-9A-Za-z\-]+$", name):
		frappe.throw(frappe._("Invalid Report Studio Report name: {0}").format(name))
	return name


def _render_inline_script(studio) -> str:
	"""Build the self-contained report_script for a Studio doc.

	Validates the config server-side (so we trust it inside safe_exec without
	re-checking permissions there), then concatenates the validated CONFIG
	dict literal with the constant INLINE_RUNNER template.
	"""
	from report_builder.engine import meta_validator, schema as schema_mod
	from report_builder.runtime.inline_runner import INLINE_RUNNER

	cfg = doc_to_config(studio)
	# Validate via existing engine — populates fieldtype/options on every
	# referenced field and rejects bad paths early.
	parsed = schema_mod.parse(cfg)
	meta_validator.validate(parsed)
	# meta_validator mutates parsed in place; rebuild a clean dict for embedding
	# (parsed has dataclass instances which we can't `repr` cleanly).
	cfg = doc_to_config(studio)
	# Backfill the freshly-validated metadata onto the embedded config so the
	# inline runner can format columns without re-running the validator.
	for col in cfg.get("columns") or []:
		if col.get("calculation_alias"):
			continue
		key = (col.get("source") or "", col.get("field_path"))
		# Look up the parsed column with the same key to copy fieldtype.
		for pcol in parsed.columns:
			if pcol.is_calculation:
				continue
			if (pcol.source or "", pcol.field_path) == key:
				col["fieldtype"] = pcol.fieldtype
				break
	for f in cfg.get("filters") or []:
		key = (f.get("source") or "", f.get("field_path"))
		for pf in parsed.filters:
			if (pf.source or "", pf.field_path) == key:
				f["fieldtype"] = pf.fieldtype
				break
	for g in cfg.get("group_by") or []:
		key = (g.get("source") or "", g.get("field_path"))
		for pg in parsed.group_by:
			if (pg.source or "", pg.field_path) == key:
				g["fieldtype"] = pg.fieldtype
				break

	# `repr` produces a Python-parseable dict literal (booleans as True/False,
	# None, escaped strings). Safe to embed verbatim.
	header = "# Auto-generated by Report Studio. Do not edit. Self-contained — no app dependencies.\n"
	return header + "CONFIG = " + repr(cfg) + "\n" + INLINE_RUNNER


def _runtime_filter_field(f: dict, base_doctype: str, source_map: dict) -> list[dict]:
	"""Translate one Studio filter into one or two query-report filter UI defs.
	Between → two fields (`<key>` + `<key>__to`). In/Not In → one field (`<key>__list`).
	Is Set / Is Not Set are skipped (no value to override)."""
	if f.get("operator") in ("Is Set", "Is Not Set"):
		return []

	field_path = f.get("field_path") or ""
	key = field_path.replace(".", "__").replace(":", "__")
	src = f.get("source") or ""
	gran = f.get("granularity") or ""

	# Resolve a friendly label AND the terminal Link target by walking the
	# path against meta. Fall back to the raw segment if a hop fails.
	label_root = source_map.get(src) or base_doctype
	human_label = field_path
	link_target = ""
	try:
		current_dt = label_root
		labels = []
		segs = [s for s in (field_path or "").split(".") if s]
		for i, seg in enumerate(segs):
			is_last = i == len(segs) - 1
			if seg == "name":
				labels.append("ID")
				if is_last:
					link_target = current_dt
				break
			meta = frappe.get_meta(current_dt)
			df = meta.get_field(seg)
			if not df:
				labels.append(seg)
				break
			labels.append(df.label or seg)
			if is_last and df.fieldtype in ("Link", "Dynamic Link") and df.options:
				link_target = df.options
			if df.fieldtype in ("Link", "Table", "Table MultiSelect") and df.options:
				current_dt = df.options
			elif not is_last:
				break
		if labels:
			human_label = " / ".join(labels)
	except Exception:
		pass
	if src:
		human_label = f"{src} · {human_label}"
	label_base = human_label + (f" ({gran})" if gran else "")

	fieldtype = f.get("fieldtype") or "Data"
	df_fieldtype = "Data"
	options = ""
	placeholder = ""

	if fieldtype in ("Date", "Datetime"):
		if gran == "Year":
			df_fieldtype, placeholder = "Data", "YYYY (e.g. 2024)"
		elif gran == "Month":
			df_fieldtype, placeholder = "Data", "YYYY-MM"
		elif gran == "All":
			df_fieldtype, placeholder = "Data", "YYYY  ·  YYYY-MM  ·  YYYY-MM-DD"
		elif fieldtype == "Datetime":
			df_fieldtype = "Datetime"
		else:
			df_fieldtype = "Date"
	elif fieldtype in ("Link", "Dynamic Link"):
		df_fieldtype, options = "Link", f.get("options") or link_target
	elif fieldtype == "Select":
		df_fieldtype, options = "Select", f.get("options") or ""
	elif fieldtype == "Check":
		df_fieldtype = "Check"
	elif fieldtype in ("Int", "Float", "Currency", "Percent"):
		df_fieldtype = "Float"

	op = f.get("operator") or "Equals"

	if op in ("In", "Not In"):
		spec = {
			"fieldname": key + "__list",
			"label": label_base + " (" + op + ")",
			"fieldtype": "Data",
			"default": f.get("value_list") or "",
			"placeholder": "Comma-separated values",
		}
		return [spec]

	primary = {
		"fieldname": key,
		"label": label_base,
		"fieldtype": df_fieldtype,
		"default": f.get("value") or "",
	}
	if options:
		primary["options"] = options
	if placeholder:
		primary["placeholder"] = placeholder

	if op == "Between":
		secondary = dict(primary)
		secondary["fieldname"] = key + "__to"
		secondary["label"] = label_base + " (To)"
		secondary["default"] = f.get("value_to") or ""
		return [primary, secondary]

	return [primary]


def _render_filter_javascript(studio, target_name: str) -> str:
	"""Build the Report.javascript content — `frappe.query_reports[name].filters`
	so end users see a runtime filter bar at /app/query-report/<name>."""
	cfg = doc_to_config(studio)
	source_map = {rs.get("alias"): rs.get("related_doctype") for rs in (cfg.get("related_sources") or [])}
	rt_filters: list[dict] = []
	for f in cfg.get("filters") or []:
		# Only filters explicitly marked "Ask at run time" go in the bar.
		if not f.get("is_runtime"):
			continue
		rt_filters.extend(_runtime_filter_field(f, studio.base_doctype, source_map))
	if not rt_filters:
		return ""
	payload = {"filters": rt_filters}
	return (
		"// Auto-generated by Report Studio. Do not edit.\n"
		"frappe.query_reports[" + json.dumps(target_name) + "] = "
		+ json.dumps(payload, indent=2) + ";\n"
	)


@frappe.whitelist()
def publish_to_standard_report(name: str) -> str:
	"""Create or update the Frappe Report record mirroring a saved Report
	Studio Report. Returns the Frappe Report's name."""
	_validate_studio_name(name)

	if not frappe.db.exists(REPORT_DOCTYPE, name):
		frappe.throw(frappe._("Report Studio Report {0} not found.").format(name))
	studio = frappe.get_doc(REPORT_DOCTYPE, name)
	if not frappe.has_permission(REPORT_DOCTYPE, "read", doc=studio):
		raise frappe.PermissionError
	if not studio.base_doctype:
		frappe.throw(frappe._("Save the report with a Data Source before publishing."))

	target_name = _mirror_name_for(studio)
	previous_name = studio.linked_report_name

	# Rename in place if previously published under a different name (title changed).
	if previous_name and previous_name != target_name and frappe.db.exists("Report", previous_name):
		from frappe.model.rename_doc import rename_doc as _rename_doc

		_rename_doc(
			doctype="Report",
			old=previous_name,
			new=target_name,
			force=True,
			merge=False,
			ignore_permissions=True,
		)

	script = _render_inline_script(studio)
	javascript = _render_filter_javascript(studio, target_name)
	# is_standard=Yes is intentional: it makes Frappe export the Report's
	# JSON + .py + .js files to disk under report_builder/report/<scrub>/.
	# Those files are what gets committed to git and shipped to teammates.
	# `report_script` is still populated so the doc round-trips losslessly,
	# but the on-disk execute() (rewritten below) is what runs.
	report_fields = {
		"report_type": "Script Report",
		"is_standard": "Yes",
		"ref_doctype": studio.base_doctype,
		"module": REPORT_BUILDER_MODULE,
		"disabled": 0,
		"report_script": script,
		"javascript": javascript,
	}

	if frappe.db.exists("Report", target_name):
		report = frappe.get_doc("Report", target_name)
		for k, v in report_fields.items():
			setattr(report, k, v)
		report.save(ignore_permissions=True)
	else:
		report = frappe.get_doc({
			"doctype": "Report",
			"report_name": target_name,
			**report_fields,
		})
		report.flags.ignore_permissions = True
		report.insert(ignore_permissions=True)

	# Frappe's create_report_py wrote a stub `execute()` that returns
	# ([], []). Overwrite it with a delegating runner that calls back into
	# run_for_standard_report — same code path /app/query-report/<name>
	# uses, so the on-disk file is the real source of truth.
	_write_delegating_execute(target_name, studio_name=studio.name)

	frappe.db.set_value(REPORT_DOCTYPE, name, {
		"linked_report_name": target_name,
		"is_published": 1,
	})
	return target_name


def _report_folder(report_name: str) -> str:
	import os

	return os.path.join(
		frappe.get_app_path("report_builder"),
		"report_builder",
		"report",
		frappe.scrub(report_name),
	)


_DELEGATING_EXECUTE_TEMPLATE = '''# Auto-generated by Report Studio. Do not edit by hand.
# This file is the on-disk runner for a published Studio report. The Studio
# config (columns, filters, joins, calculations) lives on the linked
# `Report Studio Report` doc and is shipped via the `fixtures` hook. This
# file just delegates to the report_builder engine so a teammate who pulls
# the app gets a working report after `bench migrate`.
from report_builder.api.builder import run_for_standard_report

STUDIO_REPORT = {studio_name!r}


def execute(filters=None):
\tresult = run_for_standard_report(STUDIO_REPORT, filters)
\tcolumns = result["columns"]
\trows = result["rows"]
\treturn columns, rows
'''


def _write_delegating_execute(report_name: str, studio_name: str) -> None:
	"""Overwrite Frappe's boilerplate execute() with one that delegates to
	run_for_standard_report. Called after the Report record is saved with
	is_standard=Yes (which is what triggers Frappe to create the folder
	and write the stub in the first place)."""
	import os

	_validate_studio_name(studio_name)
	scrubbed = frappe.scrub(report_name)
	if not scrubbed or "/" in scrubbed or ".." in scrubbed:
		frappe.throw(frappe._("Invalid report name for on-disk stub: {0}").format(report_name))

	app_root = os.path.realpath(frappe.get_app_path("report_builder"))
	folder = os.path.realpath(_report_folder(report_name))
	target = os.path.realpath(os.path.join(folder, scrubbed + ".py"))

	# Defence-in-depth: refuse to write anywhere outside the app's report folder.
	if not (folder.startswith(app_root + os.sep) and target.startswith(folder + os.sep)):
		frappe.throw(frappe._("Refusing to write report stub outside app directory."))

	if not os.path.isdir(folder):
		# is_standard=Yes + developer_mode should have created this. If it
		# didn't (e.g. developer_mode off), there's nothing on disk to
		# overwrite — DB-level report_script still runs via execute_script.
		return
	content = _DELEGATING_EXECUTE_TEMPLATE.format(studio_name=studio_name)
	with open(target, "w") as f:
		f.write(content)


@frappe.whitelist()
def unpublish_standard_report(name: str) -> None:
	"""Delete the linked Frappe Report record (if any)."""
	_validate_studio_name(name)
	if not frappe.db.exists(REPORT_DOCTYPE, name):
		return
	studio = frappe.get_doc(REPORT_DOCTYPE, name)
	if not frappe.has_permission(REPORT_DOCTYPE, "write", doc=studio):
		raise frappe.PermissionError

	linked = studio.linked_report_name
	if linked and frappe.db.exists("Report", linked):
		frappe.delete_doc("Report", linked, ignore_permissions=True, delete_permanently=True)

	frappe.db.set_value(REPORT_DOCTYPE, name, {
		"linked_report_name": "",
		"is_published": 0,
	})


@frappe.whitelist()
def run_for_standard_report(name: str, filters=None):
	"""Called by the mirrored Frappe Report's report_script (via frappe.call,
	from inside safe_exec). Returns a dict with columns and rows. The script
	template unpacks these into Frappe's runner."""
	_validate_studio_name(name)
	if not frappe.db.exists(REPORT_DOCTYPE, name):
		frappe.throw(frappe._("Report {0} no longer exists.").format(name))
	studio = frappe.get_doc(REPORT_DOCTYPE, name)
	if not frappe.has_permission(REPORT_DOCTYPE, "read", doc=studio):
		raise frappe.PermissionError

	from report_builder.engine.schema import MAX_EXPORT_ROWS

	cfg = doc_to_config(studio)
	res = query_engine.build_and_run(cfg, page=1, page_size=MAX_EXPORT_ROWS)

	columns = []
	for c in res.columns:
		safe_fname = (c.get("fieldname") or "field").replace(".", "__").replace(":", "__")
		ftype = c.get("fieldtype") or "Data"
		options = c.get("link_doctype") or c.get("options") or ""
		# Frappe's query_report.get_filtered_data walks Link columns and calls
		# get_meta(options); an empty options raises `DocType  not found`. The
		# Studio column row doesn't carry the link target, so drop to Data.
		if ftype in ("Link", "Dynamic Link") and not options:
			ftype = "Data"
		columns.append({
			"label": c.get("label") or safe_fname,
			"fieldname": safe_fname,
			"fieldtype": ftype,
			"options": options,
			"width": c.get("width") or 160,
		})

	rows = []
	for raw in res.rows:
		row_dict = {}
		for col_meta, value in zip(columns, raw, strict=False):
			row_dict[col_meta["fieldname"]] = value
		rows.append(row_dict)

	return {
		"columns": columns,
		"rows": rows,
	}
