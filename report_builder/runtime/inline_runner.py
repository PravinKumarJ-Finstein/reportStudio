# SPDX-License-Identifier: MIT
"""Self-contained runner template embedded in every published Frappe Report.

The string `INLINE_RUNNER` below is plain Python source that runs inside Frappe's
`safe_exec` sandbox (used by Script Reports). The publish step prepends a single
line `CONFIG = {...}` (a Python dict literal of the validated Studio config) and
stores the concatenation in `Report.report_script`.

Why everything lives inside one outer `run(CONFIG)` function:
- `safe_exec` calls Python's builtin `exec` with separate `globals` and `locals`
  dicts. Top-level `def`s end up in locals, but each function's `__globals__`
  points at the (separate) globals dict. So a top-level helper can't see another
  top-level helper. Wrapping everything in `run()` puts every helper in the same
  closure scope, where they can see each other normally.

RestrictedPython constraints we comply with:
- No identifier may begin with `_` (rejected by the compile_restricted transformer
  for variable/function names — but attribute access like `frappe._dict` is fine).
- No multi-target assignment: `a, b = tuple(...)` is forbidden because
  `_unpack_sequence_` is not provided. Use index access (`built[0]`, `built[1]`).
- `for a, b in iterable:` IS allowed (`_iter_unpack_sequence_` IS provided).
- No `import` statements at all.
"""

INLINE_RUNNER = '''
# --- Auto-generated inline runner for Report Studio. Do not edit. ---
# Runtime-filter overlay: filters dict comes from /app/query-report's filter
# bar (frappe.query_reports[name].filters). Each saved Studio filter has a
# stable key (field_path with `.`/`:` swapped to `__`); a non-empty runtime
# value overrides the saved one. value_to uses the same key + "__to".
def apply_runtime_filters(cfg, runtime):
    if not runtime:
        return
    for fcfg in cfg.get("filters") or []:
        key = (fcfg.get("field_path") or "").replace(".", "__").replace(":", "__")
        if key in runtime and runtime[key] not in (None, ""):
            fcfg["value"] = runtime[key]
        key_to = key + "__to"
        if key_to in runtime and runtime[key_to] not in (None, ""):
            fcfg["value_to"] = runtime[key_to]
        key_list = key + "__list"
        if key_list in runtime and runtime[key_list] not in (None, ""):
            fcfg["value_list"] = runtime[key_list]


def run(CONFIG):

    PAGE_SIZE_LIMIT = 10000

    def safe_alias(prefix):
        return "rs_" + "__".join(prefix).replace(" ", "_").lower()

    def related_alias(alias):
        return "rs_src_" + alias

    def aggregate_fn(name):
        # `frappe.qb.functions.<X>` is NOT exposed in safe_exec — only
        # `frappe.qb.terms.*` is flattened. Use CustomFunction wrappers to
        # build the same SQL aggregates from a path that does work.
        CF = frappe.qb.terms.CustomFunction
        fns = {
            "Count": CF("COUNT", ["x"]),
            "Sum": CF("SUM", ["x"]),
            "Avg": CF("AVG", ["x"]),
            "Min": CF("MIN", ["x"]),
            "Max": CF("MAX", ["x"]),
        }
        return fns.get(name)

    def date_bucket(field, gran):
        DateFormat = frappe.qb.terms.CustomFunction("DATE_FORMAT", ["d", "f"])
        YearFn = frappe.qb.terms.CustomFunction("YEAR", ["d"])
        QuarterFn = frappe.qb.terms.CustomFunction("QUARTER", ["d"])
        YearWeekFn = frappe.qb.terms.CustomFunction("YEARWEEK", ["d", "m"])
        ConcatFn = frappe.qb.terms.CustomFunction("CONCAT", ["a", "b", "c"])
        LV = frappe.qb.terms.LiteralValue
        # "Date" is the filter UI's label; "Day" is what Group By stores.
        # Mirror engine.aggregations.date_bucket so both produce identical SQL.
        if gran in ("Day", "Date"):
            return DateFormat(field, LV("'%%Y-%%m-%%d'"))
        if gran == "Week":
            return YearWeekFn(field, LV("3"))
        if gran == "Month":
            return DateFormat(field, LV("'%%Y-%%m'"))
        if gran == "Quarter":
            return ConcatFn(YearFn(field), LV("'-Q'"), QuarterFn(field))
        if gran == "Year":
            return YearFn(field)
        return field

    def cast_value(v, fieldtype):
        if v is None:
            return None
        if fieldtype in ("Int", "Check"):
            return frappe.utils.cint(v)
        if fieldtype in ("Float", "Currency", "Percent"):
            return frappe.utils.flt(v)
        if fieldtype == "Date":
            return frappe.utils.getdate(v)
        if fieldtype == "Datetime":
            return frappe.utils.get_datetime(v)
        return frappe.utils.cstr(v)

    def escape_like(value):
        bs = chr(92)
        s = value or ""
        s = s.replace(bs, bs + bs)
        s = s.replace("%", bs + "%")
        s = s.replace("_", bs + "_")
        return s

    def filter_predicate(field, op, value, value_to, value_list, fieldtype, gran=""):
        # If gran is set on a Date/Datetime field, the field expression is
        # wrapped in date_bucket(...) before this function is called and the
        # value must be cast to match the bucket output (int for Year, str
        # for Month/Date).
        bucketing = bool(gran) and fieldtype in ("Date", "Datetime")

        def cast(v):
            if not bucketing:
                return cast_value(v, fieldtype)
            if v is None:
                return None
            if gran == "Year":
                return frappe.utils.cint(v)
            return frappe.utils.cstr(v)

        if op == "Equals":
            return field == cast(value)
        if op == "Not Equals":
            return field != cast(value)
        if op == "Greater Than":
            return field > cast(value)
        if op == "Less Than":
            return field < cast(value)
        if op == "Between":
            return field.between(cast(value), cast(value_to))
        if op == "Is Set":
            return field.isnotnull() & (field != "")
        if op == "Is Not Set":
            return field.isnull() | (field == "")
        if op == "Contains":
            return field.like("%" + escape_like(frappe.utils.cstr(value)) + "%")
        if op == "Does Not Contain":
            return field.not_like("%" + escape_like(frappe.utils.cstr(value)) + "%")
        if op in ("In", "Not In"):
            items = []
            for p in (value_list or "").split(","):
                p = p.strip()
                if p:
                    items.append(cast(p))
            if not items:
                frappe.throw(frappe._("Filter value list is empty."))
            if op == "In":
                return field.isin(items)
            return field.notin(items)
        frappe.throw(frappe._("Unknown filter operator: {0}").format(op))

    def apply_match_op(left, op, right):
        if op == "=":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        frappe.throw(frappe._("Unknown match operator: {0}").format(op))

    def runtime_filter_empty(f):
        op = f.get("operator")
        if op in ("Is Set", "Is Not Set"):
            return False
        if op in ("In", "Not In"):
            return not f.get("value_list")
        if op == "Between":
            return f.get("value") in (None, "") or f.get("value_to") in (None, "")
        return f.get("value") in (None, "")

    def detect_value_gran(sample):
        # Pure-Python format detection (no `re` — RestrictedPython sandbox
        # forbids imports). Mirrors engine.filter_ops._detect_value_gran.
        s = (sample or "").strip()
        if not s:
            return ""
        if len(s) == 4 and s.isdigit():
            return "Year"
        if (
            len(s) == 7
            and s[4] == "-"
            and s[:4].isdigit()
            and s[5:].isdigit()
        ):
            return "Month"
        if (
            len(s) == 10
            and s[4] == "-"
            and s[7] == "-"
            and s[:4].isdigit()
            and s[5:7].isdigit()
            and s[8:].isdigit()
        ):
            return "Date"
        return ""

    def build_source_tables(cfg):
        base = frappe.qb.DocType(cfg["base_doctype"])
        tables = {"": (base, cfg["base_doctype"])}
        for rs in cfg.get("related_sources") or []:
            alias = rs["alias"]
            tables[alias] = (
                frappe.qb.DocType(rs["related_doctype"]).as_(related_alias(alias)),
                rs["related_doctype"],
            )
        return tables

    def source_doctypes(cfg):
        out = {"": cfg["base_doctype"]}
        for rs in cfg.get("related_sources") or []:
            out[rs["alias"]] = rs["related_doctype"]
        return out

    # Standard Frappe DB columns that aren't returned by meta.get_field but
    # exist on every doctype's tab table. Child tables additionally expose
    # `parent`, `parenttype`, `parentfield`, which are the most common
    # right-side targets in our join conditions (parent.name = child.parent).
    BUILTIN_COLUMNS = {
        "name", "parent", "parenttype", "parentfield",
        "idx", "owner", "creation", "modified", "modified_by", "docstatus",
        "_user_tags", "_comments", "_assign", "_liked_by",
    }

    def resolve_join_match_path(base_doctype, path):
        if not path:
            frappe.throw(frappe._("A field is missing."))
        segments = tuple(seg.strip() for seg in path.split(".") if seg.strip())
        if not segments:
            frappe.throw(frappe._("Empty field path."))
        if len(segments) == 1:
            meta = frappe.get_meta(base_doctype)
            fieldname = segments[0]
            if fieldname in BUILTIN_COLUMNS:
                return {
                    "path": fieldname,
                    "fieldname": fieldname,
                    "table_fieldname": "",
                    "table_doctype": "",
                }
            df = meta.get_field(fieldname)
            if not df:
                frappe.throw(frappe._("Field not found: {0}").format(fieldname))
            return {
                "path": fieldname,
                "fieldname": df.fieldname,
                "table_fieldname": "",
                "table_doctype": "",
            }
        if len(segments) != 2:
            frappe.throw(frappe._("Join fields can only use one child-table hop."))
        meta = frappe.get_meta(base_doctype)
        table_df = meta.get_field(segments[0])
        if not table_df or table_df.fieldtype not in ("Table", "Table MultiSelect") or not table_df.options:
            frappe.throw(frappe._("Field is not a child table: {0}").format(segments[0]))
        child_meta = frappe.get_meta(table_df.options)
        child_fieldname = segments[1]
        if child_fieldname not in BUILTIN_COLUMNS:
            child_df = child_meta.get_field(child_fieldname)
            if not child_df:
                frappe.throw(frappe._("Field not found: {0}").format(child_fieldname))
            child_fieldname = child_df.fieldname
        return {
            "path": segments[0] + "." + segments[1],
            "fieldname": child_fieldname,
            "table_fieldname": table_df.fieldname,
            "table_doctype": table_df.options,
        }

    def combine_clauses(parts):
        clause = None
        for part in parts:
            clause = part if clause is None else clause & part
        return clause

    def collect_paths(cfg):
        paths = set()
        def add(src, path):
            if path:
                paths.add(((src or ""), path))
        for c in cfg.get("columns") or []:
            if not c.get("calculation_alias"):
                add(c.get("source"), c.get("field_path"))
        for f in cfg.get("filters") or []:
            add(f.get("source"), f.get("field_path"))
        for g in cfg.get("group_by") or []:
            add(g.get("source"), g.get("field_path"))
        for s in cfg.get("sort") or []:
            add(s.get("source"), s.get("field_path"))
        for c in cfg.get("calculations") or []:
            for side in ("left", "right"):
                o = (c.get("expression") or {}).get(side) or {}
                if o.get("type") == "field":
                    add(o.get("source"), o.get("path"))
        return paths

    def resolve_link_joins(cfg, tables):
        joins = []
        prefix_table = {(): tables[""][0]}
        prefix_dt = {(): tables[""][1]}

        def ensure(prefix):
            if prefix in prefix_table:
                return prefix_table[prefix]
            parent_prefix = prefix[:-1]
            parent = ensure(parent_prefix)
            parent_dt = prefix_dt[parent_prefix]
            link_fname = prefix[-1]
            df = frappe.get_meta(parent_dt).get_field(link_fname)
            if not df or not df.options or df.fieldtype not in ("Link", "Table", "Table MultiSelect"):
                frappe.throw(frappe._("Cannot traverse field: {0}").format(link_fname))
            child_dt = df.options
            alias = safe_alias(prefix)
            child = frappe.qb.DocType(child_dt).as_(alias)
            if df.fieldtype in ("Table", "Table MultiSelect"):
                on_clause = (parent.name == child.parent) & (child.parenttype == parent_dt)
            else:
                on_clause = parent[link_fname] == child.name
            joins.append((parent, link_fname, child, on_clause))
            prefix_table[prefix] = child
            prefix_dt[prefix] = child_dt
            return child

        paths = collect_paths(cfg)
        for source, path in sorted(paths):
            if source:
                continue
            segments = tuple(seg for seg in path.split(".") if seg)
            for i in range(1, len(segments)):
                ensure(segments[:i])

        col_refs = {}
        for source, path in paths:
            if source:
                col_refs[(source, path)] = tables[source][0][path]
                continue
            segments = tuple(seg for seg in path.split(".") if seg)
            if len(segments) == 1:
                col_refs[("", path)] = tables[""][0][segments[0]]
            else:
                col_refs[("", path)] = prefix_table[segments[:-1]][segments[-1]]
        return (joins, col_refs)

    def resolve_join_field(current_alias, source_alias, path, tables, doctypes, join_type, col_refs, helper_cache, helper_joins):
        if (source_alias, path) in col_refs:
            return col_refs[(source_alias, path)]
        source_dt = doctypes[source_alias]
        ref = resolve_join_match_path(source_dt, path)
        root_table = tables[source_alias][0]
        if not ref.get("table_fieldname"):
            col_refs[(source_alias, path)] = root_table[ref["fieldname"]]
            return col_refs[(source_alias, path)]
        key = (source_alias, ref["table_fieldname"])
        if key not in helper_cache:
            child_table = ensure_child_pick_join(
                current_alias,
                (source_alias or "base") + "__" + ref["table_fieldname"],
                ref["table_doctype"],
                source_dt,
                root_table,
                join_type,
                helper_cache,
                helper_joins,
            )
            helper_cache[key] = child_table
        col_refs[(source_alias, path)] = helper_cache[key][ref["fieldname"]]
        return col_refs[(source_alias, path)]

    def referenced_source_paths(cfg):
        out = []
        for section in ("columns", "filters", "group_by", "sort"):
            for row in cfg.get(section) or []:
                source = row.get("source") or ""
                path = row.get("field_path") or ""
                if source and path:
                    out.append((source, path))
        for calc in cfg.get("calculations") or []:
            for side in ("left", "right"):
                operand = (calc.get("expression") or {}).get(side) or {}
                if operand.get("type") == "field" and (operand.get("source") or ""):
                    out.append((operand.get("source") or "", operand.get("path") or ""))
        return out

    def resolve_source_field_ref(source_alias, path, source_doctype, root_table, join_type, col_refs, child_helpers, helper_joins):
        if (source_alias, path) in col_refs:
            return col_refs[(source_alias, path)]
        ref = resolve_join_match_path(source_doctype, path)
        if not ref.get("table_fieldname"):
            col_refs[(source_alias, path)] = root_table[ref["fieldname"]]
            return col_refs[(source_alias, path)]
        key = ref["table_fieldname"]
        if key not in child_helpers:
            child_table = ensure_child_pick_join(
                source_alias,
                ref["table_fieldname"],
                ref["table_doctype"],
                source_doctype,
                root_table,
                join_type,
                child_helpers,
                helper_joins,
            )
            child_helpers[key] = child_table
        col_refs[(source_alias, path)] = child_helpers[key][ref["fieldname"]]
        return col_refs[(source_alias, path)]

    def ensure_child_pick_join(owner_alias, key, child_doctype, root_doctype, root_table, join_type, cache, helper_joins, filters=None):
        cache_key = (key, tuple((fieldname, op) for _, op, fieldname in (filters or [])))
        if cache_key in cache:
            return cache[cache_key]
        suffix = "pick" + str(len(filters or []))
        alias = safe_alias((owner_alias or "base", key, suffix))
        child_table = frappe.qb.DocType(child_doctype).as_(alias)
        sub = frappe.qb.DocType(child_doctype).as_(safe_alias((owner_alias or "base", key, suffix + "_sub")))
        query = (
            frappe.qb.from_(sub)
            .select(sub.name)
            .where(sub.parent == root_table.name)
            .where(sub.parenttype == root_doctype)
            .orderby(sub.idx)
            .orderby(sub.name)
            .limit(1)
        )
        for left_field, op, fieldname in filters or []:
            query = query.where(apply_match_op(left_field, op, sub[fieldname]))
        # `name IN (subquery LIMIT 1)` is equivalent to `name = (subquery)`
        # and works under safe_exec without a SubQuery wrapper: pypika's
        # ContainsCriterion.get_sql calls the container with subquery=True
        # automatically, parenthesising the inner SELECT correctly.
        # frappe.qb.terms is pypika.terms here, which doesn't expose SubQuery.
        helper_joins.append({
            "join_type": join_type,
            "table": child_table,
            "on_clause": child_table.name.isin(query),
        })
        cache[cache_key] = child_table
        return child_table

    def build_related_joins(cfg, tables, col_refs):
        doctypes = source_doctypes(cfg)
        by_source = {}
        for source, path in referenced_source_paths(cfg):
            if source not in by_source:
                by_source[source] = []
            if path not in by_source[source]:
                by_source[source].append(path)
        specs = []
        for rs in cfg.get("related_sources") or []:
            join_type = "inner" if (rs.get("join_type") or "Left Join") == "Inner Join" else "left"
            right_tbl = tables[rs["alias"]][0]
            pre_helper_joins = []
            post_helper_joins = []
            left_helpers = {}
            right_groups = {}
            post_helpers = {}
            conds = []
            for path in by_source.get(rs["alias"], []):
                resolve_source_field_ref(rs["alias"], path, rs["related_doctype"], right_tbl, join_type, col_refs, post_helpers, post_helper_joins)
            for cond in rs.get("conditions") or []:
                left_source = cond.get("left_source") or ""
                left_field = resolve_join_field(
                    rs["alias"], left_source, cond["left_path"], tables, doctypes, join_type, col_refs, left_helpers, pre_helper_joins
                )
                right_ref = resolve_join_match_path(rs["related_doctype"], cond["right_path"])
                if right_ref.get("table_fieldname"):
                    group_key = right_ref["table_fieldname"]
                    if group_key not in right_groups:
                        right_groups[group_key] = {"table_doctype": right_ref["table_doctype"], "conditions": []}
                    right_groups[group_key]["conditions"].append((left_field, cond.get("operator") or "=", right_ref["fieldname"]))
                else:
                    right_field = right_tbl[right_ref["fieldname"]]
                    col_refs[(rs["alias"], cond["right_path"])] = right_field
                    conds.append(apply_match_op(left_field, cond.get("operator") or "=", right_field))

            # Narrow child-doctype joins by parenttype so children of other
            # parent doctypes (sharing the same DB table) don't match every
            # base row through the parent linkage. Fires when the user used
            # the explicit "Join Child Table" flow (is_child_table=True) or
            # when they picked an istable=1 doctype manually and matched
            # right_path="parent". `getattr` isn't in safe_exec's builtins,
            # so we access istable through the meta's dict interface.
            related_meta = frappe.get_meta(rs["related_doctype"])
            is_table_doctype = bool(related_meta.get("istable") or 0)
            if (rs.get("is_child_table") or is_table_doctype) and rs.get("conditions"):
                parent_alias = None
                for cond in rs["conditions"]:
                    if cond.get("right_path") == "parent":
                        parent_alias = cond.get("left_source") or ""
                        break
                if parent_alias is None and rs.get("is_child_table"):
                    parent_alias = rs["conditions"][0].get("left_source") or ""
                if parent_alias is not None:
                    parent_dt = doctypes.get(parent_alias)
                    if parent_dt:
                        conds.append(right_tbl.parenttype == parent_dt)

            for table_fieldname, group in right_groups.items():
                sub_parent = frappe.qb.DocType(group["table_doctype"]).as_(safe_alias((rs["alias"], table_fieldname, "match_parent")))
                parent_query = frappe.qb.from_(sub_parent).select(sub_parent.parent).where(sub_parent.parenttype == rs["related_doctype"])
                for left_field, op, fieldname in group["conditions"]:
                    parent_query = parent_query.where(apply_match_op(left_field, op, sub_parent[fieldname]))
                # See ensure_child_pick_join for why we pass the query
                # directly instead of wrapping with SubQuery.
                conds.append(right_tbl.name.isin(parent_query))

                paths_for_table = []
                for path in by_source.get(rs["alias"], []):
                    if "." in path and path.split(".", 1)[0] == table_fieldname:
                        paths_for_table.append(path)
                if paths_for_table:
                    helper_table = ensure_child_pick_join(
                        rs["alias"],
                        table_fieldname,
                        group["table_doctype"],
                        rs["related_doctype"],
                        right_tbl,
                        join_type,
                        post_helpers,
                        post_helper_joins,
                        group["conditions"],
                    )
                    for path in paths_for_table:
                        path_ref = resolve_join_match_path(rs["related_doctype"], path)
                        col_refs[(rs["alias"], path)] = helper_table[path_ref["fieldname"]]

            specs.append({
                "join_type": join_type,
                "right_table": right_tbl,
                "pre_helper_joins": pre_helper_joins,
                "conditions": conds,
                "post_helper_joins": post_helper_joins,
            })
        return specs

    def build_query(cfg, count_only=False):
        tables = build_source_tables(cfg)
        base = tables[""][0]
        rl = resolve_link_joins(cfg, tables)
        joins = rl[0]
        col_refs = rl[1]
        related_joins = build_related_joins(cfg, tables, col_refs)

        q = frappe.qb.from_(base)
        for j in joins:
            q = q.left_join(j[2]).on(j[3])
        for spec in related_joins:
            for helper in spec["pre_helper_joins"]:
                if helper["join_type"] == "inner":
                    q = q.inner_join(helper["table"]).on(helper["on_clause"])
                else:
                    q = q.left_join(helper["table"]).on(helper["on_clause"])
            on_clause = combine_clauses(spec["conditions"])
            if on_clause is None:
                continue
            if spec["join_type"] == "inner":
                q = q.inner_join(spec["right_table"]).on(on_clause)
            else:
                q = q.left_join(spec["right_table"]).on(on_clause)
            for helper in spec["post_helper_joins"]:
                if helper["join_type"] == "inner":
                    q = q.inner_join(helper["table"]).on(helper["on_clause"])
                else:
                    q = q.left_join(helper["table"]).on(helper["on_clause"])
        for f in cfg.get("filters") or []:
            # Runtime filter with no effective value → skip (otherwise an
            # empty default would compare to "" and remove every row).
            if f.get("is_runtime") and runtime_filter_empty(f):
                continue
            field = col_refs[((f.get("source") or ""), f["field_path"])]
            f_ftype = f.get("fieldtype") or "Data"
            f_gran = f.get("granularity") or ""
            # "All" granularity = single filter row that accepts YYYY,
            # YYYY-MM, or YYYY-MM-DD. Detect from the user value, then
            # the field wrap and the value cast both use the resolved gran.
            # If the value is empty or doesn't match any of the three shapes,
            # silently skip the filter — the Studio UI shows the expected
            # format under the input box, no popup needed.
            if f_gran == "All" and f_ftype in ("Date", "Datetime"):
                sample = f.get("value") or f.get("value_to") or ""
                if not sample and f.get("value_list"):
                    parts_list = (f.get("value_list") or "").split(",")
                    sample = parts_list[0].strip() if parts_list else ""
                f_gran = detect_value_gran(sample)
                if f["operator"] not in ("Is Set", "Is Not Set") and not f_gran:
                    continue
            # Wrap the field with date_bucket when the user has set a
            # granularity. Contains/Does Not Contain bypass bucketing —
            # they're substring checks against the raw field.
            if f_gran and f_ftype in ("Date", "Datetime") and f["operator"] not in ("Contains", "Does Not Contain"):
                field = date_bucket(field, f_gran)
            q = q.where(filter_predicate(field, f["operator"], f.get("value"),
                f.get("value_to"), f.get("value_list"), f_ftype, f_gran))
        if cfg.get("group_by"):
            for g in cfg["group_by"]:
                field = col_refs[((g.get("source") or ""), g["field_path"])]
                q = q.groupby(date_bucket(field, g.get("granularity") or ""))

        if count_only:
            if cfg.get("group_by"):
                inner = q.select(frappe.qb.terms.LiteralValue("1"))
                CountStar = frappe.qb.terms.CustomFunction("COUNT", ["x"])
                return frappe.qb.from_(inner.as_("rs_sub")).select(CountStar(frappe.qb.terms.LiteralValue("*")))
            return q.select(frappe.qb.terms.CustomFunction("COUNT", ["x"])(frappe.qb.terms.LiteralValue("*")))

        group_buckets = {}
        for g in cfg.get("group_by") or []:
            group_buckets[((g.get("source") or ""), g["field_path"])] = g.get("granularity") or ""
        real_cols = []
        for c in cfg.get("columns") or []:
            if not c.get("calculation_alias"):
                real_cols.append(c)
        selects = []
        hidden_keys = []
        included = set()
        for c in real_cols:
            key = ((c.get("source") or ""), c["field_path"])
            included.add(key)
            field = col_refs[key]
            if c.get("aggregate"):
                fn = aggregate_fn(c["aggregate"])
                selects.append(fn(field))
            elif key in group_buckets and group_buckets[key]:
                selects.append(date_bucket(field, group_buckets[key]))
            else:
                selects.append(field)
        group_keys = set()
        for g in cfg.get("group_by") or []:
            group_keys.add(((g.get("source") or ""), g["field_path"]))
        for c in cfg.get("calculations") or []:
            for side in ("left", "right"):
                o = (c.get("expression") or {}).get(side) or {}
                if o.get("type") != "field":
                    continue
                key = ((o.get("source") or ""), (o.get("path") or ""))
                if key in included or key not in col_refs:
                    continue
                field = col_refs[key]
                if cfg.get("group_by") and key not in group_keys:
                    fn = aggregate_fn("Sum")
                    selects.append(fn(field))
                else:
                    selects.append(field)
                included.add(key)
                hidden_keys.append(key)
        if not selects:
            for g in cfg.get("group_by") or []:
                field = col_refs[((g.get("source") or ""), g["field_path"])]
                selects.append(date_bucket(field, g.get("granularity") or ""))
        for sel in selects:
            q = q.select(sel)
        for s in cfg.get("sort") or []:
            field = col_refs[((s.get("source") or ""), s["field_path"])]
            if s.get("direction") == "Ascending":
                order = frappe.qb.terms.Order.asc
            else:
                order = frappe.qb.terms.Order.desc
            q = q.orderby(field, order=order)
        return (q, hidden_keys, real_cols)

    def eval_calc(expr, lookup):
        if not expr:
            return None
        op = expr.get("op")
        def operand(o):
            if not o:
                return None
            if o.get("type") == "const":
                return o.get("value")
            return lookup.get(((o.get("source") or ""), (o.get("path") or "")))
        l = operand(expr.get("left"))
        r = operand(expr.get("right"))
        if l is None or r is None:
            return None
        l = frappe.utils.flt(l)
        r = frappe.utils.flt(r)
        if op == "+":
            return l + r
        if op == "-":
            return l - r
        if op == "*":
            return l * r
        if op == "/":
            if r == 0:
                return None
            return l / r
        return None

    def aggregate_values(values, name):
        if not values:
            return 0
        if name == "Sum":
            return sum(values)
        if name == "Avg":
            return sum(values) / len(values)
        if name == "Min":
            return min(values)
        if name == "Max":
            return max(values)
        if name == "Count":
            return len(values)
        return sum(values)

    def column_fname(c):
        if c.get("calculation_alias"):
            return "calc__" + c["calculation_alias"]
        src = c.get("source") or ""
        path = (c.get("field_path") or "").replace(".", "__")
        if src:
            return src + "__" + path
        return path

    def column_meta(cfg):
        out = []
        calc_index = {}
        for k in cfg.get("calculations") or []:
            calc_index[k["alias"]] = k
        for c in cfg.get("columns") or []:
            if c.get("calculation_alias"):
                calc = calc_index.get(c["calculation_alias"])
                ftype_map = {
                    "Currency": "Currency",
                    "Percent": "Percent",
                    "Integer": "Int",
                    "Number": "Float",
                }
                fmt = (calc or {}).get("format_type") or "Number"
                ftype = ftype_map.get(fmt, "Float")
                if calc and (calc.get("label") or calc.get("alias")):
                    fallback_label = calc.get("label") or calc["alias"]
                else:
                    fallback_label = c["calculation_alias"]
                label = c.get("label") or fallback_label
                out.append({
                    "fieldname": column_fname(c),
                    "label": label,
                    "fieldtype": ftype,
                    "options": "",
                    "width": c.get("width") or 160,
                })
                continue
            ftype = c.get("fieldtype") or "Data"
            if c.get("aggregate") == "Count":
                ftype = "Int"
            elif c.get("aggregate") in ("Sum", "Avg") and ftype != "Currency":
                ftype = "Float"
            options = c.get("link_doctype") or c.get("options") or ""
            # Frappe's query_report runner walks Link columns to enforce
            # permissions on the linked doctype (get_user_match_filters →
            # get_meta(options)). If options is empty it raises
            # `DocType  not found`. Studio doesn't track the link target on
            # the column row, so degrade to Data when we can't name it.
            if ftype in ("Link", "Dynamic Link") and not options:
                ftype = "Data"
            out.append({
                "fieldname": column_fname(c),
                "label": c.get("label") or c.get("field_path") or "",
                "fieldtype": ftype,
                "options": options,
                "width": c.get("width") or 160,
            })
        return out

    def shape_rows(cfg, raw_rows, real_cols, hidden_keys):
        calc_index = {}
        for c in cfg.get("calculations") or []:
            calc_index[c["alias"]] = c
        real_count = len(real_cols)
        out = []
        keys_in_order = []
        for c in real_cols:
            keys_in_order.append(((c.get("source") or ""), c["field_path"]))
        for k in hidden_keys:
            keys_in_order.append(k)
        for raw in raw_rows:
            raw = list(raw)
            lookup = {}
            i = 0
            while i < len(keys_in_order) and i < len(raw):
                lookup[keys_in_order[i]] = raw[i]
                i = i + 1
            real_values = raw[:real_count]
            row_dict = {}
            ri_idx = 0
            for c in cfg.get("columns") or []:
                if c.get("calculation_alias"):
                    calc = calc_index.get(c["calculation_alias"])
                    expr = (calc or {}).get("expression")
                    row_dict[column_fname(c)] = eval_calc(expr, lookup)
                else:
                    if ri_idx < len(real_values):
                        row_dict[column_fname(c)] = real_values[ri_idx]
                    else:
                        row_dict[column_fname(c)] = None
                    ri_idx = ri_idx + 1
            out.append(row_dict)
        return out

    built = build_query(CONFIG)
    data_q = built[0]
    hidden_keys = built[1]
    real_cols = built[2]
    raw_rows = data_q.limit(PAGE_SIZE_LIMIT).run(as_dict=False) or []
    columns = column_meta(CONFIG)
    rows = shape_rows(CONFIG, raw_rows, real_cols, hidden_keys)
    return (columns, rows, None, None, None)


apply_runtime_filters(CONFIG, filters if isinstance(filters, dict) else None)
data = run(CONFIG)
'''
