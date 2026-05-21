# SPDX-License-Identifier: MIT
import json
from typing import Optional

import frappe
from frappe.utils.csvutils import to_csv

from report_builder.api.builder import doc_to_config
from report_builder.engine import query_engine
from report_builder.engine.schema import MAX_EXPORT_ROWS

REPORT_DOCTYPE = "Report Studio Report"
ALLOWED_FORMATS = {"xlsx", "csv", "pdf"}


def _slugify(value: str) -> str:
	out = []
	for ch in (value or "report").lower():
		if ch.isalnum():
			out.append(ch)
		elif ch in (" ", "-", "_"):
			out.append("-")
	slug = "".join(out).strip("-") or "report"
	return slug[:80]


def _build_config_from_saved(name: str) -> tuple[str, dict]:
	doc = frappe.get_doc(REPORT_DOCTYPE, name)
	if not frappe.has_permission(REPORT_DOCTYPE, "read", doc=doc):
		raise frappe.PermissionError
	return doc.title or name, doc_to_config(doc)


@frappe.whitelist()
def export_report(name: Optional[str] = None, config=None, fmt: str = "xlsx", title: str = ""):
	fmt = (fmt or "xlsx").lower()
	if fmt not in ALLOWED_FORMATS:
		frappe.throw(frappe._("Unsupported export format: {0}").format(fmt))

	if name:
		report_title, cfg = _build_config_from_saved(name)
	else:
		if isinstance(config, str):
			cfg = json.loads(config)
		else:
			cfg = config or {}
		report_title = title or "report"

	result = query_engine.run_full(cfg, max_rows=MAX_EXPORT_ROWS)

	headers = [c["label"] for c in result.columns]
	rows = result.rows

	filename_base = _slugify(report_title)

	if fmt == "xlsx":
		return _respond_xlsx(filename_base, headers, rows)
	if fmt == "csv":
		return _respond_csv(filename_base, headers, rows)
	return _respond_pdf(filename_base, report_title, headers, rows)


def _respond_xlsx(filename_base: str, headers: list, rows: list) -> None:
	from frappe.utils.xlsxutils import build_xlsx_response

	data = [headers] + [list(r) for r in rows]
	build_xlsx_response(data, filename_base)


def _respond_csv(filename_base: str, headers: list, rows: list) -> None:
	csv_rows = [headers] + [[_csv_cell(v) for v in r] for r in rows]
	csv_text = to_csv(csv_rows)
	frappe.response["filename"] = f"{filename_base}.csv"
	frappe.response["filecontent"] = csv_text
	frappe.response["type"] = "binary"


def _csv_cell(value):
	if value is None:
		return ""
	return value


def _respond_pdf(filename_base: str, title: str, headers: list, rows: list) -> None:
	from frappe.utils.pdf import get_pdf

	# Template path is a hardcoded literal (not user input) and the template
	# uses Jinja auto-escaping for all context values — no SSTI surface.
	html = frappe.render_template(  # nosemgrep: frappe-ssti
		"report_builder/templates/includes/export_table.html",
		{
			"title": title,
			"headers": headers,
			"rows": rows,
			"row_count": len(rows),
		},
	)
	pdf_bytes = get_pdf(html)
	frappe.response["filename"] = f"{filename_base}.pdf"
	frappe.response["filecontent"] = pdf_bytes
	frappe.response["type"] = "pdf"
