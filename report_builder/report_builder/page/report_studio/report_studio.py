# SPDX-License-Identifier: MIT
import frappe


def get_context(context):
	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.PermissionError
	return context
