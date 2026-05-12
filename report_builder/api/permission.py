# SPDX-License-Identifier: MIT
import frappe

REPORT_STUDIO_ROLES = ("System Manager", "Report Studio User")


def has_app_permission():
	user = frappe.session.user
	if user == "Administrator":
		return True
	user_roles = set(frappe.get_roles(user))
	return any(r in user_roles for r in REPORT_STUDIO_ROLES)


def report_query_conditions(user=None):
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	roles = set(frappe.get_roles(user))
	if "System Manager" in roles:
		return ""

	user_safe = frappe.db.escape(user)
	return (
		f"(`tabReport Studio Report`.owner = {user_safe}"
		f" or `tabReport Studio Report`.is_public = 1"
		f" or exists ("
		f"  select 1 from `tabDocShare`"
		f"  where `tabDocShare`.share_doctype = 'Report Studio Report'"
		f"  and `tabDocShare`.share_name = `tabReport Studio Report`.name"
		f"  and `tabDocShare`.user = {user_safe}"
		f"  and `tabDocShare`.read = 1"
		f"))"
	)


def has_report_permission(doc, user=None, permission_type="read"):
	user = user or frappe.session.user
	if user == "Administrator":
		return True

	roles = set(frappe.get_roles(user))
	if "System Manager" in roles:
		return True

	if doc.owner == user:
		return True

	if permission_type == "read" and getattr(doc, "is_public", 0):
		return True

	share = frappe.db.exists(
		"DocShare",
		{
			"share_doctype": "Report Studio Report",
			"share_name": doc.name,
			"user": user,
			permission_type: 1,
		},
	)
	return bool(share)
