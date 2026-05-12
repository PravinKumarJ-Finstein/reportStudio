# SPDX-License-Identifier: MIT
import frappe
from frappe.model.document import Document


class ReportStudioReport(Document):
	def validate(self):
		self._normalize_visibility()
		self._clamp_page_size()
		self._validate_shared_roles()

	def _normalize_visibility(self):
		if self.visibility not in ("Private", "Public", "Shared with Roles"):
			self.visibility = "Private"
		self.is_public = 1 if self.visibility == "Public" else 0
		if self.visibility != "Shared with Roles":
			self.shared_roles = []

	def _clamp_page_size(self):
		size = int(self.page_size or 20)
		self.page_size = max(5, min(500, size))

	def _validate_shared_roles(self):
		seen = set()
		valid_rows = []
		for row in self.shared_roles or []:
			if not row.role:
				continue
			if row.role in seen:
				continue
			if not frappe.db.exists("Role", row.role):
				frappe.throw(frappe._("Role {0} does not exist").format(row.role))
			seen.add(row.role)
			valid_rows.append(row)
		self.shared_roles = valid_rows

	def on_update(self):
		self._sync_doc_shares()

	def on_trash(self):
		frappe.db.delete(
			"DocShare",
			{"share_doctype": "Report Studio Report", "share_name": self.name},
		)
		# Cascade: drop the mirrored Frappe Report record if present.
		linked = self.linked_report_name
		if linked and frappe.db.exists("Report", linked):
			try:
				frappe.delete_doc("Report", linked, ignore_permissions=True, delete_permanently=True)
			except Exception:
				frappe.log_error(
					title="Report Studio: cascade delete failed",
					message=frappe.get_traceback(),
				)

	def _sync_doc_shares(self):
		existing = frappe.get_all(
			"DocShare",
			filters={
				"share_doctype": "Report Studio Report",
				"share_name": self.name,
			},
			fields=["name", "user", "read", "write"],
		)
		existing_by_user = {row.user: row for row in existing}

		desired_users = {}
		if self.visibility == "Shared with Roles":
			for row in self.shared_roles or []:
				users = frappe.get_all(
					"Has Role",
					filters={"role": row.role, "parenttype": "User"},
					pluck="parent",
				)
				for u in users:
					if u in ("Administrator", "Guest"):
						continue
					if not frappe.db.exists("User", {"name": u, "enabled": 1}):
						continue
					can_edit = bool(row.can_edit)
					if u not in desired_users or can_edit:
						desired_users[u] = can_edit

		for user, can_edit in desired_users.items():
			row = existing_by_user.get(user)
			if row is None:
				frappe.get_doc(
					{
						"doctype": "DocShare",
						"share_doctype": "Report Studio Report",
						"share_name": self.name,
						"user": user,
						"read": 1,
						"write": 1 if can_edit else 0,
						"everyone": 0,
					}
				).insert(ignore_permissions=True)
			elif bool(row.write) != can_edit:
				frappe.db.set_value("DocShare", row.name, "write", 1 if can_edit else 0)

		for user, row in existing_by_user.items():
			if user not in desired_users:
				frappe.delete_doc("DocShare", row.name, ignore_permissions=True)