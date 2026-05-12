# SPDX-License-Identifier: MIT
"""One-time backfill: every existing Report Studio Report becomes visible
in /app/report by mirroring it into a Frappe Report record.

Idempotent — re-running the patch updates already-published reports in place.
Failures on individual reports are logged and skipped so a single bad doc
doesn't block migrate.
"""

import frappe


def execute():
	from report_builder.api.builder import publish_to_standard_report

	if not frappe.db.has_table("Report Studio Report"):
		return

	rows = frappe.get_all(
		"Report Studio Report",
		fields=["name", "is_published"],
		order_by="creation asc",
	)
	if not rows:
		return

	previous_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		for row in rows:
			try:
				publish_to_standard_report(row.name)
			except Exception:
				frappe.log_error(
					title=f"Report Studio backfill failed: {row.name}",
					message=frappe.get_traceback(),
				)
	finally:
		frappe.set_user(previous_user)
