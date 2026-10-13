# Best Practices for BAQs and SQL to BAQ Conversion

> **Key Takeaways**
> * **Modularity:** Separate data gathering from logic for complex queries.
> * **Uniqueness:** Every alias must be globally unique to satisfy the Kinetic compiler.
> * **Type Safety:** Explicitly cast ambiguous and recursive columns to prevent precision loss or type mismatches.
> * **Integrity:** Use numeric literals for bit fields and explicit joins for data relationships.
> * **Validation:** Implement robust error prevention through validation checks.
> 
> 

---

## 1. Structure and Organization

### CTE Architecture

* **Separation of Concerns:** Build logic in distinct stages:
* **Base Discovery:** Primary table joins and initial filtering.
* **Recursive Processing:** Hierarchical traversal or iterative calculations.
* **Final Output:** Formatting, leaf-level filtering, and display logic.


* **Logical Progression:** Ensure data flows predictably from raw tables to complex calculations.
* **Explicit Aliasing:** Always alias CTE references immediately to prevent ambiguity.
* **Standard:** `FROM Recursive_Member AS [RM]`


* **Union All:** Recursive members must use `UNION ALL` (Standard `UNION` is not permitted in recursion).
* **MaxRecursion 100:** SQL Server and Kinetic limit recursion to 100 levels. Early filtering is your primary defense against crashing and incomplete data.
* **Limits:** Avoid `TOP` clauses within modular CTEs; apply limits in the Presentation layer.
* **Syntax Hygiene:** Enclose all references in square brackets `[]` to avoid conflicts with reserved words.

### Query Organization

Section complex queries into functional areas e.g.:

1. **Data Source (The "What"):** Define the clean, filtered dataset.
2. **Transformation (The "How"):** Apply recursion or complex logic.
3. **Presentation (The "Final"):** Select columns for the UI; apply sorting and final filtering.

### Commenting and Internal Documentation

* **`/* block comments */`** ensure internal documentation does not interfere with the execution string.

---

## 2. Naming Conventions

### Alias Scope and Uniqueness

* **Global Uniqueness:** Kinetic flattens queries; an alias used in a CTE or subquery cannot be reused elsewhere in the query.
* **Functional Aliasing:** Name aliases based on their origin and role (e.g., `[Source_Record]`, `[Parent_Context]`).
* **Recursive Referencing:** When a CTE joins to itself, use a distinct alias for the "results-so-far" (e.g., `FROM [CTE_Name] AS [Prior_Level]`).

### Standardized Naming

* **Origin Prefixes:** Use `[TableName]_[ColumnName]` for source data (e.g., `[Part_PartNum]`).
* **Calculated Fields:** Use the `Calculated_` prefix for any expression-generated column (e.g., `[Calculated_Path]`).
* **Format:** Avoid special characters; use underscores instead of spaces or hyphens.

---

## 3. Performance & Data Integrity

### Literal Values

* **Numeric Bits:** Use `1` and `0` for Boolean/Bit fields. Kinetic may fail to parse `True`/`False` literals.
* **Standard:** `WHERE Table.InActive = 0`



### Precision Management

* **Recursion Casting:** To prevent "Rounding Drift" or "Type Mismatches" in `UNION ALL` statements, cast decimals and strings to their maximum required size in the Anchor member.
* **Standard:** `CAST(Field AS DECIMAL(18,5))` or `CAST(Field AS NVARCHAR(MAX))`



### Completeness

* Ensure every column has an explicit table prefix or alias.
* CTE, Union, 'Union All', & Except tables must maintain the same column number, type, and order between all members.

### Optimization

* **Early Filtering:** Apply `WHERE` clauses to tables and subqueries at the earliest opportunity to reduce the computational payload.
* **Join Integrity:**
* Use **INNER JOIN** for mandatory relationships.
* Use **LEFT JOIN** for optional attributes or to identify "Leaf" nodes (where the right side `IS NULL`).



---

## 4. Troubleshooting

| Error | Cause | Resolution |
| --- | --- | --- |
| **Invalid Column Name** | Referencing a source table alias inside the Recursive Member. | Reference the **Anchor's output column names** when pulling from the CTE alias. |
| **Union Type Mismatch** | Anchor and Recursive columns have different lengths or types. | Use explicit `CAST` to ensure both halves of the `UNION` are identical. |
| **Infinite Loop** | Missing join criteria or circular data references. | Ensure join and `WHERE` clauses provide a clear "termination" point for recursion. |
| **Ambiguous Column** | Using a column name that exists in multiple tables without a prefix. | **Always** use the alias prefix (e.g., `[Source_Record].[Company]`). |

---

## 5. Helpful Kinetic Functions & String Handling

When building delimited strings, leverage built-in `Ice` functions to parse for a clean final presentation:

* **Count Entries:** `[Ice].num_entries([Calculated_Column], '~')`
* **Extract Entry:** `[Ice].entry([Index], [Calculated_Column], '~')`

---
