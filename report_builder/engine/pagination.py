# SPDX-License-Identifier: MIT
from frappe.utils import cint

from report_builder.engine.schema import MAX_PAGE_SIZE


def clamp_page_size(value, default=20):
	value = cint(value or default)
	return max(5, min(MAX_PAGE_SIZE, value))


def clamp_page(value):
	value = cint(value or 1)
	return max(1, min(value, 100000))


def apply(query, page: int, page_size: int):
	offset = (page - 1) * page_size
	return query.limit(page_size).offset(offset)
