/**
 * Report Studio — no-code report builder for Frappe.
 * Single-file bundle: state, components, and wiring.
 */

frappe.provide("frappe.report_studio");

const RB_API = {
	getAllowedDoctypes: (search = "") =>
		frappe.call({
			method: "report_builder.api.metadata.get_allowed_doctypes",
			args: { search, limit: 100 },
		}).then((r) => r.message || []),

	getFields: (doctype) =>
		frappe.call({
			method: "report_builder.api.metadata.get_fields",
			args: { doctype, depth: 1 },
		}).then((r) => r.message || []),

	getChildTables: (doctype) =>
		frappe.call({
			method: "report_builder.api.metadata.get_child_tables",
			args: { doctype },
		}).then((r) => r.message || []),

	preview: (config, page = 1, page_size = 20) =>
		frappe.call({
			method: "report_builder.api.builder.preview",
			args: { config: JSON.stringify(config), page, page_size },
		}).then((r) => r.message),

	saveReport: (payload) => {
		console.log("[ReportStudio] frappe.call → save_report", payload);
		return frappe.call({
			method: "report_builder.api.builder.save_report",
			args: payload,
		}).then((r) => {
			console.log("[ReportStudio] save_report response", r);
			if (r && r.exc) {
				const err = new Error("Server error during save");
				err._server_messages = r._server_messages || "";
				throw err;
			}
			return r ? r.message : null;
		});
	},

	loadReport: (name) =>
		frappe.call({
			method: "report_builder.api.builder.load_report",
			args: { name },
		}).then((r) => r.message),

	listReports: (search = "", mine_only = 0) =>
		frappe.call({
			method: "report_builder.api.builder.list_reports",
			args: { search, mine_only, limit: 100 },
		}).then((r) => r.message || []),

	deleteReport: (name) =>
		frappe.call({
			method: "report_builder.api.builder.delete_report",
			args: { name },
		}),

	publishStandardReport: (name) =>
		frappe.call({
			method: "report_builder.api.builder.publish_to_standard_report",
			args: { name },
		}).then((r) => r.message),

	unpublishStandardReport: (name) =>
		frappe.call({
			method: "report_builder.api.builder.unpublish_standard_report",
			args: { name },
		}),
};

const TEXT_FIELDTYPES = new Set([
	"Data", "Small Text", "Long Text", "Text", "Text Editor",
	"Code", "Read Only", "Markdown Editor",
	"Phone", "Email", "Password", "Barcode", "JSON", "Color",
	"HTML Editor", "HTML",
]);
const NUMERIC_FIELDTYPES = new Set(["Int", "Float", "Currency", "Percent", "Duration", "Rating"]);
const DATE_FIELDTYPES = new Set(["Date"]);
const DATETIME_FIELDTYPES = new Set(["Datetime"]);
const TIME_FIELDTYPES = new Set(["Time"]);
const LINK_FIELDTYPES = new Set(["Link", "Dynamic Link"]);
const SELECT_FIELDTYPES = new Set(["Select", "Autocomplete"]);

function operatorsFor(fieldtype) {
	if (DATE_FIELDTYPES.has(fieldtype) || DATETIME_FIELDTYPES.has(fieldtype) || TIME_FIELDTYPES.has(fieldtype)) {
		return ["Equals", "Greater Than", "Less Than", "Between", "Is Set", "Is Not Set"];
	}
	if (NUMERIC_FIELDTYPES.has(fieldtype)) {
		return ["Equals", "Not Equals", "Greater Than", "Less Than", "Between", "In", "Not In", "Is Set", "Is Not Set"];
	}
	if (LINK_FIELDTYPES.has(fieldtype)) {
		return ["Equals", "Not Equals", "In", "Not In", "Is Set", "Is Not Set"];
	}
	if (SELECT_FIELDTYPES.has(fieldtype)) {
		return ["Equals", "Not Equals", "In", "Not In", "Is Set", "Is Not Set"];
	}
	if (fieldtype === "Check") {
		return ["Equals"];
	}
	// Treat unknown fieldtypes as text — better to over-allow operators
	// than to leave users with only "Is Set / Is Not Set" on, e.g., a Phone
	// or custom field whose type we don't explicitly recognize.
	return ["Equals", "Not Equals", "Contains", "Does Not Contain", "In", "Not In", "Is Set", "Is Not Set"];
}

// ---------------------------------------------------------------------------
// Drag-ghost cursor lock
// ---------------------------------------------------------------------------
// SortableJS in `forceFallback` mode positions the floating ghost via
// `transform: translate3d(...)` based on the original element's viewport rect
// + cursor delta. Any ancestor `transform`, body padding, or a scrollable
// container can throw that math off and the ghost ends up far from the
// cursor.
//
// Bypass: while a drag is in progress, attach a mousemove listener that
// force-positions `.sortable-fallback` at fixed viewport coordinates
// directly under the cursor. CSS then sets `transform: none` so Sortable's
// own positioning is overridden.
let _ghostFollow = null;
let _lastCursor = { x: 0, y: 0 };

// Track the cursor globally so onStart can place the ghost at the right
// position immediately, before the user moves the mouse another pixel.
document.addEventListener("mousemove", (e) => {
	_lastCursor.x = e.clientX;
	_lastCursor.y = e.clientY;
}, { passive: true, capture: true });

function placeGhost(x, y) {
	const ghost = document.body.querySelector(".sortable-fallback");
	if (!ghost) return;
	// setProperty with "" priority guarantees the inline style takes effect
	// over the .sortable-fallback CSS rule (no !important fight).
	ghost.style.setProperty("left", (x + 14) + "px", "");
	ghost.style.setProperty("top", (y + 8) + "px", "");
}

function startGhostFollow() {
	stopGhostFollow();
	// Place at current cursor immediately (before the next mousemove fires).
	requestAnimationFrame(() => placeGhost(_lastCursor.x, _lastCursor.y));
	_ghostFollow = (e) => placeGhost(e.clientX, e.clientY);
	document.addEventListener("mousemove", _ghostFollow, { passive: true });
}
function stopGhostFollow() {
	if (_ghostFollow) {
		document.removeEventListener("mousemove", _ghostFollow);
		_ghostFollow = null;
	}
}

function makeControl({ fieldtype, options, value, parent, placeholder, label }) {
	const df = { fieldname: "v", fieldtype, options, label: label || "", placeholder };
	const ctrl = frappe.ui.form.make_control({ df, parent, render_input: true, only_input: true });
	if (value !== undefined && value !== null && value !== "") {
		ctrl.set_value(value);
	}
	return ctrl;
}

class ReportStudio {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Report Studio"),
			single_column: true,
		});

		this.state = this._emptyState();
		this.fieldsByPath = new Map();
		this.lastResult = null;

		this._buildLayout();
		this._buildToolbar();
		this._wireDoctypePicker();
		this._wireBuckets();
		this._wirePreview();
		this._renderAllBuckets();
		this._renderRelatedList();
		this._renderCalcList();
	}

	_emptyState() {
		return {
			docName: null,
			title: "",
			description: "",
			visibility: "Private",
			pageSize: 20,
			baseDoctype: null,
			relatedSources: [],
			calculations: [],
			columns: [],
			filters: [],
			groupBy: [],
			sharedRoles: [],
			page: 1,
			isPublished: false,
			linkedReportName: "",
		};
	}

	_fieldKey(source, path) {
		return source ? `${source}|${path}` : path;
	}

	_cleanupModalArtifacts() {
		stopGhostFollow();
		document.body.classList.remove("rs-dragging");
		const clear = () => {
			const openModals = $(".modal.show:visible, .modal.in:visible").length;
			if (openModals) return;
			$("body").removeClass("modal-open").css("padding-right", "");
			$(".modal-backdrop").remove();
		};
		setTimeout(clear, 0);
		setTimeout(clear, 180);
	}

	_sourceLabel(source) {
		if (!source) return this.state.baseDoctype || __("Base");
		const rs = this.state.relatedSources.find((r) => r.alias === source);
		return rs ? `${rs.alias} (${rs.related_doctype})` : source;
	}

	_buildLayout() {
		const $body = $(this.page.body);
		$body.html(`
			<div class="rs-root">
				<div class="rs-source-row">
					<div class="rs-source-picker"></div>
					<div class="rs-source-status text-muted"></div>
				</div>
				<div class="rs-builder">
					<div class="rs-palette-wrap">
						<div class="rs-section-title">${__("Available Fields")}</div>
						<input type="search"
							class="form-control input-sm rs-palette-search"
							placeholder="${__("Search fields…")}" />
						<div class="rs-palette" data-empty-text="${__("Select a DocType first.")}"></div>
					</div>
					<div class="rs-right-col">
						<div class="rs-extras">
							<div class="rs-card rs-related-card">
								<div class="rs-card-header">
									<span>${__("Join DocTypes")}</span>
									<button class="btn btn-default btn-xs rs-add-related">+ ${__("Add Join")}</button>
								</div>
								<div class="rs-card-help">${__("Bring in another DocType and match by field values.")}</div>
								<div class="rs-related-list"></div>
							</div>
							<div class="rs-card rs-calc-card">
								<div class="rs-card-header">
									<span>${__("Calculations")}</span>
									<button class="btn btn-default btn-xs rs-add-calc">+ ${__("Add Calculation")}</button>
								</div>
								<div class="rs-card-help">${__("Build new fields like Net = Gross − Discount.")}</div>
								<div class="rs-calc-list"></div>
							</div>
						</div>
						<div class="rs-buckets">
							<div class="rs-bucket rs-bucket-columns" data-bucket="columns">
								<div class="rs-bucket-title">${__("Columns")}</div>
								<div class="rs-bucket-help">${__("Drag fields here to show them.")}</div>
								<div class="rs-bucket-rows"></div>
							</div>
							<div class="rs-bucket" data-bucket="filters">
								<div class="rs-bucket-title">${__("Filters")}</div>
								<div class="rs-bucket-help">${__("Drag fields here to limit your data.")}</div>
								<div class="rs-bucket-rows"></div>
							</div>
							<div class="rs-bucket" data-bucket="groupBy">
								<div class="rs-bucket-title">${__("Group Data By")}</div>
								<div class="rs-bucket-help">${__("Drag a field to group rows.")}</div>
								<div class="rs-bucket-rows"></div>
							</div>
						</div>
					</div>
				</div>
				<div class="rs-preview-wrap">
					<div class="rs-preview-toolbar">
						<button class="btn btn-primary btn-sm rs-preview-btn">
							<span class="rs-preview-label">${__("Preview Report")}</span>
						</button>
						<div class="rs-pager">
							<button class="btn btn-default btn-xs rs-page-prev">&laquo;</button>
							<span class="rs-page-info text-muted"></span>
							<button class="btn btn-default btn-xs rs-page-next">&raquo;</button>
							<select class="rs-page-size form-control input-xs">
								<option value="10">10</option>
								<option value="20" selected>20</option>
								<option value="50">50</option>
								<option value="100">100</option>
								<option value="200">200</option>
							</select>
						</div>
					</div>
					<div class="rs-preview"></div>
				</div>
			</div>
		`);

		this.$root = $body.find(".rs-root");
		this.$palette = $body.find(".rs-palette");
		this.$sourcePicker = $body.find(".rs-source-picker");
		this.$sourceStatus = $body.find(".rs-source-status");
		this.$relatedList = $body.find(".rs-related-list");
		this.$calcList = $body.find(".rs-calc-list");
		this.$paletteSearch = $body.find(".rs-palette-search");
		this.$paletteSearch.on("input", () => this._applyPaletteFilter(this.$paletteSearch.val()));
		$body.find(".rs-add-related").on("click", () => this._addRelatedDialog());
		$body.find(".rs-add-calc").on("click", () => this._addCalcDialog());
		this.$buckets = {
			columns: $body.find('.rs-bucket[data-bucket="columns"] .rs-bucket-rows'),
			filters: $body.find('.rs-bucket[data-bucket="filters"] .rs-bucket-rows'),
			groupBy: $body.find('.rs-bucket[data-bucket="groupBy"] .rs-bucket-rows'),
		};
		this.$preview = $body.find(".rs-preview");
		this.$previewBtn = $body.find(".rs-preview-btn");
		this.$pager = $body.find(".rs-pager");
		this.$pageInfo = $body.find(".rs-page-info");
	}

	_buildToolbar() {
		this.page.set_primary_action(__("Save"), () => this._save(false), "save");
		this.page.add_menu_item(__("Save As New"), () => this._save(true));
		this.page.add_menu_item(__("Open Report"), () => this._openReportDialog());
		this.page.add_menu_item(__("New Report"), () => this._reset());
		this.page.add_menu_item(__("Sharing & Visibility"), () => this._shareDialog());
		this.page.add_menu_item(__("Publish as Standard Report"), () => this._publishStandardReport());
		this.page.add_menu_item(__("Unpublish from Standard Report"), () => this._unpublishStandardReport());
		this.page.add_menu_item(__("Delete Report"), () => this._deleteCurrent(), false, "Ctrl+Shift+D");

		this.page.add_action_icon("download", () => this._exportDialog(), __("Export"));
		this.page.add_action_icon("file", () => this._openReportDialog(), __("Open"));
	}

	async _publishStandardReport() {
		if (!this.state.docName) {
			frappe.msgprint({
				title: __("Save first"),
				message: __("Save the report before publishing it to /app/report."),
				indicator: "orange",
			});
			return;
		}
		try {
			const mirrorName = await RB_API.publishStandardReport(this.state.docName);
			this.state.isPublished = true;
			this.state.linkedReportName = mirrorName;
			const url = `/app/query-report/${encodeURIComponent(mirrorName)}`;
			frappe.show_alert(
				{
					message: __(
						`<b>Published as ${frappe.utils.escape_html(mirrorName)}</b> &nbsp; <a href="${url}" target="_blank">${__("open report →")}</a>`
					),
					indicator: "green",
				},
				12
			);
		} catch (e) {
			console.error("[ReportStudio] publish failed", e);
			frappe.msgprint({
				title: __("Could not publish"),
				message: (e && (e.message || e._server_messages)) || __("See browser console."),
				indicator: "red",
			});
		}
	}

	async _unpublishStandardReport() {
		if (!this.state.docName) return;
		if (!this.state.isPublished) {
			frappe.show_alert({
				message: __("This report is not published."),
				indicator: "orange",
			});
			return;
		}
		const ok = await this._confirm(__("Remove this report from /app/report?"));
		if (!ok) return;
		try {
			await RB_API.unpublishStandardReport(this.state.docName);
			this.state.isPublished = false;
			this.state.linkedReportName = "";
			frappe.show_alert({
				message: __("Unpublished from /app/report"),
				indicator: "blue",
			});
		} catch (e) {
			console.error("[ReportStudio] unpublish failed", e);
			frappe.msgprint({
				title: __("Could not unpublish"),
				message: (e && (e.message || e._server_messages)) || "",
				indicator: "red",
			});
		}
	}

	_wireDoctypePicker() {
		const df = {
			fieldname: "base_doctype",
			fieldtype: "Link",
			options: "DocType",
			label: __("Select DocType"),
			placeholder: __("Type to search a DocType"),
			get_query: () => ({
				query: "report_builder.api.metadata.search_doctypes",
			}),
			change: () => this._onBaseDoctypeChanged(),
		};
		this.doctypePickerCtrl = frappe.ui.form.make_control({
			df,
			parent: this.$sourcePicker[0],
			render_input: true,
		});
	}

	async _onBaseDoctypeChanged() {
		const dt = this.doctypePickerCtrl.get_value();
		if (!dt) return;
		if (this.state.baseDoctype && this.state.baseDoctype !== dt) {
			const ok = await this._confirm(
				__("Switching DocType will clear current Columns / Filters / Group / Sort. Continue?")
			);
			if (!ok) {
				this.doctypePickerCtrl.set_value(this.state.baseDoctype);
				return;
			}
			this.state.columns = [];
			this.state.filters = [];
			this.state.groupBy = [];
			this._renderAllBuckets();
			this._clearPreview();
		}
		this.state.baseDoctype = dt;
		this.$sourceStatus.text(__("Loading fields…"));
		try {
			const fields = await RB_API.getFields(dt);
			this._setFields(fields);
			this.$sourceStatus.text("");
		} catch (e) {
			this.$sourceStatus.text(__("Could not load fields."));
			console.error(e);
		}
	}

	_setFields(fields) {
		this.fieldsByPath.clear();
		this.sourceFields = { "": fields };
		this._indexFields("", fields);
		this._renderPalette();
	}

	_indexFields(source, fields) {
		fields.forEach((f) => {
			this.fieldsByPath.set(this._fieldKey(source, f.path), {
				source,
				path: f.path,
				label: f.label,
				fieldtype: f.fieldtype,
				options: f.options,
			});
			if (f.children) this._indexFields(source, f.children);
		});
	}

	async _loadFieldsForSource(source, doctype) {
		const fields = await RB_API.getFields(doctype);
		this.sourceFields = this.sourceFields || {};
		this.sourceFields[source] = fields;
		this._indexFields(source, fields);
		this._renderPalette();
	}

	_dropSource(source) {
		if (!source) return;
		if (this.sourceFields) delete this.sourceFields[source];
		const drop = [];
		this.fieldsByPath.forEach((_, key) => {
			if (key.startsWith(`${source}|`)) drop.push(key);
		});
		drop.forEach((k) => this.fieldsByPath.delete(k));
	}

	_renderPalette() {
		this.$palette.empty();
		const baseFields = (this.sourceFields && this.sourceFields[""]) || [];
		if (!baseFields.length) {
			this.$palette.html(`<div class="text-muted small">${__("No fields available.")}</div>`);
			return;
		}

		// Child-table joins (is_child_table=true related sources) are nested
		// under their parent's Table field instead of getting their own
		// top-level palette section. Build a parent_alias -> {parent_field:
		// child_alias} map, then inject the child fields when rendering.
		const childInjectionMap = {};
		(this.state.relatedSources || []).forEach((rs) => {
			if (!rs.is_child_table) return;
			const parentAlias = (rs.conditions || [])[0]?.left_source ?? "";
			const parentField = rs.child_parent_field || "";
			if (!parentField) return;
			childInjectionMap[parentAlias] = childInjectionMap[parentAlias] || {};
			childInjectionMap[parentAlias][parentField] = rs.alias;
		});

		const injectChildrenFor = (sourceAlias, fields) => {
			const map = childInjectionMap[sourceAlias] || {};
			return (fields || []).map((f) => {
				const isTable = f.fieldtype === "Table" || f.fieldtype === "Table MultiSelect";
				if (isTable && map[f.fieldname]) {
					const childAlias = map[f.fieldname];
					const childFields = (this.sourceFields && this.sourceFields[childAlias]) || [];
					// Re-tag each child field so dragging it carries the
					// child source alias, not the parent's. requires_child_join
					// is cleared because the explicit join now exists.
					const reTagged = childFields.map((cf) => ({
						...cf,
						__source: childAlias,
						requires_child_join: false,
					}));
					return { ...f, children: reTagged, requires_child_join: false };
				}
				if (f.children) {
					return { ...f, children: injectChildrenFor(sourceAlias, f.children) };
				}
				return f;
			});
		};

		const sections = [];
		sections.push(`
			<div class="rs-palette-section" data-source="">
				<div class="rs-palette-section-title">${frappe.utils.escape_html(this.state.baseDoctype || "Base")}</div>
				<div class="rs-palette-tree">${this._buildPaletteHtml(injectChildrenFor("", baseFields), 0, "")}</div>
			</div>
		`);
		(this.state.relatedSources || []).forEach((rs) => {
			// A child-table join is not its own top-level palette section —
			// its fields are nested under the parent Table field above.
			if (rs.is_child_table) return;
			const fields = injectChildrenFor(rs.alias, (this.sourceFields && this.sourceFields[rs.alias]) || []);
			sections.push(`
				<div class="rs-palette-section" data-source="${frappe.utils.escape_html(rs.alias)}">
					<div class="rs-palette-section-title">
						<span class="rs-source-badge">${frappe.utils.escape_html(rs.alias)}</span>
						${frappe.utils.escape_html(rs.related_doctype)}
					</div>
					<div class="rs-palette-tree">${this._buildPaletteHtml(fields, 0, rs.alias)}</div>
				</div>
			`);
		});
		// Calculations as draggable chips.
		if (this.state.calculations && this.state.calculations.length) {
			const calcs = this.state.calculations
				.map(
					(c) => `
					<div class="rs-field-leaf rs-calc-leaf"
						data-calc="${frappe.utils.escape_html(c.alias)}"
						draggable="true">
						<span class="rs-field-label">${frappe.utils.escape_html(c.label || c.alias)}</span>
						<span class="rs-field-meta">Σ</span>
					</div>
				`
				)
				.join("");
			sections.push(`
				<div class="rs-palette-section" data-source="__calc">
					<div class="rs-palette-section-title">${__("Calculations")}</div>
					<div class="rs-palette-tree">${calcs}</div>
				</div>
			`);
		}
		this.$palette.html(sections.join(""));
		this._initPaletteSortable();
		this._wirePaletteToggles();
		if (this.$paletteSearch && this.$paletteSearch.val()) {
			this._applyPaletteFilter(this.$paletteSearch.val());
		}
	}

	_buildPaletteHtml(fields, depth, source = "") {
		// Render a FLAT list of leaves under a Sortable container. Link parents
		// render with a caret sibling; their children render as flat siblings
		// (also `.rs-field-leaf`) tagged with `data-group` and hidden by default.
		// The toggle simply shows/hides leaves matching the group id.
		const out = [];
		const escapeHtml = frappe.utils.escape_html;
		const dataAttrsFor = (f) => {
			// f.__source overrides the section source for fields injected from
			// a nested child-table join — the leaf must drag with the child
			// alias so the engine resolves it against the right related source.
			const effectiveSource = f.__source !== undefined ? f.__source : source;
			let attrs = `data-source="${escapeHtml(effectiveSource)}" data-path="${escapeHtml(f.path)}" data-fieldtype="${escapeHtml(f.fieldtype)}" data-options="${escapeHtml(f.options || "")}" data-label="${escapeHtml(f.label)}"`;
			if (f.requires_child_join) {
				attrs += ` data-requires-child-join="1" data-child-parent-field="${escapeHtml(f.child_parent_field || "")}" data-child-doctype="${escapeHtml(f.child_doctype || "")}"`;
			}
			if (f.is_child_table) attrs += ` data-is-child-table="1"`;
			return attrs;
		};
		const labelHtmlFor = (f) => {
			return `<span class="rs-field-label">${escapeHtml(f.label)}</span> <span class="rs-field-meta">${escapeHtml(f.fieldtype)}</span>`;
		};

		fields.forEach((f) => {
			const hasChildren = f.children && f.children.length;
			const isTable = f.fieldtype === "Table" || f.fieldtype === "Table MultiSelect";
			if (hasChildren) {
				const gid = `g_${(source || "base")}__${f.path.replace(/[^A-Za-z0-9_]/g, "_")}`;
				// Table parents are NOT draggable on their own — there is no
				// SQL column for the table itself; the user has to pick a
				// child field. We mark the header with `rs-no-drag` and the
				// palette Sortable's `filter` excludes it.
				const headerClasses = isTable
					? "rs-field-leaf rs-field-group-header rs-no-drag"
					: "rs-field-leaf rs-field-group-header";
				// Link parents must be a DIRECT child of .rs-palette-tree so
				// SortableJS treats them as a drag candidate the same way it
				// does plain leaves and link children. Wrapping them in an
				// extra .rs-field-row made the leaf a grandchild of the
				// sortable container, and Sortable's drag-start was failing
				// silently — that's why the caret expanded but no drag began.
				// Caret now lives inside the leaf; data-toggle only on caret
				// so a click on the label area never beats drag-start.
				out.push(`
					<div class="${headerClasses}" ${dataAttrsFor(f)}>
						<span class="rs-caret" data-toggle="${gid}" role="button" tabindex="0">▶</span>
						${labelHtmlFor(f)}
					</div>
				`);
				f.children.forEach((c) => {
					// 40px lines up with the parent label start (9px leaf
					// padding + 24px caret + ~8px flex gap).
					out.push(`
						<div class="rs-field-leaf rs-link-child" data-group="${gid}" ${dataAttrsFor(c)} style="display:none; padding-left:40px">
							${labelHtmlFor(c)}
						</div>
					`);
				});
			} else {
				out.push(`
					<div class="rs-field-leaf" ${dataAttrsFor(f)}>
						${labelHtmlFor(f)}
					</div>
				`);
			}
		});
		return out.join("");
	}

	_wirePaletteToggles() {
		// Click handler — toggles children with matching data-group. Bound
		// on the caret only; the header leaf is purely draggable so clicking
		// it never expands the row (otherwise click-to-expand wins the race
		// against Sortable's drag-start and the Link parent feels un-draggable).
		this.$palette.off("click.rs");
		this.$palette.on("click.rs", "[data-toggle]", (e) => {
			const groupId = e.currentTarget.dataset.toggle;
			if (!groupId) return;
			e.preventDefault();
			e.stopPropagation();
			const $caret = this.$palette.find(`.rs-caret[data-toggle="${groupId}"]`);
			const $children = this.$palette.find(`.rs-link-child[data-group="${groupId}"]`);
			const open = $children.first().is(":visible");
			$children.toggle(!open);
			$caret.text(open ? "▶" : "▼");
		});

		// Capture-phase mousedown — Sortable's listeners are on the tree (a
		// parent of the caret); to stop the caret from starting a drag we have
		// to intercept the event during the capture phase, BEFORE it bubbles
		// up to Sortable.
		if (this._capturePaletteMousedown) {
			this.$palette[0].removeEventListener(
				"mousedown",
				this._capturePaletteMousedown,
				true
			);
		}
		this._capturePaletteMousedown = (e) => {
			if (e.target.closest && e.target.closest(".rs-caret")) {
				e.stopPropagation();
				e.preventDefault();
			}
		};
		this.$palette[0].addEventListener(
			"mousedown",
			this._capturePaletteMousedown,
			true
		);
	}

	_applyPaletteFilter(rawQuery) {
		const q = (rawQuery || "").toLowerCase().trim();
		const $palette = this.$palette;

		if (!q) {
			// Reset: every leaf and section visible; Link children hidden until
			// the user expands them via the caret.
			$palette
				.find(".rs-field-leaf, .rs-palette-section")
				.css("display", "");
			$palette.find(".rs-link-child").hide();
			$palette.find(".rs-caret").text("▶");
			return;
		}

		// Clear any section/leaf hiding from a previous pass before re-evaluating.
		// jQuery's :visible cascades through ancestors, so leaves inside a section
		// hidden last pass would still report :hidden even after toggle(true) —
		// the downstream group/section checks then re-hide the section and the
		// new matches never appear. Resetting display here makes the per-leaf
		// toggle the single source of truth for this pass.
		$palette.find(".rs-palette-section").css("display", "");
		$palette.find(".rs-field-group-header").css("display", "");

		// Strict start-of-fieldname match: a leaf is shown only if the field's
		// own name (last segment of the dotted path) starts with the query.
		// Typing "cust" hits "customer", "customer_name", "customer_group" but
		// "name" does NOT hit "customer_name".
		$palette.find(".rs-field-leaf").each(function () {
			const path = (this.dataset.path || "").toLowerCase();
			const fieldname = path ? path.split(".").pop() : "";
			const label = (this.dataset.label || "").toLowerCase();
			const match = (fieldname && fieldname.startsWith(q)) || (label && label.startsWith(q));
			$(this).toggle(match);
		});

		// For each Link parent header: keep the header visible if any of its
		// children match the query, even when the parent label itself didn't.
		// Also auto-expand children so matches are visible without a click.
		$palette.find(".rs-field-group-header").each((_, headerEl) => {
			const $header = $(headerEl);
			const $caret = $header.find(".rs-caret");
			const groupId = $caret.attr("data-toggle");
			const $children = groupId ? $palette.find(`.rs-link-child[data-group="${groupId}"]`) : $();
			const anyChildVisible = $children.toArray().some((el) => el.style.display !== "none");
			if (anyChildVisible) {
				$header.show();
				$caret.text("▼");
			} else {
				$children.hide();
				$caret.text("▶");
			}
		});

		// Hide an entire section if nothing inside is visible. Use the leaf's
		// own `style.display` rather than :visible so a section we just reset
		// at the top of this pass is judged by its leaves' own state, not by
		// the prior pass's section-level hiding.
		$palette.find(".rs-palette-section").each(function () {
			const $s = $(this);
			const hasVisibleLeaf = $s
				.find(".rs-field-leaf")
				.toArray()
				.some((el) => el.style.display !== "none");
			$s.toggle(hasVisibleLeaf);
		});
	}

	_initPaletteSortable() {
		(this._paletteSortables || []).forEach((s) => {
			try { s.destroy(); } catch (_) {}
		});
		this._paletteSortables = [];
		// Flat tree — one Sortable per .rs-palette-tree section. Children of
		// Link parents are siblings of the parent leaf, not nested.
		this.$palette[0].querySelectorAll(".rs-palette-tree").forEach((el) => {
			const s = window.Sortable.create(el, {
				group: { name: "rs-fields", pull: "clone", put: false },
				sort: false,
				draggable: ".rs-field-leaf",
				// Table parents have no SQL column to map onto, so dragging
				// them produces an invalid query. Mark them with rs-no-drag
				// (in _renderPalette) and skip them in the Sortable filter.
				filter: ".rs-no-drag",
				preventOnFilter: false,
				animation: 120,
				fallbackOnBody: true,
				forceFallback: true,
				fallbackTolerance: 4,
				onStart: () => {
					document.body.classList.add("rs-dragging");
					startGhostFollow();
				},
				onEnd: () => {
					document.body.classList.remove("rs-dragging");
					stopGhostFollow();
				},
			});
			this._paletteSortables.push(s);
		});
	}

	_wireBuckets() {
		const handlers = {
			columns: (data) => this._addColumn(data),
			filters: (data) => this._addFilter(data),
			groupBy: (data) => this._addGroupBy(data),
		};

		Object.entries(this.$buckets).forEach(([key, $bucket]) => {
			window.Sortable.create($bucket[0], {
				// pull: "clone" so dragging a bucket row to another bucket
				// COPIES the field. The original stays put; the dropped clone
				// is replaced when the target bucket re-renders from state.
				group: { name: "rs-fields", pull: "clone", put: true },
				draggable: ".rs-bucket-row",
				// Drag only by the label area — inputs/selects inside the row
				// remain interactive (no accidental drag while editing).
				handle: ".rs-row-label",
				animation: 120,
				forceFallback: true,
				fallbackTolerance: 4,
				// Append the dragged ghost to <body> so its position doesn't
				// inherit any transform/scroll context from the bucket grid
				// (otherwise the ghost can drift away from the cursor).
				fallbackOnBody: true,
				onStart: () => {
					document.body.classList.add("rs-dragging");
					startGhostFollow();
				},
				onEnd: () => {
					document.body.classList.remove("rs-dragging");
					stopGhostFollow();
				},
				onAdd: (evt) => {
					const item = evt.item;
					const fromSameBucket = evt.from === evt.to;
					const calcAlias = item.dataset.calc;
					const data = {
						source: item.dataset.source || "",
						path: item.dataset.path,
						fieldtype: item.dataset.fieldtype,
						options: item.dataset.options || null,
						label: item.dataset.label,
						calc: calcAlias || null,
					};
					$(item).remove();
					if (fromSameBucket) {
						// Drop the duplicate clone left behind by Sortable.
						// (In-bucket reorder isn't tracked in state yet.)
						this._renderBucket(key);
						return;
					}
					if (data.calc) {
						if (key !== "columns") {
							frappe.show_alert({
								message: __("Calculations can only be in Columns."),
								indicator: "orange",
							});
							return;
						}
						this._addCalcColumn(data.calc);
						return;
					}
					if (!data.path) return;
					if (data.fieldtype === "Table" || data.fieldtype === "Table MultiSelect") {
						frappe.show_alert({
							message: __("Pick a field inside the table, not the table itself."),
							indicator: "orange",
						});
						return;
					}
					handlers[key](data);
				},
			});
		});
	}

	_addColumn(data) {
		const source = data.source || "";
		this.state.columns.push({
			source,
			field_path: data.path,
			label: data.label,
			fieldtype: data.fieldtype,
			aggregate: "",
			calculation_alias: null,
		});
		this._renderBucket("columns");
	}

	_addCalcColumn(alias) {
		const calc = (this.state.calculations || []).find((c) => c.alias === alias);
		if (!calc) return;
		this.state.columns.push({
			source: "",
			field_path: null,
			calculation_alias: alias,
			label: calc.label || alias,
			fieldtype: "Float",
			aggregate: "",
		});
		this._renderBucket("columns");
	}

	_addFilter(data) {
		const ops = operatorsFor(data.fieldtype);
		this.state.filters.push({
			source: data.source || "",
			field_path: data.path,
			fieldtype: data.fieldtype,
			operator: ops[0],
			value: "",
			value_to: "",
			value_list: "",
			granularity: "",
			is_runtime: false,
			_meta: { label: data.label, options: data.options },
		});
		this._renderBucket("filters");
	}

	_addGroupBy(data) {
		const source = data.source || "";
		this.state.groupBy.push({
			source,
			field_path: data.path,
			fieldtype: data.fieldtype,
			granularity: DATE_FIELDTYPES.has(data.fieldtype) || DATETIME_FIELDTYPES.has(data.fieldtype) ? "Day" : "",
			_meta: { label: data.label },
		});
		// Reconcile column aggregates with the new group state. Group key
		// columns must have an empty aggregate; non-key columns default to
		// no aggregate (show the actual value) — the user can opt into
		// Count/Sum/etc explicitly via the Select.
		this.state.columns.forEach((col) => {
			if (col.calculation_alias) return;
			const isKey = this.state.groupBy.some(
				(g) => (g.source || "") === (col.source || "") && g.field_path === col.field_path
			);
			if (isKey) col.aggregate = "";
		});
		this._renderBucket("groupBy");
		this._renderBucket("columns");
	}

	_renderAllBuckets() {
		["columns", "filters", "groupBy"].forEach((b) => this._renderBucket(b));
	}

	_renderBucket(bucket) {
		const $b = this.$buckets[bucket];
		$b.empty();
		const items = this.state[bucket];
		if (!items.length) {
			$b.append(`<div class="rs-bucket-empty text-muted small">${__("Drop fields here.")}</div>`);
			return;
		}
		const esc = frappe.utils.escape_html;
		items.forEach((item, idx) => {
			// Tag the bucket row with the same data-* attributes the palette
			// leaves use, so dragging this row to another bucket lets that
			// bucket's onAdd handler reconstruct the field info.
			let dataAttrs;
			if (item.calculation_alias) {
				dataAttrs = `data-calc="${esc(item.calculation_alias)}" data-label="${esc(item.label || "")}"`;
			} else {
				dataAttrs =
					`data-source="${esc(item.source || "")}"` +
					` data-path="${esc(item.field_path || "")}"` +
					` data-fieldtype="${esc(item.fieldtype || "")}"` +
					` data-label="${esc(item.label || "")}"`;
			}
			const $row = $(`<div class="rs-bucket-row" data-idx="${idx}" ${dataAttrs}></div>`);
			$b.append($row);
			if (bucket === "columns") this._renderColumnRow($row, item, idx);
			else if (bucket === "filters") this._renderFilterRow($row, item, idx);
			else if (bucket === "groupBy") this._renderGroupRow($row, item, idx);
		});
	}

	_renderColumnRow($row, item, idx) {
		if (item.calculation_alias) {
			return this._renderCalcColumnRow($row, item, idx);
		}
		const source = item.source || "";
		const meta = this.fieldsByPath.get(this._fieldKey(source, item.field_path)) || { label: item.label || item.field_path };
		const isGroupKey = this.state.groupBy.some(
			(g) => (g.source || "") === source && g.field_path === item.field_path
		);
		const showAggregate = this.state.groupBy.length > 0 && !isGroupKey;
		const sourceBadge = source
			? `<span class="rs-row-badge rs-badge-source">${frappe.utils.escape_html(source)}</span>`
			: "";
		const displayLabel = item.label || meta.label || item.field_path;
		const labelHtml = `${sourceBadge}<span class="rs-row-label-text">${frappe.utils.escape_html(displayLabel)}</span>`;
		const badge = isGroupKey
			? `<span class="rs-row-badge rs-badge-group">${__("Group Key")}</span>`
			: "";
		const ruleBadge = item.visibility_rule
			? `<span class="rs-row-badge rs-badge-rule" title="${__("Has visibility rule")}">👁</span>`
			: "";
		const fmtBadge = (item.format_rules && item.format_rules.length)
			? `<span class="rs-row-badge rs-badge-rule" title="${__("Has format rules")}">🎨</span>`
			: "";
		$row.html(`
			<div class="rs-row-main">
				<div class="rs-row-label">
					${labelHtml} ${badge} ${ruleBadge} ${fmtBadge}
					<button class="rs-row-icon rs-row-rename" title="${__("Rename")}">✎</button>
					<button class="rs-row-icon rs-row-visibility" title="${__("Show only when…")}">👁</button>
					<button class="rs-row-icon rs-row-format" title="${__("Conditional formatting")}">🎨</button>
				</div>
				<div class="rs-row-aggregate"></div>
				<button class="btn btn-default btn-xs rs-row-remove" title="${__("Remove")}">&times;</button>
			</div>
		`);
		$row.find(".rs-row-rename").on("click", (e) => {
			e.stopPropagation();
			this._promptRenameColumn(item, displayLabel, () => this._renderBucket("columns"));
		});
		$row.find(".rs-row-visibility").on("click", (e) => {
			e.stopPropagation();
			this._visibilityDialog(item, () => this._renderBucket("columns"));
		});
		$row.find(".rs-row-format").on("click", (e) => {
			e.stopPropagation();
			this._formatRulesDialog(item, () => this._renderBucket("columns"));
		});
		const $agg = $row.find(".rs-row-aggregate");
		if (showAggregate) {
			const ctrl = makeControl({
				fieldtype: "Select",
				options: ["", "Count", "Sum", "Avg", "Min", "Max"].join("\n"),
				value: item.aggregate || "",
				parent: $agg[0],
				label: __("Summarize as"),
			});
			ctrl.$input?.on("change", () => {
				item.aggregate = ctrl.get_value() || "";
			});
		} else {
			item.aggregate = "";
			$agg.empty();
		}
		$row.find(".rs-row-remove").on("click", () => {
			this.state.columns.splice(idx, 1);
			this._renderBucket("columns");
		});
	}

	_renderCalcColumnRow($row, item, idx) {
		const calc = (this.state.calculations || []).find((c) => c.alias === item.calculation_alias);
		const label = item.label || (calc && (calc.label || calc.alias)) || item.calculation_alias;
		const ruleBadge = item.visibility_rule
			? `<span class="rs-row-badge rs-badge-rule" title="${__("Has visibility rule")}">👁</span>`
			: "";
		const fmtBadge = (item.format_rules && item.format_rules.length)
			? `<span class="rs-row-badge rs-badge-rule" title="${__("Has format rules")}">🎨</span>`
			: "";
		$row.html(`
			<div class="rs-row-main">
				<div class="rs-row-label">
					<span class="rs-row-badge rs-badge-calc">${__("Calc")}</span>
					<span class="rs-row-label-text">${frappe.utils.escape_html(label)}</span>
					${ruleBadge} ${fmtBadge}
					<button class="rs-row-icon rs-row-rename" title="${__("Rename")}">✎</button>
					<button class="rs-row-icon rs-row-visibility" title="${__("Show only when…")}">👁</button>
					<button class="rs-row-icon rs-row-format" title="${__("Conditional formatting")}">🎨</button>
				</div>
				<div class="rs-row-aggregate text-muted small">${calc ? frappe.utils.escape_html(this._formulaPreview(calc)) : ""}</div>
				<button class="btn btn-default btn-xs rs-row-remove" title="${__("Remove")}">&times;</button>
			</div>
		`);
		$row.find(".rs-row-rename").on("click", (e) => {
			e.stopPropagation();
			this._promptRenameColumn(item, label, () => this._renderBucket("columns"));
		});
		$row.find(".rs-row-visibility").on("click", (e) => {
			e.stopPropagation();
			this._visibilityDialog(item, () => this._renderBucket("columns"));
		});
		$row.find(".rs-row-format").on("click", (e) => {
			e.stopPropagation();
			this._formatRulesDialog(item, () => this._renderBucket("columns"));
		});
		$row.find(".rs-row-remove").on("click", () => {
			this.state.columns.splice(idx, 1);
			this._renderBucket("columns");
		});
	}

	_promptRenameColumn(item, current, after) {
		frappe.prompt(
			[
				{
					fieldtype: "Data",
					fieldname: "label",
					label: __("Display Label"),
					default: current,
					reqd: 0,
				},
			],
			({ label }) => {
				const v = (label || "").trim();
				item.label = v || null;
				if (after) after();
			},
			__("Rename Column"),
			__("Save")
		);
	}

	_formulaPreview(calc) {
		const op = (calc.expression && calc.expression.op) || "?";
		const left = this._operandPreview(calc.expression && calc.expression.left);
		const right = this._operandPreview(calc.expression && calc.expression.right);
		return `${left} ${op} ${right}`;
	}

	_operandPreview(operand) {
		if (!operand) return "?";
		if (operand.type === "const") return String(operand.value);
		const src = operand.source || "";
		const path = operand.path || "?";
		return src ? `${src}.${path}` : path;
	}

	_renderFilterRow($row, item, idx) {
		const fieldtype = item.fieldtype || "Data";
		const source = item.source || "";
		const meta = this.fieldsByPath.get(this._fieldKey(source, item.field_path)) || {};
		const baseLabel = meta.label || item._meta?.label || item.field_path;
		const sourceBadge = source
			? `<span class="rs-row-badge rs-badge-source">${frappe.utils.escape_html(source)}</span>`
			: "";
		const label = `${sourceBadge}${frappe.utils.escape_html(baseLabel)}`;
		const ops = operatorsFor(fieldtype);
		if (!ops.includes(item.operator)) item.operator = ops[0];

		const isDateLike = DATE_FIELDTYPES.has(fieldtype) || DATETIME_FIELDTYPES.has(fieldtype);
		const granularityCell = isDateLike ? `<div class="rs-filter-gran"></div>` : "";
		const runtimeChecked = item.is_runtime ? "checked" : "";
		$row.html(`
			<div class="rs-row-main rs-filter-row">
				<div class="rs-row-label" title="${frappe.utils.escape_html(baseLabel)}">${label}</div>
				${granularityCell}
				<div class="rs-filter-op"></div>
				<div class="rs-filter-value rs-filter-value-1"></div>
				<div class="rs-filter-value rs-filter-value-2"></div>
				<label class="rs-filter-runtime" title="${__("If checked, this filter shows up in the runtime filter bar of the published report. The value below is just the default.")}">
					<input type="checkbox" class="rs-filter-runtime-input" ${runtimeChecked}/>
					<span>${__("Ask")}</span>
				</label>
				<button class="btn btn-default btn-xs rs-row-remove" title="${__("Remove")}">&times;</button>
			</div>
		`);
		$row.find(".rs-filter-runtime-input").on("change", function () {
			item.is_runtime = this.checked;
		});

		if (isDateLike) {
			const $gran = $row.find(".rs-filter-gran");
			// "All" lets one filter row accept any of YYYY, YYYY-MM, or
			// YYYY-MM-DD. The engine detects the format from what the user
			// types and applies the matching SQL bucket.
			const granCtrl = makeControl({
				fieldtype: "Select",
				options: ["", "All", "Date", "Month", "Year"].join("\n"),
				value: item.granularity || "",
				parent: $gran[0],
				label: __("Granularity"),
			});
			granCtrl.$input?.on("change", () => {
				item.granularity = granCtrl.get_value() || "";
				// Re-render so the value cell switches (date picker ↔ year/month input).
				this._renderFilterRow($row, item, idx);
			});
		}

		const $op = $row.find(".rs-filter-op");
		const opCtrl = makeControl({
			fieldtype: "Select",
			options: ops.join("\n"),
			value: item.operator,
			parent: $op[0],
			label: __("Condition"),
		});
		opCtrl.$input?.on("change", () => {
			item.operator = opCtrl.get_value();
			this._renderFilterRow($row, item, idx);
		});

		this._renderFilterValue($row, item, fieldtype, meta);

		$row.find(".rs-row-remove").on("click", () => {
			this.state.filters.splice(idx, 1);
			this._renderBucket("filters");
		});
	}

	_renderFilterValue($row, item, fieldtype, meta) {
		const $v1 = $row.find(".rs-filter-value-1").empty();
		const $v2 = $row.find(".rs-filter-value-2").empty();
		const op = item.operator;

		if (op === "Is Set" || op === "Is Not Set") {
			$v1.append(`<span class="text-muted small">${__("(no value)")}</span>`);
			return;
		}
		if (op === "In" || op === "Not In") {
			const ctrl = makeControl({
				fieldtype: "Small Text",
				value: item.value_list || "",
				parent: $v1[0],
				placeholder: __("Comma-separated values"),
				label: __("Values"),
			});
			ctrl.$input?.on("change", () => { item.value_list = ctrl.get_value() || ""; });
			ctrl.$wrapper?.find("input,textarea").on("change", () => { item.value_list = ctrl.get_value() || ""; });
			return;
		}

		// Date-bucket aware value rendering: when granularity is Year/Month
		// the underlying SQL compares against an int / "YYYY-MM" string;
		// "All" accepts any of YYYY / YYYY-MM / YYYY-MM-DD as free text.
		// Give the user a plain text input rather than a date picker for
		// any of those modes.
		const gran = item.granularity || "";
		const isDateLike = DATE_FIELDTYPES.has(fieldtype) || DATETIME_FIELDTYPES.has(fieldtype);
		const useBucketInput = isDateLike && (gran === "Year" || gran === "Month" || gran === "All");

		if (useBucketInput) {
			this._mountBucketInput($v1[0], gran, item.value, (v) => { item.value = v; });
			if (op === "Between") {
				this._mountBucketInput($v2[0], gran, item.value_to, (v) => { item.value_to = v; });
			}
			return;
		}

		const valueCtrl = this._mountValueControl($v1[0], fieldtype, meta, item.value);
		valueCtrl?.$input?.on("change", () => { item.value = valueCtrl.get_value() ?? ""; });
		valueCtrl?.$wrapper?.find("input,select,textarea").on("change", () => {
			item.value = valueCtrl.get_value() ?? "";
		});

		if (op === "Between") {
			const v2 = this._mountValueControl($v2[0], fieldtype, meta, item.value_to);
			v2?.$input?.on("change", () => { item.value_to = v2.get_value() ?? ""; });
			v2?.$wrapper?.find("input,select,textarea").on("change", () => {
				item.value_to = v2.get_value() ?? "";
			});
		}
	}

	_mountBucketInput(host, gran, value, onChange) {
		// Plain HTML inputs for bucketed dates. Frappe's Date control would
		// force a full date pick which doesn't match the bucketed comparison.
		//   Year  → number input, "2024"
		//   Month → text input, "2024-03"
		//   All   → text input, accepts YYYY / YYYY-MM / YYYY-MM-DD
		const isYear = gran === "Year";
		let placeholder = "2024-03";
		let hint = "";
		if (isYear) {
			placeholder = "2024";
		} else if (gran === "All") {
			placeholder = "2024-03-30";
			hint = __("YYYY  ·  YYYY-MM  ·  YYYY-MM-DD");
		}
		const $wrap = $(`
			<div class="rs-bucket-input-wrap">
				<input
					type="${isYear ? "number" : "text"}"
					class="form-control input-xs rs-bucket-input"
					placeholder="${placeholder}"
					${isYear ? 'min="1900" max="2999"' : ''}
					value="${frappe.utils.escape_html(value || "")}"
				/>
				${hint ? `<div class="rs-bucket-hint">${frappe.utils.escape_html(hint)}</div>` : ""}
			</div>
		`);
		const $in = $wrap.find("input");
		$(host).empty().append($wrap);
		$in.on("input change", () => onChange($in.val() || ""));
	}

	_mountValueControl(parent, fieldtype, meta, currentValue) {
		const baseDf = {
			fieldname: "v",
			fieldtype: "Data",
			label: "",
		};

		if (DATE_FIELDTYPES.has(fieldtype)) baseDf.fieldtype = "Date";
		else if (DATETIME_FIELDTYPES.has(fieldtype)) baseDf.fieldtype = "Datetime";
		else if (TIME_FIELDTYPES.has(fieldtype)) baseDf.fieldtype = "Time";
		else if (NUMERIC_FIELDTYPES.has(fieldtype)) baseDf.fieldtype = "Float";
		else if (LINK_FIELDTYPES.has(fieldtype)) {
			baseDf.fieldtype = "Link";
			baseDf.options = meta.options || "";
		} else if (SELECT_FIELDTYPES.has(fieldtype)) {
			baseDf.fieldtype = "Select";
			baseDf.options = meta.options || "";
		} else if (fieldtype === "Check") {
			baseDf.fieldtype = "Check";
		} else {
			baseDf.fieldtype = "Data";
		}

		const ctrl = frappe.ui.form.make_control({
			df: baseDf,
			parent,
			render_input: true,
			only_input: true,
		});
		if (currentValue !== undefined && currentValue !== null && currentValue !== "") {
			try { ctrl.set_value(currentValue); } catch (_) {}
		}
		return ctrl;
	}

	_renderGroupRow($row, item, idx) {
		const source = item.source || "";
		const meta = this.fieldsByPath.get(this._fieldKey(source, item.field_path)) || {};
		const label = meta.label || item._meta?.label || item.field_path;
		const isDate = DATE_FIELDTYPES.has(item.fieldtype) || DATETIME_FIELDTYPES.has(item.fieldtype);
		const sourceBadge = source
			? `<span class="rs-row-badge rs-badge-source">${frappe.utils.escape_html(source)}</span>`
			: "";
		$row.html(`
			<div class="rs-row-main">
				<div class="rs-row-label">${sourceBadge}${frappe.utils.escape_html(label)}</div>
				<div class="rs-group-granularity"></div>
				<button class="btn btn-default btn-xs rs-row-remove" title="${__("Remove")}">&times;</button>
			</div>
		`);
		if (isDate) {
			const ctrl = makeControl({
				fieldtype: "Select",
				options: ["", "Day", "Week", "Month", "Quarter", "Year"].join("\n"),
				value: item.granularity || "",
				parent: $row.find(".rs-group-granularity")[0],
				label: __("Granularity"),
			});
			ctrl.$input?.on("change", () => { item.granularity = ctrl.get_value() || ""; });
		} else {
			item.granularity = "";
		}
		$row.find(".rs-row-remove").on("click", () => {
			this.state.groupBy.splice(idx, 1);
			if (this.state.groupBy.length === 0) {
				this.state.columns.forEach((c) => { c.aggregate = ""; });
			}
			this._renderBucket("groupBy");
			this._renderBucket("columns");
		});
	}

	_renderRelatedList() {
		this.$relatedList.empty();
		// Child-table joins are materialized as their own related sources so
		// the engine can wire them up, but they shouldn't appear as standalone
		// rows here — they belong to whichever parent ticked them.
		const visibleRows = this.state.relatedSources
			.map((rs, idx) => ({ rs, idx }))
			.filter(({ rs }) => !rs.is_child_table);
		if (!visibleRows.length) {
			this.$relatedList.append(`<div class="rs-card-empty text-muted small">${__("None yet.")}</div>`);
			return;
		}
		visibleRows.forEach(({ rs, idx }) => {
			const condCount = (rs.conditions || []).length;
			const $row = $(`
				<div class="rs-related-row">
					<div class="rs-related-info">
						<span class="rs-source-badge">${frappe.utils.escape_html(rs.alias)}</span>
						<strong>${frappe.utils.escape_html(rs.related_doctype)}</strong>
						<span class="text-muted small">${frappe.utils.escape_html(rs.join_type || "Left Join")}, ${condCount} ${__("match condition(s)")}</span>
					</div>
					<div class="rs-related-actions">
						<button class="btn btn-default btn-xs rs-related-edit">${__("Edit")}</button>
						<button class="btn btn-default btn-xs rs-related-remove">${__("Remove")}</button>
					</div>
				</div>
			`);
			$row.find(".rs-related-edit").on("click", () => this._addRelatedDialog(idx));
			$row.find(".rs-related-remove").on("click", () => this._removeRelatedSource(idx));
			this.$relatedList.append($row);
		});
	}

	_renderCalcList() {
		this.$calcList.empty();
		if (!this.state.calculations.length) {
			this.$calcList.append(`<div class="rs-card-empty text-muted small">${__("None yet.")}</div>`);
			return;
		}
		this.state.calculations.forEach((calc, idx) => {
			const $row = $(`
				<div class="rs-calc-row">
					<div class="rs-calc-info">
						<strong>${frappe.utils.escape_html(calc.label || calc.alias)}</strong>
						<span class="text-muted small">${frappe.utils.escape_html(this._formulaPreview(calc))}</span>
					</div>
					<div class="rs-calc-actions">
						<button class="btn btn-default btn-xs rs-calc-edit">${__("Edit")}</button>
						<button class="btn btn-default btn-xs rs-calc-remove">${__("Remove")}</button>
					</div>
				</div>
			`);
			$row.find(".rs-calc-edit").on("click", () => this._addCalcDialog(idx));
			$row.find(".rs-calc-remove").on("click", () => this._removeCalculation(idx));
			this.$calcList.append($row);
		});
	}

	async _addRelatedDialog(editIdx = null) {
		if (!this.state.baseDoctype) {
			frappe.show_alert({ message: __("Pick a base DocType first."), indicator: "orange" });
			return;
		}
		const editing = editIdx != null ? this.state.relatedSources[editIdx] : null;
		const initial = editing
			? JSON.parse(JSON.stringify(editing))
			: { alias: "", related_doctype: "", join_type: "Left Join", conditions: [] };

		const d = new frappe.ui.Dialog({
			title: editing ? __("Edit Join DocType") : __("Add Join DocType"),
			size: "large",
			fields: [
				{ fieldname: "alias", fieldtype: "Data", label: __("Alias (short name)"), reqd: 1 },
				{
					fieldname: "related_doctype",
					fieldtype: "Link",
					label: __("Join DocType"),
					options: "DocType",
					reqd: 1,
					get_query: () => ({ query: "report_builder.api.metadata.search_doctypes" }),
				},
				{
					fieldname: "join_type",
					fieldtype: "Select",
					label: __("Join Type"),
					options: "Left Join\nInner Join",
					default: "Left Join",
				},
				{ fieldname: "child_section", fieldtype: "Section Break", label: __("Join via Child Tables") },
				{ fieldname: "child_html", fieldtype: "HTML" },
				{ fieldname: "cond_section", fieldtype: "Section Break", label: __("Match Conditions") },
				{ fieldname: "cond_html", fieldtype: "HTML" },
				{ fieldname: "child_cond_section", fieldtype: "Section Break", label: __("Child Match Conditions") },
				{ fieldname: "child_cond_html", fieldtype: "HTML" },
			],
			primary_action_label: editing ? __("Save") : __("Add"),
			primary_action: async (values) => {
				d.disable_primary_action();
				try {
					const conditions = (initial.conditions || []).filter(
						(c) => c.left_path && c.right_path
					);
					if (!conditions.length) {
						frappe.show_alert({ message: __("Add at least one match condition."), indicator: "orange" });
						return;
					}
					if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(values.alias)) {
						frappe.show_alert({ message: __("Alias must be letters/numbers/underscore."), indicator: "orange" });
						return;
					}
					const rs = {
						alias: values.alias.trim(),
						related_doctype: values.related_doctype,
						join_type: values.join_type || "Left Join",
						conditions,
					};
					// Require child-to-child match conditions for every ticked
					// related-side child join. Without them, the cartesian
					// product would explode child rows in unexpected ways, so
					// the user must say how the rows should pair up.
					for (const p of pendingRelatedChildJoins) {
						const cm = (pendingChildMatch[p.alias] || []).filter(
							(c) => c.left_path && c.right_path
						);
						if (!cm.length) {
							frappe.show_alert({
								message: __("Add a child match condition for {0}, or untick it.", [p.alias]),
								indicator: "orange",
							});
							return;
						}
					}
					// Materialize ticked child-table joins as full related-source
					// rows. Base-side rows reference base/other-related and must
					// land BEFORE the main RS; related-side rows reference the
					// just-added main RS and must land AFTER it.
					const buildChildRs = (p, leftSource, extraConditions = []) => ({
						alias: p.alias,
						related_doctype: p.child_doctype,
						join_type: "Left Join",
						is_child_table: true,
						child_parent_field: p.parent_field,
						conditions: [
							{
								left_source: leftSource,
								left_path: "name",
								operator: "=",
								right_path: "parent",
							},
							...extraConditions,
						],
					});
					const baseChildRs = pendingBaseChildJoins.map((p) => buildChildRs(p, p.parent_alias || ""));
					const relatedChildRs = pendingRelatedChildJoins.map((p) =>
						buildChildRs(
							p,
							rs.alias,
							(pendingChildMatch[p.alias] || []).filter((c) => c.left_path && c.right_path),
						)
					);

					if (editing) {
						const oldAlias = editing.alias;
						// Drop the previously-committed child joins for this RS
						// (we pulled them into pending on open); re-inserting from
						// pending below reflects any tick/untick changes the user
						// made in the dialog. Adjust editIdx for any removals
						// that sit before it so the splice lands on the main RS.
						let targetIdx = editIdx;
						if (initialChildAliases.size) {
							for (let i = 0; i < editIdx; i++) {
								if (initialChildAliases.has(this.state.relatedSources[i].alias)) targetIdx--;
							}
							this.state.relatedSources = this.state.relatedSources.filter(
								(r, i) => i === editIdx || !initialChildAliases.has(r.alias)
							);
							const keep = new Set([...baseChildRs, ...relatedChildRs].map((r) => r.alias));
							initialChildAliases.forEach((a) => {
								if (!keep.has(a)) this._dropSource(a);
							});
						}
						// Insert base child joins immediately before the slot we're
						// rewriting, then replace it with the edited RS, then append
						// related child joins right after.
						this.state.relatedSources.splice(targetIdx, 1, ...baseChildRs, rs, ...relatedChildRs);
						if (oldAlias !== rs.alias) {
							const rewrite = (rows) => rows.forEach((r) => { if ((r.source || "") === oldAlias) r.source = rs.alias; });
							rewrite(this.state.columns);
							rewrite(this.state.filters);
							rewrite(this.state.groupBy);
							this._dropSource(oldAlias);
						}
					} else {
						this.state.relatedSources.push(...baseChildRs, rs, ...relatedChildRs);
					}
					d.hide();
					this._cleanupModalArtifacts();
					for (const cfg of [...baseChildRs, rs, ...relatedChildRs]) {
						await this._loadFieldsForSource(cfg.alias, cfg.related_doctype);
					}
					this._renderRelatedList();
					this._renderAllBuckets();
				} catch (e) {
					console.error(e);
					this._cleanupModalArtifacts();
					frappe.show_alert({ message: __("Could not add join DocType."), indicator: "red" });
				} finally {
					d.enable_primary_action();
				}
			},
		});
		d.$wrapper.on("hidden.bs.modal", () => this._cleanupModalArtifacts());

		d.set_value("alias", initial.alias);
		d.set_value("related_doctype", initial.related_doctype);
		d.set_value("join_type", initial.join_type);

		const $cond = d.get_field("cond_html").$wrapper;
		const $childCond = d.get_field("child_cond_html").$wrapper;

		// Child-table joins the user has ticked but not yet saved. Two buckets:
		// "base" tables hang off the base doctype or any other existing related
		// source (committed BEFORE the main RS); "related" tables hang off the
		// related doctype being added in this dialog (committed AFTER it).
		const pendingBaseChildJoins = [];
		const pendingRelatedChildJoins = [];
		// Map of related-child alias -> array of child-to-child match conditions.
		// Each entry: { left_source, left_path, operator, right_path }.
		// `right_path` resolves against the related child's own doctype;
		// `left_source` must be "" (base) or a base-side child alias.
		const pendingChildMatch = {};
		const allPendingChildJoins = () => [...pendingBaseChildJoins, ...pendingRelatedChildJoins];

		// On edit, restore the previously-committed child joins for this RS
		// so the dialog shows ticked checkboxes, populated condition rows,
		// and the same child-match conditions the user set last time.
		// `initialChildAliases` lets renderers de-dup these against the
		// committed iterations, and lets the save path remove the old rows
		// before re-inserting whatever the user ends up keeping.
		const initialChildAliases = new Set();
		if (editing) {
			this.state.relatedSources.forEach((other, idx) => {
				if (idx === editIdx) return;
				if (!other.is_child_table) return;
				if (!other.child_parent_field) return;
				const parentAlias = other.conditions?.[0]?.left_source ?? "";
				if (parentAlias === editing.alias) {
					pendingRelatedChildJoins.push({
						alias: other.alias,
						child_doctype: other.related_doctype,
						parent_alias: "__new__",
						parent_field: other.child_parent_field,
						side: "related",
					});
					initialChildAliases.add(other.alias);
					pendingChildMatch[other.alias] = (other.conditions || []).slice(1).map((c) => ({
						left_source: c.left_source || "",
						left_path: c.left_path || "",
						operator: c.operator || "=",
						right_path: c.right_path || "",
					}));
				} else if (idx < editIdx) {
					pendingBaseChildJoins.push({
						alias: other.alias,
						child_doctype: other.related_doctype,
						parent_alias: parentAlias,
						parent_field: other.child_parent_field,
						side: "base",
					});
					initialChildAliases.add(other.alias);
				}
			});
		}

		const sanitizeAlias = (s) => `c_${(s || "base").replace(/[^A-Za-z0-9_]/g, "_")}`;
		const ensureUniqueAlias = (proposed) => {
			const taken = new Set([
				...this.state.relatedSources.map((r) => r.alias),
				...allPendingChildJoins().map((p) => p.alias),
			]);
			let candidate = proposed;
			let n = 2;
			while (taken.has(candidate)) {
				candidate = `${proposed}_${n}`;
				n++;
			}
			return candidate;
		};

		// Look up fields for a source alias — checks committed sources first,
		// then any pending child-join alias whose doctype's fields are cached.
		const lookupSourceFields = (alias) => {
			const existing = (this.sourceFields && this.sourceFields[alias]) || null;
			if (existing) return this._flattenFields(existing);
			const pending = allPendingChildJoins().find((p) => p.alias === alias);
			if (pending) {
				const cacheKey = `dt::${pending.child_doctype}`;
				if (this._fieldCache && this._fieldCache.has(cacheKey)) {
					return this._flattenFields(this._fieldCache.get(cacheKey));
				}
			}
			return [];
		};

		const renderConds = async () => {
			$cond.empty();
			// Collect ticked/committed child-table fieldnames so we can reveal
			// only the child fields the user has actually opted into.
			const checkedBaseChildFields = new Set();
			this.state.relatedSources.forEach((other, idx) => {
				if (editIdx != null && idx >= editIdx) return;
				if (!other.is_child_table) return;
				if (initialChildAliases.has(other.alias)) return;
				const parentAlias = (other.conditions || [])[0]?.left_source ?? "";
				if (parentAlias !== "") return;
				if (other.child_parent_field) checkedBaseChildFields.add(other.child_parent_field);
			});
			pendingBaseChildJoins.forEach((p) => {
				if ((p.parent_alias || "") !== "") return;
				if (p.parent_field) checkedBaseChildFields.add(p.parent_field);
			});
			const checkedRelatedChildFields = new Set();
			pendingRelatedChildJoins.forEach((p) => {
				if (p.parent_field) checkedRelatedChildFields.add(p.parent_field);
			});
			const filterByCheckedChildren = (fields, allowed) => (fields || []).filter((f) => {
				if (!f.requires_child_join) return true;
				return allowed.has(f.child_parent_field);
			});
			const baseFields = filterByCheckedChildren(
				this._flattenFields(await this._cachedFields("", this.state.baseDoctype)),
				checkedBaseChildFields
			);
			const rightDoctype = d.get_value("related_doctype");
			const rightFields = rightDoctype
				? filterByCheckedChildren(
					this._flattenFields(await this._cachedFields(`__rs:${initial.alias || "new"}`, rightDoctype)),
					checkedRelatedChildFields
				)
				: [];
			const $tbl = $(`<div class="rs-cond-table"></div>`);
			$cond.append($tbl);
			(initial.conditions || []).forEach((cond, ci) => {
				// Left-side source dropdown: only the base doctype plus its
				// child-table joins (committed or pending in this dialog).
				// Other related sources are intentionally excluded — joins go
				// from the base/base-child to the related doctype being added.
				const sourceOptions = [{ label: this.state.baseDoctype || __("Base"), value: "" }];
				this.state.relatedSources.forEach((other, idx) => {
					if (editIdx != null && idx >= editIdx) return;
					if (!other.is_child_table) return;
					if (initialChildAliases.has(other.alias)) return;
					const parentAlias = (other.conditions || [])[0]?.left_source ?? "";
					if (parentAlias !== "") return;
					sourceOptions.push({ label: `${other.alias} (${other.related_doctype}) [child]`, value: other.alias });
				});
				pendingBaseChildJoins.forEach((p) => {
					if ((p.parent_alias || "") !== "") return;
					sourceOptions.push({ label: `${p.alias} (${p.child_doctype}) [child]`, value: p.alias });
				});
				const $r = $(`
					<div class="rs-cond-row">
						<select class="form-control rs-cond-left-source" style="max-width:200px;flex:0 0 200px"></select>
						<div class="rs-cond-left-field-wrap" style="min-width:320px;flex:1 1 320px"></div>
						<select class="form-control rs-cond-op" style="max-width:80px;flex:0 0 80px">
							<option value="=">=</option>
							<option value="!=">!=</option>
							<option value=">">&gt;</option>
							<option value=">=">&gt;=</option>
							<option value="<">&lt;</option>
							<option value="<=">&lt;=</option>
						</select>
						<div class="rs-cond-right-field-wrap" style="min-width:320px;flex:1 1 320px"></div>
						<button class="btn btn-default btn-xs rs-cond-remove">&times;</button>
					</div>
				`);
				const $ls = $r.find(".rs-cond-left-source");
				sourceOptions.forEach((o) => $ls.append(`<option value="${frappe.utils.escape_html(o.value)}">${frappe.utils.escape_html(o.label)}</option>`));
				$ls.val(cond.left_source || "");

				// Searchable typeahead — Insights-style "click opens full list,
				// type to filter". Frappe's Autocomplete already configures
				// Awesomplete with minChars=0 and FILTER_CONTAINS (substring,
				// case-insensitive across label+value) — we don't need to
				// override its filter. We just need to feed options through
				// set_data so the control's _data map is populated; otherwise
				// get_input_value() can't translate the label the user picked
				// back to the underlying field path.
				const buildFieldPicker = ($parent, placeholder, onPick) => {
					const ctl = frappe.ui.form.make_control({
						parent: $parent[0],
						df: {
							fieldtype: "Autocomplete",
							fieldname: `f_${ci}_${Math.random().toString(36).slice(2, 8)}`,
							placeholder,
							options: [],
							onchange: function () {
								onPick(ctl.get_value());
							},
						},
						render_input: true,
					});
					return ctl;
				};

				const fieldsToOptions = (fields) => (fields || []).map((f) => ({
					label: `${f.label} (${f.path})`,
					value: f.path,
				}));

				const setPickerOptions = (ctl, options) => {
					// set_data populates _data + awesomplete.list together,
					// which is required for the picker to round-trip values.
					if (typeof ctl.set_data === "function") {
						ctl.set_data(options);
					} else {
						ctl.df.options = options;
						if (ctl.awesomplete) ctl.awesomplete.list = options;
					}
				};

				const lfControl = buildFieldPicker(
					$r.find(".rs-cond-left-field-wrap"),
					__("Click or type to pick a field"),
					(v) => { cond.left_path = v || ""; }
				);

				const refreshLeftOptions = (initial = false) => {
					const src = $ls.val();
					const fields = src ? lookupSourceFields(src) : baseFields;
					setPickerOptions(lfControl, fieldsToOptions(fields));
					// Setting value after set_data so the autocomplete's _data
					// map has the entry and the input renders the label, not
					// the raw path.
					if (initial && cond.left_path) lfControl.set_value(cond.left_path);
				};
				refreshLeftOptions(true);
				$ls.on("change", () => {
					cond.left_source = $ls.val();
					cond.left_path = "";
					lfControl.set_value("");
					refreshLeftOptions();
				});

				const $op = $r.find(".rs-cond-op").val(cond.operator || "=").on("change", () => { cond.operator = $op.val(); });

				const rfControl = buildFieldPicker(
					$r.find(".rs-cond-right-field-wrap"),
					__("Click or type to pick a field"),
					(v) => { cond.right_path = v || ""; }
				);
				setPickerOptions(rfControl, fieldsToOptions(rightFields));
				if (cond.right_path) rfControl.set_value(cond.right_path);

				$r.find(".rs-cond-remove").on("click", () => { initial.conditions.splice(ci, 1); renderConds(); });
				$tbl.append($r);
			});
			const $add = $(`<button class="btn btn-default btn-xs">+ ${__("Add Condition")}</button>`);
			$add.on("click", () => {
				initial.conditions.push({ left_source: "", left_path: "", operator: "=", right_path: "" });
				renderConds();
			});
			$cond.append($add);
		};

		// Render child-to-child match conditions per ticked related-side child.
		// Required for any ticked related-side child — without it the engine
		// would emit an unconstrained child join (cartesian rows).
		const renderChildMatchSection = async () => {
			$childCond.empty();
			if (!pendingRelatedChildJoins.length) {
				$childCond.append(
					`<div class="text-muted small">${__("Tick a child table on the related side to set row-pair conditions.")}</div>`
				);
				return;
			}
			$childCond.append(
				`<div class="text-muted small" style="margin-bottom:8px">${__("How should child rows pair up? Required for each ticked related-side child.")}</div>`
			);

			// Build the left-source options once: base + base-side child aliases.
			const leftSourceOptions = [{ label: this.state.baseDoctype || __("Base"), value: "" }];
			this.state.relatedSources.forEach((other, idx) => {
				if (editIdx != null && idx >= editIdx) return;
				if (!other.is_child_table) return;
				if (initialChildAliases.has(other.alias)) return;
				const parentAlias = (other.conditions || [])[0]?.left_source ?? "";
				if (parentAlias !== "") return;
				leftSourceOptions.push({
					label: `${other.alias} (${other.related_doctype}) [child]`,
					value: other.alias,
				});
			});
			pendingBaseChildJoins.forEach((p) => {
				if ((p.parent_alias || "") !== "") return;
				leftSourceOptions.push({
					label: `${p.alias} (${p.child_doctype}) [child]`,
					value: p.alias,
				});
			});

			const baseDoctypeFields = this._flattenFields(
				await this._cachedFields("", this.state.baseDoctype)
			);

			for (const p of pendingRelatedChildJoins) {
				const rightFields = this._flattenFields(
					await this._cachedFields(`dt::${p.child_doctype}`, p.child_doctype)
				);
				const list = pendingChildMatch[p.alias] = pendingChildMatch[p.alias] || [];

				const $sec = $(`<div class="rs-child-match-section" style="margin-bottom:16px;border:1px solid var(--border-color,#cbd5e1);border-radius:6px;padding:10px"></div>`);
				$sec.append(
					$(`<div style="font-weight:600;margin-bottom:6px"></div>`).text(
						`${p.alias} (${p.child_doctype})`
					)
				);

				const $tbl = $(`<div class="rs-cond-table"></div>`);
				$sec.append($tbl);

				list.forEach((mc, mi) => {
					const $r = $(`
						<div class="rs-cond-row">
							<select class="form-control rs-cm-left-source" style="max-width:200px;flex:0 0 200px"></select>
							<div class="rs-cm-left-wrap" style="min-width:320px;flex:1 1 320px"></div>
							<select class="form-control rs-cm-op" style="max-width:80px;flex:0 0 80px">
								<option value="=">=</option>
								<option value="!=">!=</option>
								<option value=">">&gt;</option>
								<option value=">=">&gt;=</option>
								<option value="<">&lt;</option>
								<option value="<=">&lt;=</option>
							</select>
							<div class="rs-cm-right-wrap" style="min-width:320px;flex:1 1 320px"></div>
							<button class="btn btn-default btn-xs rs-cm-remove">&times;</button>
						</div>
					`);
					const $ls = $r.find(".rs-cm-left-source");
					leftSourceOptions.forEach((o) =>
						$ls.append(
							`<option value="${frappe.utils.escape_html(o.value)}">${frappe.utils.escape_html(o.label)}</option>`
						)
					);
					$ls.val(mc.left_source || "");

					const lfCtl = frappe.ui.form.make_control({
						parent: $r.find(".rs-cm-left-wrap")[0],
						df: {
							fieldtype: "Autocomplete",
							fieldname: `cm_l_${p.alias}_${mi}`,
							placeholder: __("Click or type to pick a field"),
							options: [],
							onchange: function () {
								mc.left_path = lfCtl.get_value() || "";
							},
						},
						render_input: true,
					});

					const fieldsToOptions = (fields) =>
						(fields || []).map((f) => ({ label: `${f.label} (${f.path})`, value: f.path }));
					const setOptions = (ctl, opts) => {
						if (typeof ctl.set_data === "function") ctl.set_data(opts);
						else {
							ctl.df.options = opts;
							if (ctl.awesomplete) ctl.awesomplete.list = opts;
						}
					};
					const refreshLeft = (initial = false) => {
						const src = $ls.val();
						const fields = src ? lookupSourceFields(src) : baseDoctypeFields;
						setOptions(lfCtl, fieldsToOptions(fields));
						if (initial && mc.left_path) lfCtl.set_value(mc.left_path);
					};
					refreshLeft(true);
					$ls.on("change", () => {
						mc.left_source = $ls.val();
						mc.left_path = "";
						lfCtl.set_value("");
						refreshLeft();
					});

					const $op = $r.find(".rs-cm-op").val(mc.operator || "=");
					$op.on("change", () => {
						mc.operator = $op.val();
					});

					const rfCtl = frappe.ui.form.make_control({
						parent: $r.find(".rs-cm-right-wrap")[0],
						df: {
							fieldtype: "Autocomplete",
							fieldname: `cm_r_${p.alias}_${mi}`,
							placeholder: __("Click or type to pick a field"),
							options: [],
							onchange: function () {
								mc.right_path = rfCtl.get_value() || "";
							},
						},
						render_input: true,
					});
					setOptions(rfCtl, fieldsToOptions(rightFields));
					if (mc.right_path) rfCtl.set_value(mc.right_path);

					$r.find(".rs-cm-remove").on("click", () => {
						list.splice(mi, 1);
						renderChildMatchSection();
					});

					$tbl.append($r);
				});

				const $add = $(`<button class="btn btn-default btn-xs">+ ${__("Add Condition")}</button>`);
				$add.on("click", async () => {
					list.push({ left_source: "", left_path: "", operator: "=", right_path: "" });
					await renderChildMatchSection();
				});
				$sec.append($add);

				$childCond.append($sec);
			}
		};

		const $childWrap = d.get_field("child_html").$wrapper;
		// Bumped on every renderChildSection call so concurrent renders (the
		// related_doctype field fires both df.onchange AND a jQuery change event)
		// don't duplicate-append after their awaits.
		let childRenderToken = 0;

		const renderChildSection = async () => {
			const myToken = ++childRenderToken;
			const baseSections = [];
			if (this.state.baseDoctype) {
				const baseChildren = await RB_API.getChildTables(this.state.baseDoctype);
				if (myToken !== childRenderToken) return;
				if (baseChildren.length) {
					baseSections.push({
						ownerAlias: "",
						ownerLabel: `${this.state.baseDoctype} (${__("base")})`,
						children: baseChildren,
						side: "base",
					});
				}
			}
			for (const [idx, other] of this.state.relatedSources.entries()) {
				if (editIdx != null && idx >= editIdx) continue;
				if (other.is_child_table) continue; // already a child join itself
				const cs = await RB_API.getChildTables(other.related_doctype);
				if (myToken !== childRenderToken) return;
				if (cs.length) {
					baseSections.push({
						ownerAlias: other.alias,
						ownerLabel: `${other.alias} (${other.related_doctype})`,
						children: cs,
						side: "base",
					});
				}
			}
			const rightDt = d.get_value("related_doctype");
			let relatedSection = null;
			if (rightDt) {
				const rs = await RB_API.getChildTables(rightDt);
				if (myToken !== childRenderToken) return;
				if (rs.length) {
					relatedSection = {
						ownerAlias: "__new__",
						ownerLabel: `${rightDt} (${__("this related")})`,
						children: rs,
						side: "related",
					};
				}
			}

			// All async work done — clear and render synchronously so a later
			// stale call (already short-circuited above) can't append on top.
			$childWrap.empty();
			if (!baseSections.length && !relatedSection) {
				$childWrap.append(`<div class="text-muted small">${__("No child tables to join.")}</div>`);
				return;
			}
			$childWrap.append(`<div class="text-muted small" style="margin-bottom:8px">${__("Tick a child table to add it as a join. Each ticked table becomes a separate related source.")}</div>`);

			const renderChecks = ($host, sec) => {
				$host.empty();
				const arr = sec.side === "base" ? pendingBaseChildJoins : pendingRelatedChildJoins;
				sec.children.forEach((ct) => {
					const $row = $(`<label class="rs-child-row" style="display:flex; gap:8px; align-items:center; margin:2px 0"><input type="checkbox" class="rs-child-cb" style="margin:0" /><span class="rs-child-label"></span></label>`);
					$row.find(".rs-child-label").text(`${ct.label} (${ct.child_doctype})`);
					const $cb = $row.find(".rs-child-cb");
					const existing = arr.find((p) => p.parent_alias === sec.ownerAlias && p.parent_field === ct.fieldname);
					$cb.prop("checked", !!existing);
					$cb.on("change", async () => {
						if ($cb.is(":checked")) {
							const alias = ensureUniqueAlias(sanitizeAlias(`${sec.ownerAlias || "base"}_${ct.fieldname}`));
							arr.push({
								alias,
								child_doctype: ct.child_doctype,
								parent_alias: sec.ownerAlias,
								parent_field: ct.fieldname,
								side: sec.side,
							});
							// Pre-fetch the child doctype's fields so the source dropdown
							// in the conditions table can populate immediately.
							await this._cachedFields(`dt::${ct.child_doctype}`, ct.child_doctype);
						} else {
							const idx = arr.findIndex((p) => p.parent_alias === sec.ownerAlias && p.parent_field === ct.fieldname);
							const removed = idx >= 0 ? arr.splice(idx, 1)[0] : null;
							if (removed) {
								// Drop child-match entries that referenced this child.
								if (sec.side === "related") {
									delete pendingChildMatch[removed.alias];
								} else {
									Object.values(pendingChildMatch).forEach((list) => {
										for (let i = list.length - 1; i >= 0; i--) {
											if (list[i].left_source === removed.alias) list.splice(i, 1);
										}
									});
								}
							}
						}
						await renderConds();
						await renderChildMatchSection();
					});
					$host.append($row);
				});
			};

			if (baseSections.length) {
				const $baseWrap = $(`<div class="rs-child-base-wrap" style="margin-bottom:12px"></div>`);
				$baseWrap.append($(`<div class="text-muted small" style="margin-bottom:4px;font-weight:600"></div>`).text(__("Base Doc")));
				const $select = $(`<select class="form-control rs-child-base-select" style="max-width:520px;margin-bottom:8px"></select>`);
				baseSections.forEach((sec, i) => {
					$select.append(`<option value="${i}">${frappe.utils.escape_html(sec.ownerLabel)}</option>`);
				});
				$baseWrap.append($select);
				const $checks = $(`<div class="rs-child-base-checks"></div>`);
				$baseWrap.append($checks);
				renderChecks($checks, baseSections[0]);
				$select.on("change", () => {
					const idx = parseInt($select.val(), 10) || 0;
					renderChecks($checks, baseSections[idx]);
				});
				$childWrap.append($baseWrap);
			}

			if (relatedSection) {
				const $sec = $(`<div class="rs-child-section" style="margin-bottom:12px"></div>`);
				$sec.append($(`<div class="text-muted small" style="margin-bottom:6px;font-weight:600"></div>`).text(relatedSection.ownerLabel));
				const $checks = $(`<div></div>`);
				renderChecks($checks, relatedSection);
				$sec.append($checks);
				$childWrap.append($sec);
			}
		};

		d.fields_dict.related_doctype.df.onchange = () => {
			renderConds();
			renderChildSection();
			renderChildMatchSection();
		};
		d.fields_dict.related_doctype.$input?.on("change", () => {
			renderConds();
			renderChildSection();
			renderChildMatchSection();
		});
		await renderConds();
		await renderChildSection();
		await renderChildMatchSection();
		d.show();
	}

	_lookupFieldsForSource(source) {
		const fields = (this.sourceFields && this.sourceFields[source]) || [];
		return this._flattenFields(fields);
	}

	_flattenFields(fields, out = []) {
		(fields || []).forEach((f) => {
			out.push(f);
			if (f.children) this._flattenFields(f.children, out);
		});
		return out;
	}

	async _cachedFields(_cacheKey, doctype) {
		// Light cache keyed by doctype name; reuses same fetch for related-source palettes.
		this._fieldCache = this._fieldCache || new Map();
		if (!doctype) return [];
		const k = `dt::${doctype}`;
		if (this._fieldCache.has(k)) return this._fieldCache.get(k);
		const fields = await RB_API.getFields(doctype);
		this._fieldCache.set(k, fields);
		return fields;
	}

	_removeRelatedSource(idx) {
		const rs = this.state.relatedSources[idx];
		if (!rs) return;
		this.state.relatedSources.splice(idx, 1);
		// purge any rows referencing this source
		const filterFn = (r) => (r.source || "") !== rs.alias;
		this.state.columns = this.state.columns.filter(filterFn);
		this.state.filters = this.state.filters.filter(filterFn);
		this.state.groupBy = this.state.groupBy.filter(filterFn);
		this._dropSource(rs.alias);
		this._renderRelatedList();
		this._renderPalette();
		this._renderAllBuckets();
	}

	_addCalcDialog(editIdx = null) {
		if (!this.state.baseDoctype) {
			frappe.show_alert({ message: __("Pick a base DocType first."), indicator: "orange" });
			return;
		}
		const editing = editIdx != null ? this.state.calculations[editIdx] : null;
		const initial = editing
			? JSON.parse(JSON.stringify(editing))
			: {
				alias: "",
				label: "",
				format_type: "Number",
				expression: {
					op: "-",
					left: { type: "field", source: "", path: "" },
					right: { type: "field", source: "", path: "" },
				},
			};

		const d = new frappe.ui.Dialog({
			title: editing ? __("Edit Calculation") : __("Add Calculation"),
			size: "large",
			fields: [
				{ fieldname: "alias", fieldtype: "Data", label: __("Name (alias)"), reqd: 1 },
				{ fieldname: "label", fieldtype: "Data", label: __("Display Label") },
				{ fieldname: "format_type", fieldtype: "Select", label: __("Format"), options: "Number\nInteger\nCurrency\nPercent", default: "Number" },
				{ fieldname: "operand_section", fieldtype: "Section Break", label: __("Formula") },
				{ fieldname: "expr_html", fieldtype: "HTML" },
			],
			primary_action_label: editing ? __("Save") : __("Add"),
			primary_action: (values) => {
				if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(values.alias)) {
					frappe.show_alert({ message: __("Name must be letters/numbers/underscore."), indicator: "orange" });
					return;
				}
				const expr = initial.expression;
				const validOperand = (op) => op && (op.type === "const" || (op.type === "field" && op.path));
				if (!validOperand(expr.left) || !validOperand(expr.right)) {
					frappe.show_alert({ message: __("Both sides need a field or constant."), indicator: "orange" });
					return;
				}
				const calc = {
					alias: values.alias.trim(),
					label: (values.label || "").trim() || values.alias.trim(),
					format_type: values.format_type || "Number",
					expression: expr,
				};
				if (editing) {
					this.state.calculations[editIdx] = calc;
				} else {
					this.state.calculations.push(calc);
					// Auto-add as a column so the user sees it in the
					// preview without having to drag it from the palette.
					// (Drag is still available for re-adding after remove.)
					const alreadyInColumns = this.state.columns.some(
						(c) => c.calculation_alias === calc.alias
					);
					if (!alreadyInColumns) this._addCalcColumn(calc.alias);
				}
				d.hide();
				this._renderCalcList();
				this._renderPalette();
				this._renderAllBuckets();
			},
		});

		d.set_value("alias", initial.alias);
		d.set_value("label", initial.label);
		d.set_value("format_type", initial.format_type);

		const $expr = d.get_field("expr_html").$wrapper;
		const renderExpr = async () => {
			$expr.empty();
			const $row = $(`
				<div class="rs-calc-builder">
					<div class="rs-calc-operand"></div>
					<div class="rs-calc-op-row">
						<select class="form-control rs-calc-op">
							<option value="+">+</option>
							<option value="-">−</option>
							<option value="*">×</option>
							<option value="/">÷</option>
						</select>
					</div>
					<div class="rs-calc-operand2"></div>
				</div>
			`);
			$expr.append($row);
			$row.find(".rs-calc-op").val(initial.expression.op || "-").on("change", function () {
				initial.expression.op = $(this).val();
			});
			await this._mountOperand($row.find(".rs-calc-operand")[0], initial.expression, "left");
			await this._mountOperand($row.find(".rs-calc-operand2")[0], initial.expression, "right");
		};
		renderExpr();
		d.show();
	}

	async _mountOperand(host, parentExpr, side) {
		const operand = parentExpr[side] || (parentExpr[side] = { type: "field", source: "", path: "" });
		const $h = $(host);
		$h.empty();
		const $type = $(`
			<select class="form-control">
				<option value="field">${__("Field")}</option>
				<option value="const">${__("Number")}</option>
			</select>
		`);
		$type.val(operand.type || "field");
		$h.append($type);
		const $body = $(`<div class="rs-operand-body"></div>`);
		$h.append($body);
		const NUMERIC_FIELDTYPES = ["Int", "Float", "Currency", "Percent", "Duration", "Long Int"];
		const renderBody = async () => {
			$body.empty();
			if (operand.type === "field") {
				const sources = [{ label: this.state.baseDoctype || __("Base"), value: "" }];
				this.state.relatedSources.forEach((rs) =>
					sources.push({ label: `${rs.alias} (${rs.related_doctype})`, value: rs.alias })
				);
				const $src = $(`<select class="form-control" style="max-width:200px;flex:0 0 200px"></select>`);
				sources.forEach((s) => $src.append(`<option value="${frappe.utils.escape_html(s.value)}">${frappe.utils.escape_html(s.label)}</option>`));
				$src.val(operand.source || "");
				const $fieldWrap = $(`<div class="rs-calc-field-wrap" style="min-width:280px;flex:1 1 280px"></div>`);

				const fieldCtl = frappe.ui.form.make_control({
					parent: $fieldWrap[0],
					df: {
						fieldtype: "Autocomplete",
						fieldname: `calc_f_${Math.random().toString(36).slice(2, 8)}`,
						placeholder: __("Click or type to pick a numeric field"),
						options: [],
						onchange: function () {
							operand.path = fieldCtl.get_value() || "";
						},
					},
					render_input: true,
				});
				if (operand.path) fieldCtl.set_value(operand.path);

				const setOptions = (ctl, opts) => {
					if (typeof ctl.set_data === "function") ctl.set_data(opts);
					else {
						ctl.df.options = opts;
						if (ctl.awesomplete) ctl.awesomplete.list = opts;
					}
				};

				const fillFields = async () => {
					const src = $src.val();
					const dt = src
						? (this.state.relatedSources.find((r) => r.alias === src) || {}).related_doctype
						: this.state.baseDoctype;
					const raw = (await this._cachedFields(`dt::${dt}`, dt)) || [];
					const flat = this._flattenFields(raw);
					const numeric = flat.filter((f) => NUMERIC_FIELDTYPES.includes(f.fieldtype));
					setOptions(
						fieldCtl,
						numeric.map((f) => ({ label: `${f.label} (${f.path})`, value: f.path }))
					);
				};
				$src.on("change", async () => {
					operand.source = $src.val();
					operand.path = "";
					fieldCtl.set_value("");
					await fillFields();
				});
				$body.append($src).append($fieldWrap);
				await fillFields();
			} else {
				const $val = $(`<input type="number" class="form-control" placeholder="${__("Number")}" />`);
				$val.val(operand.value ?? "");
				$val.on("change", () => { operand.value = parseFloat($val.val() || "0"); });
				$body.append($val);
			}
		};
		$type.on("change", () => {
			operand.type = $type.val();
			if (operand.type === "field") {
				if (operand.source === undefined) operand.source = "";
				if (operand.path === undefined) operand.path = "";
			} else {
				if (operand.value === undefined) operand.value = 0;
			}
			renderBody();
		});
		await renderBody();
	}

	_removeCalculation(idx) {
		const calc = this.state.calculations[idx];
		if (!calc) return;
		this.state.calculations.splice(idx, 1);
		this.state.columns = this.state.columns.filter((c) => c.calculation_alias !== calc.alias);
		this._renderCalcList();
		this._renderPalette();
		this._renderAllBuckets();
	}

	_columnIdFor(col) {
		if (col.calculation_alias) return `calc:${col.calculation_alias}`;
		if (col.source) return `${col.source}:${col.field_path}`;
		return col.field_path;
	}

	_columnsForRulePicker() {
		// Build {value, label} list of currently configured columns.
		return this.state.columns.map((c) => ({
			value: this._columnIdFor(c),
			label: c.label || (c.calculation_alias ? c.calculation_alias : c.field_path),
		}));
	}

	_visibilityDialog(item, after) {
		const cols = this._columnsForRulePicker();
		const initial = item.visibility_rule || { column_id: cols[0]?.value || "", operator: "=", value: "" };
		const d = new frappe.ui.Dialog({
			title: __("Show this column only when…"),
			fields: [
				{
					fieldname: "column_id",
					fieldtype: "Select",
					label: __("Compare column"),
					options: cols.map((c) => `${c.value}\t${c.label}`).map((s) => s.split("\t")[0]).join("\n"),
					default: initial.column_id,
				},
				{
					fieldname: "operator",
					fieldtype: "Select",
					label: __("Condition"),
					options: ["=", "!=", ">", ">=", "<", "<=", "contains", "starts_with", "is_set", "is_not_set"].join("\n"),
					default: initial.operator || "=",
				},
				{
					fieldname: "value",
					fieldtype: "Data",
					label: __("Value"),
					default: initial.value || "",
					depends_on: "eval:!['is_set','is_not_set'].includes(doc.operator)",
				},
				{
					fieldname: "clear_section",
					fieldtype: "Section Break",
				},
				{
					fieldname: "clear",
					fieldtype: "Button",
					label: __("Remove rule"),
				},
			],
			primary_action_label: __("Save"),
			primary_action: ({ column_id, operator, value }) => {
				if (!column_id) {
					frappe.show_alert({ message: __("Pick a column first."), indicator: "orange" });
					return;
				}
				item.visibility_rule = {
					column_id,
					operator: operator || "=",
					value: value || "",
				};
				d.hide();
				if (after) after();
			},
		});
		d.fields_dict.clear.input.onclick = () => {
			item.visibility_rule = null;
			d.hide();
			if (after) after();
		};
		d.show();
	}

	_formatRulesDialog(item, after) {
		const cols = this._columnsForRulePicker();
		const local = JSON.parse(JSON.stringify(item.format_rules || []));
		const d = new frappe.ui.Dialog({
			title: __("Conditional Formatting"),
			size: "large",
			fields: [
				{ fieldname: "intro", fieldtype: "HTML", options: `<div class="text-muted small">${__("Rules are checked top-to-bottom. The first match wins.")}</div>` },
				{ fieldname: "rules_html", fieldtype: "HTML" },
			],
			primary_action_label: __("Save"),
			primary_action: () => {
				item.format_rules = local.filter((r) => r.column_id);
				d.hide();
				if (after) after();
			},
		});
		const $rules = d.get_field("rules_html").$wrapper;
		const presets = [
			{ key: "red", bg: "#fee2e2", fg: "#991b1b", label: __("Red") },
			{ key: "green", bg: "#dcfce7", fg: "#166534", label: __("Green") },
			{ key: "orange", bg: "#ffedd5", fg: "#9a3412", label: __("Orange") },
			{ key: "yellow", bg: "#fef9c3", fg: "#854d0e", label: __("Yellow") },
			{ key: "blue", bg: "#dbeafe", fg: "#1e40af", label: __("Blue") },
			{ key: "gray", bg: "#f3f4f6", fg: "#374151", label: __("Gray") },
		];
		const render = () => {
			$rules.empty();
			local.forEach((rule, idx) => {
				const $r = $(`
					<div class="rs-fmt-row">
						<select class="form-control rs-fmt-col" style="max-width:160px"></select>
						<select class="form-control rs-fmt-op" style="max-width:130px">
							<option value="=">=</option>
							<option value="!=">!=</option>
							<option value=">">&gt;</option>
							<option value=">=">&gt;=</option>
							<option value="<">&lt;</option>
							<option value="<=">&lt;=</option>
							<option value="contains">${__("contains")}</option>
							<option value="starts_with">${__("starts with")}</option>
							<option value="is_set">${__("is set")}</option>
							<option value="is_not_set">${__("is not set")}</option>
						</select>
						<input type="text" class="form-control rs-fmt-val" placeholder="${__("Value")}" style="max-width:120px" />
						<select class="form-control rs-fmt-color" style="max-width:120px"></select>
						<button class="btn btn-default btn-xs rs-fmt-remove">&times;</button>
					</div>
				`);
				const $c = $r.find(".rs-fmt-col");
				cols.forEach((c) => $c.append(`<option value="${frappe.utils.escape_html(c.value)}">${frappe.utils.escape_html(c.label)}</option>`));
				$c.val(rule.column_id || "").on("change", () => { rule.column_id = $c.val(); });
				$r.find(".rs-fmt-op").val(rule.operator || "=").on("change", function () {
					rule.operator = $(this).val();
					$r.find(".rs-fmt-val").toggle(!["is_set", "is_not_set"].includes(rule.operator));
				});
				$r.find(".rs-fmt-val").val(rule.value || "").on("input", function () {
					rule.value = $(this).val();
				}).toggle(!["is_set", "is_not_set"].includes(rule.operator || "="));
				const $color = $r.find(".rs-fmt-color");
				presets.forEach((p) =>
					$color.append(`<option value="${p.key}">${p.label}</option>`)
				);
				$color.val(rule.preset || "red").on("change", () => {
					rule.preset = $color.val();
					const found = presets.find((p) => p.key === rule.preset);
					rule.background = found?.bg;
					rule.color = found?.fg;
				});
				if (!rule.background) {
					const found = presets.find((p) => p.key === (rule.preset || "red"));
					rule.background = found?.bg;
					rule.color = found?.fg;
					rule.preset = rule.preset || "red";
				}
				$r.find(".rs-fmt-remove").on("click", () => {
					local.splice(idx, 1);
					render();
				});
				$rules.append($r);
			});
			const $add = $(`<button class="btn btn-default btn-xs">+ ${__("Add Rule")}</button>`);
			$add.on("click", () => {
				local.push({
					column_id: cols[0]?.value || "",
					operator: "=",
					value: "",
					preset: "red",
					background: "#fee2e2",
					color: "#991b1b",
				});
				render();
			});
			$rules.append($add);
		};
		render();
		d.show();
	}

	_evalRule(rule, row) {
		if (!rule || !rule.column_id) return null;
		const cell = row[rule.column_id];
		const op = rule.operator || "=";
		const v = rule.value;
		if (op === "is_set") return cell !== null && cell !== undefined && cell !== "";
		if (op === "is_not_set") return cell === null || cell === undefined || cell === "";
		const cellNum = parseFloat(cell);
		const vNum = parseFloat(v);
		const numericCmp = !Number.isNaN(cellNum) && !Number.isNaN(vNum);
		switch (op) {
			case "=": return String(cell ?? "") === String(v ?? "");
			case "!=": return String(cell ?? "") !== String(v ?? "");
			case ">": return numericCmp ? cellNum > vNum : String(cell ?? "") > String(v ?? "");
			case ">=": return numericCmp ? cellNum >= vNum : String(cell ?? "") >= String(v ?? "");
			case "<": return numericCmp ? cellNum < vNum : String(cell ?? "") < String(v ?? "");
			case "<=": return numericCmp ? cellNum <= vNum : String(cell ?? "") <= String(v ?? "");
			case "contains": return String(cell ?? "").toLowerCase().includes(String(v ?? "").toLowerCase());
			case "starts_with": return String(cell ?? "").toLowerCase().startsWith(String(v ?? "").toLowerCase());
		}
		return false;
	}

	_wirePreview() {
		this.$previewBtn.on("click", () => { this.state.page = 1; this._runPreview(); });
		this.$root.find(".rs-page-prev").on("click", () => {
			if (this.state.page > 1) { this.state.page -= 1; this._runPreview(); }
		});
		this.$root.find(".rs-page-next").on("click", () => {
			const totalPages = this._totalPages();
			if (this.state.page < totalPages) { this.state.page += 1; this._runPreview(); }
		});
		this.$root.find(".rs-page-size").on("change", (e) => {
			this.state.pageSize = parseInt(e.target.value, 10) || 20;
			this.state.page = 1;
			this._runPreview();
		});
	}

	_totalPages() {
		if (!this.lastResult) return 1;
		return Math.max(1, Math.ceil((this.lastResult.total || 0) / (this.lastResult.page_size || 20)));
	}

	_buildConfig() {
		return {
			base_doctype: this.state.baseDoctype,
			related_sources: (this.state.relatedSources || []).map((rs) => ({
				alias: rs.alias,
				related_doctype: rs.related_doctype,
				join_type: rs.join_type || "Left Join",
				// Persist the child-table flags so reopening the report
				// recognises these as nested children (rendered under the
				// parent Table field) instead of separate palette sections.
				is_child_table: !!rs.is_child_table,
				child_parent_field: rs.child_parent_field || "",
				conditions: (rs.conditions || []).map((c) => ({
					left_source: c.left_source || "",
					left_path: c.left_path,
					operator: c.operator || "=",
					right_path: c.right_path,
				})),
			})),
			calculations: (this.state.calculations || []).map((c) => ({
				alias: c.alias,
				label: c.label || c.alias,
				format_type: c.format_type || "Number",
				expression: c.expression,
			})),
			columns: this.state.columns.map((c) => {
				const ruleBits = {
					visibility_rule: c.visibility_rule || null,
					format_rules: c.format_rules || [],
				};
				if (c.calculation_alias) {
					return {
						calculation_alias: c.calculation_alias,
						label: c.label || null,
						width: c.width || null,
						...ruleBits,
					};
				}
				return {
					source: c.source || "",
					field_path: c.field_path,
					label: c.label || null,
					fieldtype: c.fieldtype,
					aggregate: c.aggregate || "",
					...ruleBits,
				};
			}),
			filters: this.state.filters.map((f) => ({
				source: f.source || "",
				field_path: f.field_path,
				fieldtype: f.fieldtype,
				operator: f.operator,
				value: f.value ?? "",
				value_to: f.value_to ?? "",
				value_list: f.value_list ?? "",
				granularity: f.granularity || "",
				is_runtime: !!f.is_runtime,
			})),
			group_by: this.state.groupBy.map((g) => ({
				source: g.source || "",
				field_path: g.field_path,
				fieldtype: g.fieldtype,
				granularity: g.granularity || "",
			})),
			sort: [],
		};
	}

	async _runPreview() {
		if (!this.state.baseDoctype) {
			frappe.show_alert({ message: __("Please select a DocType."), indicator: "orange" });
			return;
		}
		if (!this.state.columns.length && !this.state.groupBy.length) {
			frappe.show_alert({ message: __("Add at least one column."), indicator: "orange" });
			return;
		}
		this.$preview.html(`<div class="text-muted">${__("Running…")}</div>`);
		try {
			const cfg = this._buildConfig();
			const res = await RB_API.preview(cfg, this.state.page, this.state.pageSize);
			this.lastResult = res;
			this._renderPreview(res);
			this._updatePager(res);
		} catch (e) {
			console.error(e);
			this.$preview.html(`<div class="text-danger">${__("Failed to run report.")} ${frappe.utils.escape_html(e.message || "")}</div>`);
		}
	}

	_renderPreview(res) {
		this.$preview.empty();
		if (!res.rows.length) {
			this.$preview.html(`<div class="text-muted">${__("No rows match your filters.")}</div>`);
			return;
		}
		const columns = res.columns.map((c) => ({
			id: c.fieldname,
			name: c.label,
			width: c.width || 160,
			editable: false,
			format: (value, row, column, data) => this._formatCell(value, c, data),
		}));
		const data = res.rows.map((row) => {
			const obj = {};
			res.columns.forEach((c, i) => { obj[c.fieldname] = row[i]; });
			return obj;
		});
		this._dataTable = new frappe.DataTable(this.$preview[0], {
			columns,
			data,
			// "fixed" keeps each column at its requested width so the table can
			// exceed the container width, which is what gives DataTable its
			// horizontal scroll. "fluid" stretches columns to fit and never
			// scrolls horizontally.
			layout: "fixed",
			noDataMessage: __("No rows match your filters."),
			inlineFilters: false,
		});
	}

	_formatCell(value, column, row) {
		// Visibility rule: if set and the row fails it, blank the cell.
		if (column.visibility_rule && row) {
			if (!this._evalRule(column.visibility_rule, row)) return "";
		}

		if (value === null || value === undefined || value === "") return "";
		const display = frappe.utils.escape_html(String(value));
		let inner = display;
		if (column.link_doctype) {
			const slug = column.link_doctype.toLowerCase().replace(/ /g, "-");
			const href = `/app/${slug}/${encodeURIComponent(value)}`;
			inner = `<a href="${href}" target="_blank" rel="noopener" class="rs-cell-link">${display}</a>`;
		}

		// Format rules: first match wins.
		if (column.format_rules && column.format_rules.length && row) {
			for (const rule of column.format_rules) {
				if (this._evalRule(rule, row)) {
					const styles = [];
					if (rule.background) styles.push(`background:${rule.background}`);
					if (rule.color) styles.push(`color:${rule.color}`);
					styles.push("padding:2px 6px");
					styles.push("border-radius:3px");
					return `<span style="${styles.join(";")}">${inner}</span>`;
				}
			}
		}

		return inner;
	}

	_updatePager(res) {
		const start = (res.page - 1) * res.page_size + 1;
		const end = Math.min(res.page * res.page_size, res.total);
		const totalPages = Math.max(1, Math.ceil(res.total / res.page_size));
		if (!res.total) {
			this.$pageInfo.text("");
		} else {
			this.$pageInfo.text(`${start}–${end} of ${res.total} (page ${res.page} / ${totalPages})`);
		}
	}

	_clearPreview() {
		this.lastResult = null;
		this.$preview.empty();
		this.$pageInfo.text("");
	}

	async _save(asNew) {
		console.log("[ReportStudio] save clicked", {
			asNew,
			baseDoctype: this.state.baseDoctype,
			columns: this.state.columns.length,
			docName: this.state.docName,
		});

		if (!this.state.baseDoctype) {
			frappe.msgprint({
				title: __("Cannot save"),
				message: __("Select a DocType first."),
				indicator: "orange",
			});
			return;
		}

		if (!this.state.columns.length && !this.state.groupBy.length) {
			frappe.msgprint({
				title: __("Cannot save"),
				message: __("Add at least one column to the report before saving."),
				indicator: "orange",
			});
			return;
		}

		const title = await this._promptTitle(asNew ? "" : (this.state.title || ""));
		console.log("[ReportStudio] title from prompt:", JSON.stringify(title));
		if (!title) {
			console.log("[ReportStudio] save aborted: empty title");
			return;
		}

		let configJson;
		try {
			configJson = JSON.stringify(this._buildConfig());
		} catch (e) {
			console.error("[ReportStudio] _buildConfig threw", e);
			frappe.msgprint({
				title: __("Cannot save"),
				message: __("Something is wrong with the current configuration: {0}", [e.message || e]),
				indicator: "red",
			});
			return;
		}

		const payload = {
			title,
			base_doctype: this.state.baseDoctype,
			config: configJson,
			visibility: this.state.visibility || "Private",
			shared_roles: JSON.stringify(this.state.sharedRoles || []),
			description: this.state.description || "",
			page_size: this.state.pageSize || 20,
		};
		if (!asNew && this.state.docName) payload.name = this.state.docName;

		console.log("[ReportStudio] sending save payload", payload);
		frappe.show_alert({ message: __("Saving…"), indicator: "blue" });

		try {
			const name = await RB_API.saveReport(payload);
			console.log("[ReportStudio] saved successfully as", name);
			this.state.docName = name;
			this.state.title = title;
			this.page.set_title(`${__("Report Studio")} — ${title}`);
			frappe.show_alert(
				{
					message: __(
						`<b>Saved as {0}</b> &nbsp; <a href="/app/report-studio-report/{1}" target="_blank">${__(
							"open record →"
						)}</a>`,
						[title, name]
					),
					indicator: "green",
				},
				10
			);
		} catch (e) {
			console.error("[ReportStudio] save failed", e, "payload was", payload);
			const detail =
				(e && (e.message || (e._server_messages && e._server_messages.toString()))) ||
				(e && JSON.stringify(e)) ||
				"";
			frappe.msgprint({
				title: __("Could not save report"),
				message: detail || __("Open the browser console (F12) and look for [ReportStudio] save failed."),
				indicator: "red",
			});
		}
	}

	_promptTitle(initial = "") {
		return new Promise((resolve) => {
			frappe.prompt(
				[{ fieldname: "title", fieldtype: "Data", label: __("Report Title"), reqd: 1, default: initial }],
				({ title }) => resolve(title?.trim() || ""),
				__("Save Report"),
				__("Save")
			);
		});
	}

	async _openReportDialog() {
		const reports = await RB_API.listReports("");
		const d = new frappe.ui.Dialog({
			title: __("Open Report"),
			size: "large",
			fields: [
				{ fieldname: "search", fieldtype: "Data", label: __("Search title…") },
				{ fieldname: "list_html", fieldtype: "HTML" },
			],
		});
		const $list = d.get_field("list_html").$wrapper;
		const render = (filter = "") => {
			$list.empty();
			const f = (filter || "").toLowerCase();
			const filtered = reports.filter(
				(r) =>
					!f ||
					(r.title || "").toLowerCase().includes(f) ||
					(r.name || "").toLowerCase().includes(f) ||
					(r.base_doctype || "").toLowerCase().includes(f)
			);
			if (!filtered.length) {
				$list.append(
					`<div class="text-muted small" style="padding:8px">${__(
						"No saved reports yet."
					)}</div>`
				);
				return;
			}
			const $tbl = $(
				`<div class="rs-open-list" style="max-height:50vh;overflow:auto"></div>`
			);
			filtered.forEach((r) => {
				const $row = $(`
					<div class="rs-open-row" style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-top:1px solid var(--border-color,#eee);cursor:pointer">
						<div style="flex:1">
							<div style="font-weight:600">${frappe.utils.escape_html(r.title || r.name)}</div>
							<div class="text-muted small">${frappe.utils.escape_html(r.base_doctype || "")} · ${frappe.utils.escape_html(r.name)}</div>
						</div>
						<a href="/app/report-studio-report/${encodeURIComponent(r.name)}" target="_blank" class="text-muted small">${__("record →")}</a>
					</div>
				`);
				$row.on("click", (e) => {
					if ($(e.target).is("a")) return;
					d.hide();
					this._loadReport(r.name);
				});
				$tbl.append($row);
			});
			$list.append($tbl);
		};
		d.fields_dict.search.$input?.on("input", function () {
			render($(this).val());
		});
		render();
		d.show();
	}

	async _loadReport(name) {
		try {
			const data = await RB_API.loadReport(name);
			this.state = this._emptyState();
			this.state.docName = data.name;
			this.state.title = data.title;
			this.state.description = data.description || "";
			this.state.visibility = data.visibility;
			this.state.pageSize = data.page_size || 20;
			this.state.sharedRoles = data.shared_roles || [];
			this.state.baseDoctype = data.base_doctype;
			this.state.isPublished = !!data.is_published;
			this.state.linkedReportName = data.linked_report_name || "";

			this.doctypePickerCtrl.set_value(data.base_doctype);

			const baseFields = await RB_API.getFields(data.base_doctype);
			this._setFields(baseFields);

			this.state.relatedSources = (data.config.related_sources || []).map((rs) => {
				// Detect the canonical child-table parent linkage so reports
				// saved before is_child_table was persisted still render
				// nested under their parent Table field. The save path now
				// stores these flags directly, but legacy rows omit them.
				const cond0 = (rs.conditions || [])[0] || {};
				const looksLikeChildLinkage = cond0.right_path === "parent" && cond0.left_path === "name";
				return {
					alias: rs.alias,
					related_doctype: rs.related_doctype,
					join_type: rs.join_type || "Left Join",
					is_child_table: !!rs.is_child_table || looksLikeChildLinkage,
					child_parent_field: rs.child_parent_field || "",
					conditions: rs.conditions || [],
				};
			});
			for (const rs of this.state.relatedSources) {
				await this._loadFieldsForSource(rs.alias, rs.related_doctype);
			}
			// Backfill child_parent_field for legacy rows that flagged as
			// child-table via the linkage pattern but didn't have the
			// fieldname stored. We look up the parent source's fields and
			// pick the Table field whose options match the related doctype.
			for (const rs of this.state.relatedSources) {
				if (!rs.is_child_table || rs.child_parent_field) continue;
				const parentAlias = (rs.conditions || [])[0]?.left_source ?? "";
				const parentFields = (this.sourceFields && this.sourceFields[parentAlias]) || [];
				const match = this._flattenFields(parentFields).find(
					(f) => (f.fieldtype === "Table" || f.fieldtype === "Table MultiSelect")
						&& f.options === rs.related_doctype
				);
				if (match) rs.child_parent_field = match.fieldname;
			}

			this.state.calculations = (data.config.calculations || []).map((c) => ({
				alias: c.alias,
				label: c.label || c.alias,
				format_type: c.format_type || "Number",
				expression: c.expression || { op: "+", left: { type: "const", value: 0 }, right: { type: "const", value: 0 } },
			}));

			this.state.columns = (data.config.columns || []).map((c) => ({
				source: c.source || "",
				field_path: c.field_path,
				calculation_alias: c.calculation_alias || null,
				label: c.label,
				fieldtype: c.fieldtype,
				aggregate: c.aggregate || "",
				visibility_rule: c.visibility_rule || null,
				format_rules: c.format_rules || [],
			}));
			this.state.filters = (data.config.filters || []).map((f) => ({
				source: f.source || "",
				field_path: f.field_path,
				fieldtype: f.fieldtype,
				operator: f.operator,
				value: f.value || "",
				value_to: f.value_to || "",
				value_list: f.value_list || "",
				granularity: f.granularity || "",
				is_runtime: !!f.is_runtime,
			}));
			this.state.groupBy = (data.config.group_by || []).map((g) => ({
				source: g.source || "",
				field_path: g.field_path,
				fieldtype: g.fieldtype,
				granularity: g.granularity || "",
			}));
			this._renderRelatedList();
			this._renderCalcList();
			this._renderPalette();
			this._renderAllBuckets();
			this.$root.find(".rs-page-size").val(this.state.pageSize);
			frappe.show_alert({ message: __("Loaded {0}", [data.title]), indicator: "green" });
		} catch (e) {
			console.error(e);
			frappe.show_alert({ message: __("Could not load report."), indicator: "red" });
		}
	}

	async _deleteCurrent() {
		if (!this.state.docName) {
			frappe.show_alert({ message: __("Save the report first."), indicator: "orange" });
			return;
		}
		const ok = await this._confirm(__("Delete this saved report?"));
		if (!ok) return;
		try {
			await RB_API.deleteReport(this.state.docName);
			frappe.show_alert({ message: __("Deleted."), indicator: "green" });
			this._reset();
		} catch (e) {
			frappe.show_alert({ message: __("Could not delete."), indicator: "red" });
		}
	}

	_reset() {
		this.state = this._emptyState();
		try { this.doctypePickerCtrl.set_value(""); } catch (_) {}
		this.fieldsByPath.clear();
		this.sourceFields = {};
		this.$palette.empty();
		this._renderAllBuckets();
		this._renderRelatedList();
		this._renderCalcList();
		this._clearPreview();
	}

	_shareDialog() {
		const d = new frappe.ui.Dialog({
			title: __("Sharing & Visibility"),
			fields: [
				{
					fieldname: "visibility",
					fieldtype: "Select",
					label: __("Visibility"),
					options: ["Private", "Public", "Shared with Roles"].join("\n"),
					default: this.state.visibility || "Private",
				},
				{
					fieldname: "roles_section",
					fieldtype: "Section Break",
					label: __("Roles"),
					depends_on: "eval:doc.visibility=='Shared with Roles'",
				},
				{
					fieldname: "shared_roles",
					fieldtype: "Table MultiSelect",
					label: __("Roles with Read Access"),
					options: "Has Role",
					depends_on: "eval:doc.visibility=='Shared with Roles'",
				},
			],
			primary_action_label: __("Apply"),
			primary_action: ({ visibility, shared_roles }) => {
				this.state.visibility = visibility || "Private";
				const rows = (shared_roles || []).map((r) => ({ role: r.role, can_edit: 0 }));
				this.state.sharedRoles = rows;
				d.hide();
				frappe.show_alert({ message: __("Sharing updated. Save to apply."), indicator: "blue" });
			},
		});
		const initialRoles = (this.state.sharedRoles || []).map((r) => ({ role: r.role }));
		d.set_value("visibility", this.state.visibility || "Private");
		d.set_value("shared_roles", initialRoles);
		d.show();
	}

	_exportDialog() {
		if (!this.state.baseDoctype || !this.state.columns.length) {
			frappe.show_alert({ message: __("Build and preview a report first."), indicator: "orange" });
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __("Export"),
			fields: [
				{
					fieldname: "fmt",
					fieldtype: "Select",
					label: __("Format"),
					options: ["xlsx", "csv", "pdf"].join("\n"),
					default: "xlsx",
				},
			],
			primary_action_label: __("Download"),
			primary_action: ({ fmt }) => {
				d.hide();
				this._downloadExport(fmt || "xlsx");
			},
		});
		d.show();
	}

	_downloadExport(fmt) {
		const cfg = this._buildConfig();
		const params = new URLSearchParams();
		params.set("cmd", "report_builder.api.export.export_report");
		params.set("fmt", fmt);
		if (this.state.docName) {
			params.set("name", this.state.docName);
		} else {
			params.set("config", JSON.stringify(cfg));
			params.set("title", this.state.title || "report");
		}
		const url = `/api/method/report_builder.api.export.export_report?${params.toString()}`;
		const a = document.createElement("a");
		a.href = url;
		a.target = "_blank";
		a.rel = "noopener";
		document.body.appendChild(a);
		a.click();
		setTimeout(() => a.remove(), 0);
	}

	_confirm(msg) {
		return new Promise((resolve) => {
			frappe.confirm(msg, () => resolve(true), () => resolve(false));
		});
	}
}

frappe.pages["report-studio"].on_page_load = function (wrapper) {
	frappe.report_studio.controller = new ReportStudio(wrapper);
};

frappe.pages["report-studio"].on_page_show = function () {
	// noop
};
