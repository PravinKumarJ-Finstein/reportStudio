# SPDX-License-Identifier: MIT
"""Drop the deprecated Report Studio Chart child doctype and the `charts`
Table field on Report Studio Report.

The Studio chart feature was removed. The doctype JSON and Python files for
"Report Studio Chart" have already been deleted from the app — but Frappe's
DocType records, DB tables, and DocField rows still reference them. Any code
path that calls `frappe.new_doc("Report Studio Chart")` (e.g. setting child
table defaults when saving a Report Studio Report) blows up with
ModuleNotFoundError because the controller can no longer be imported.

This patch runs pre_model_sync so the cleanup happens BEFORE Frappe tries to
re-sync the Report Studio Report DocType from JSON (which would re-load meta
and try to import the chart controller).

Idempotent: skips silently if the chart doctype or field is already gone.
"""

import frappe


def execute():
	# Remove the `charts` field from Report Studio Report's DocField rows so
	# Frappe stops walking it during set_defaults.
	if frappe.db.has_table("DocField"):
		frappe.db.delete(
			"DocField",
			{"parent": "Report Studio Report", "fieldname": "charts"},
		)
		frappe.db.delete(
			"DocField",
			{"parent": "Report Studio Report", "fieldname": "section_charts"},
		)

	# Drop the chart child table — its data is no longer referenced.
	# DDL: literal SQL with no user input. Required because Frappe's ORM does
	# not expose a DROP TABLE primitive.
	if frappe.db.has_table("tabReport Studio Chart"):
		frappe.db.sql("DROP TABLE IF EXISTS `tabReport Studio Chart`")  # nosemgrep: frappe-dont-use-frappe-db-sql

	# Delete the DocType record itself (and any leftover meta).
	if frappe.db.exists("DocType", "Report Studio Chart"):
		# Use db.delete instead of doc.delete so we skip the controller import.
		frappe.db.delete("DocType", {"name": "Report Studio Chart"})
		frappe.db.delete("DocField", {"parent": "Report Studio Chart"})
		frappe.db.delete("Custom Field", {"dt": "Report Studio Chart"})
		frappe.db.delete("Property Setter", {"doc_type": "Report Studio Chart"})

	frappe.clear_cache(doctype="Report Studio Report")
	frappe.db.commit()
