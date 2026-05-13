# SPDX-License-Identifier: MIT
"""Migrate mirrored Reports from the old "Title (RPT-NNNN)" name format to
"Title RPT-NNNN" (no parentheses).

Why: published Studio reports are `is_standard=Yes`, so Frappe writes a
Python module on disk at
`report_builder/report_builder/report/<scrub(name)>/`. Python identifiers
cannot contain parentheses, so the old folder names (e.g.
`pr_demo_2_(rpt_00065)`) are unimportable — every run crashes with
ModuleNotFoundError. The new format scrubs to `pr_demo_2_rpt_00065`,
which is a valid identifier.

For each affected Report:
  1. Rename the Report record in DB (frappe.model.rename_doc).
  2. Wipe the old `(...)` folder Frappe exported.
  3. Republish via publish_to_standard_report so the new folder gets a
     proper delegating execute() instead of the boilerplate stub.
  4. Update the linked_report_name on the Studio doc.

Idempotent: already-renamed reports are skipped.
"""

import os
import shutil

import frappe


def execute():
	from frappe.model.rename_doc import rename_doc

	from report_builder.api.builder import (
		REPORT_DOCTYPE,
		publish_to_standard_report,
	)

	report_dir = os.path.join(
		frappe.get_app_path("report_builder"),
		"report_builder",
		"report",
	)

	# Find every Report Builder mirror whose name still has the old "(RPT-...)"
	# suffix. Match is structural (a "(" near the end) — we don't pin a regex
	# because RPT names can vary.
	rows = frappe.get_all(
		"Report",
		filters={"module": "Report Builder"},
		fields=["name"],
	)

	previous_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		for row in rows:
			old_name = row.name
			if "(" not in old_name or ")" not in old_name:
				continue

			new_name = old_name.replace("(", "").replace(")", "")
			new_name = " ".join(new_name.split())  # collapse double-spaces
			if new_name == old_name:
				continue

			# Rename the Report row (force=True bypasses the standard-report
			# rename guard; we're the publisher, we own this doc).
			try:
				rename_doc(
					doctype="Report",
					old=old_name,
					new=new_name,
					force=True,
					merge=False,
					ignore_permissions=True,
				)
			except Exception:
				frappe.log_error(
					title=f"Report Studio: rename failed {old_name} -> {new_name}",
					message=frappe.get_traceback(),
				)
				continue

			# Wipe the old `(...)` folder on disk — it's unimportable and
			# Frappe won't garbage-collect it on its own.
			old_folder = os.path.join(report_dir, frappe.scrub(old_name))
			if os.path.isdir(old_folder):
				shutil.rmtree(old_folder, ignore_errors=True)

			# Find the Studio doc that pointed at this Report and update
			# its linked_report_name (otherwise the next publish would try
			# to rename old_name -> new_name again).
			studio_name = frappe.db.get_value(
				REPORT_DOCTYPE,
				{"linked_report_name": old_name},
				"name",
			)
			if studio_name:
				frappe.db.set_value(
					REPORT_DOCTYPE,
					studio_name,
					"linked_report_name",
					new_name,
					update_modified=False,
				)
				# Republish to write the delegating execute() under the new
				# folder. If it fails (e.g. base_doctype missing), the row
				# is still renamed and runnable via report_script.
				try:
					publish_to_standard_report(studio_name)
				except Exception:
					frappe.log_error(
						title=f"Report Studio: republish failed for {studio_name}",
						message=frappe.get_traceback(),
					)
	finally:
		frappe.set_user(previous_user)
		frappe.db.commit()
