# SPDX-License-Identifier: MIT
"""One-time refresh: every existing Frappe Report record mirrored from a
Report Studio Report is re-published with the new self-contained inline runner.
After this patch runs, the `report_script` field no longer references the
report_builder app — making each Report record portable to any Frappe site.

Idempotent: re-running produces the same script bytes.
Failures on individual reports are logged and skipped.
"""

import frappe


def execute():
	from report_builder.api.builder import publish_to_standard_report

	if not frappe.db.has_table("Report Studio Report"):
		return

	rows = frappe.get_all(
		"Report Studio Report",
		fields=["name"],
		order_by="creation asc",
	)
	if not rows:
		return

	# Run the republish as Administrator so updated Report records get full
	# permissions and Administrator ownership, then restore in `finally`.
	previous_user = frappe.session.user
	frappe.set_user("Administrator")  # nosemgrep: frappe-setuser
	try:
		for row in rows:
			try:
				publish_to_standard_report(row.name)
			except Exception:
				frappe.log_error(
					title=f"Report Studio: republish (inline runner) failed for {row.name}",
					message=frappe.get_traceback(),
				)
	finally:
		frappe.set_user(previous_user)  # nosemgrep: frappe-setuser
