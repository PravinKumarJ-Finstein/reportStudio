### Report Builder

A Frappe app that lets users design, run, and share reports **entirely through a UI** — selecting
columns, applying filters, grouping, sorting, joining related/child-table sources, and adding
calculated fields — **without writing any code**.

The app ships a desk page called **Report Studio** (route: `/app/report-studio`) where reports
are built visually, previewed live, exported (XLSX / CSV / PDF), and optionally published as
native Frappe Query Reports.

---

## Table of contents

- [Requirements](#requirements)
- [Roles & permissions](#roles--permissions)
- [Using Report Studio](#using-report-studio)
- [Exporting](#exporting)
- [Sharing & publishing](#sharing--publishing)
- [How it works](#how-it-works)
- [Limits & safety](#limits--safety)

---

## Requirements

- Frappe Framework **v15**
- Python **3.10+**

---

## Roles & permissions

The app installs a role called **Report Studio User** (shipped as a fixture).

| Role                  | Can access Report Studio | Sees reports                                        |
| --------------------- | ------------------------ | --------------------------------------------------- |
| `Administrator`       | Yes                      | All reports                                         |
| `System Manager`      | Yes                      | All reports                                         |
| `Report Studio User`  | Yes                      | Own reports, public reports, and reports shared with them |

To let a user build reports, assign them the **Report Studio User** role (or System Manager).
Importantly, the app **never bypasses Frappe permissions** — a user can only build reports on
DocTypes they already have `read` access to, and row-level permission query conditions of the
underlying DocTypes are still respected.

After enabling the app, open Report Studio at `/app/report-studio` on your site. It also
appears as a **Report Studio** tile on the Apps screen.

---

## Using Report Studio

Report Studio is a drag-and-drop builder. A typical flow:

1. **Pick a data source.** Choose the base DocType the report runs on (only DocTypes you can
   read are listed).

2. **Add related / child sources (optional).** Join in other DocTypes:
   - **Related sources** — link another DocType via Link-field join conditions (`Left Join` or
     `Inner Join`).
   - **Child tables** — pull in rows from a child table of the base DocType.
   Joins can go up to a depth of **2**.

3. **Choose columns.** Drag fields from any source into the columns area. Each column can have
   a custom label, width, and an optional **aggregate** (`Count`, `Sum`, `Avg`, `Min`, `Max`).

4. **Add calculated fields (optional).** Build derived columns from arithmetic expressions
   (`+`, `-`, `*`, `/`) over other fields, formatted as Number, Integer, Currency, or Percent.

5. **Filter.** Add filter rows with operators like `=`, `!=`, `>`, `in`, `like`, `between`,
   `is set`, date-range granularities, etc. Filters can be marked as **runtime filters** so
   they become prompts when the report is run.

6. **Group & sort.** Group by one or more fields (with date granularity — Day/Week/Month/
   Quarter/Year) and define multi-level sort order (Ascending/Descending).

7. **Preview.** The result table updates live, paginated. Page size is configurable.

8. **Save.** Saving creates a **Report Studio Report** document, with a title, description,
   and visibility setting.

---

## Exporting

From a previewed or saved report you can export to:

- **XLSX**
- **CSV**
- **PDF**

Exports run the full query (not just the current page) up to a cap of **10,000 rows**.

---

## Sharing & publishing

Each report has a **Visibility** setting:

- **Private** — only the owner (and System Managers) can see it.
- **Public** — any Report Studio user can see it.
- **Shared with Roles** — visible to users holding the selected roles.

Reports can also be **published as a standard Frappe Query Report**. Publishing generates a
native report (visible in the desk Report list and report views) that delegates execution back
to the Report Studio engine, including runtime filters. Publishing can be reversed with
**unpublish**.

---

## How it works

```
Report Studio page (JS UI)
        │  builds a JSON "config"
        ▼
report_builder.api.*   ──  whitelisted endpoints
  builder.py   → preview / save / load / list / delete / publish
  export.py    → export_report (xlsx/csv/pdf)
  metadata.py  → DocType & field discovery for the UI
  permission.py→ role checks + row-level query conditions
        │
        ▼
report_builder.engine  ──  the query engine
  schema.py        → validates & normalises the config
  meta_validator.py→ verifies DocTypes/fields exist & are readable
  join_resolver.py → resolves related/child-table joins
  filter_ops.py    → translates filters into SQL conditions
  aggregations.py  → applies Count/Sum/Avg/Min/Max + group-by
  pagination.py    → page / page-size handling
  query_engine.py  → assembles it all via Frappe's query builder
        ▼
        Frappe / MariaDB
```

Key points:

- **The UI produces a JSON config**, not SQL. The browser never sends raw queries.
- **The engine builds queries with Frappe's query builder (pypika)** — fields, DocTypes, joins,
  and operators are all validated against DocType metadata before a query is assembled, so the
  surface for SQL injection is closed.
- **Permissions are enforced server-side.** Every DocType touched is checked for `read`
  permission, and `permission_query_conditions` are applied so users only see rows they are
  allowed to see.
- **Data model.** A report is stored as a `Report Studio Report` DocType with child tables:
  `Report Studio Related Source`, `Report Studio Column`, `Report Studio Calculation`,
  `Report Studio Filter`, `Report Studio Group By`, `Report Studio Sort`, and
  `Report Studio Share`.
- **Published reports** use an inline runner (`report_builder.runtime.inline_runner`) so the
  generated standard report stays in sync with the Report Studio definition.

---

### License

mit
