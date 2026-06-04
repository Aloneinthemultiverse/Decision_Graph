# QUESTION

I want to add a new dependency-injection mechanism that takes priority over the existing Depends() — what do I need to know before touching the dependency-resolution code in fastapi/dependencies/utils.py?

## RUN 1 — vanilla

Before modifying the dependency-resolution engine in `fastapi/dependencies/utils.py` to add a high-priority dependency injection (DI) mechanism, you must carefully navigate FastAPI's established static analysis and execution patterns. 

Here is what you need to know, structured across risks, prior decisions, target files, and merge strategy:

### **Risks**
1. **Scope and Lifetime Violations:** FastAPI strictly validates dependency scopes at startup (e.g., raising `DependencyScopeError` if a `"request"`-scoped dependency attempts to depend on a `"function"`-scoped one). Your new mechanism must integrate seamlessly with `Dependant.scope` and `computed_scope` check logic to avoid runtime errors or memory leaks.
2. **Bypassing the Cache Engine:** FastAPI leverages a caching system based on `DependencyCacheKey` to prevent redundant execution of dependencies within the same request. If your mechanism overrides `Depends()` but does not properly register cache keys, it could cause double-execution of heavy dependencies or state corruption.
3. **AST / Tree Resolution Corruption:** Route dependencies are parsed *once at startup* (generating a static tree of `Dependant` objects). If your mechanism relies on dynamic runtime checks that modify this structure post-startup, it will break OpenAPI schema generation and static parameter validation.
4. **Annotated Order and Priority Collisions:** In `analyze_param()`, FastAPI scans `Annotated` arguments in a specific order, picking the last relevant metadata object. Injecting a new mechanism requires careful integration here to ensure it intercepts parameter extraction before `Depends` or `FieldInfo` processes them.

### **Prior Decisions & Conventions**
5. **Annotated vs. Default Value Dual-Path:** FastAPI supports both `def endpoint(x: T = MyDI())` and `def endpoint(x: Annotated[T, MyDI()])`. Your DI mechanism must support both patterns by handling them in both the `Annotated` extraction logic and the default-value check branch of `analyze_param()`.
6. **Strict Exclusivity Guardrails:** The codebase enforces strict exclusivity (e.g., asserting that you cannot define both `Depends` in `Annotated` and as a default value simultaneously). You must maintain these assertions so developers do not accidentally configure conflicting DI sources.
7. **Special-Cased System Types:** Certain types (like `Request`, `WebSocket`, `HTTPConnection`, `Response`, `StarletteBackgroundTasks`, and `SecurityScopes`) are automatically injected by `add_non_field_param_to_dependency` without needing `Depends()`. Your mechanism must decide whether it explicitly overrides these system-injected types or defers to them.

### **Files to Update**
8. **`/fastapi/params.py`:** Create your new injection marker class (e.g., class `PrioritizedDepends` inheriting from or mimicking `Depends`) to define the user-facing API.
9. **`/fastapi/dependencies/models.py`:** Update the `Dependant` model class to store metadata, scope info, and caching behavior specific to your prioritized DI mechanism.
10. **`/fastapi/dependencies/utils.py`:** 
    * Update `analyze_param()` to detect and prioritize your marker over standard `Depends`.
    * Update `get_dependant()` to construct the dependency hierarchy with your new logic.
    * Modify the runtime dependency solver (e.g., `solve_dependencies()`) to handle the resolution priority during request processing.

### **Merge-Order Guidance**
11. **Phase 1 (Models & Markers):** Implement and merge the declarative schema changes in `/fastapi/params.py` and `/fastapi/dependencies/models.py` first. This establishes the structural contract without modifying existing resolution pipelines.
12. **Phase 2 (Resolution & Integration):** Update `/fastapi/dependencies/utils.py` to parse and resolve the new injection mechanism. Accompany this with robust unit tests validating signature parsing, parameter extraction precedence, and execution caching before exposing it to high-level route logic.

## RUN 2 — with-graph

Before making modifications to the dependency-injection resolution system in `fastapi/dependencies/utils.py`, consider the following key points:

*   **Override Position in `analyze_param`**: Your new priority injection mechanism must be evaluated early inside `analyze_param`. It should intercept parameter annotations (such as `Annotated`) or defaults before the logic falls back to `Depends` or `FieldInfo` extraction.
*   **OpenAPI Extraction and Documentation Impact**: The `blast_radius_files` signal lists **4** affected files, including `fastapi/openapi/utils.py` and `docs_src/extending_openapi/tutorial001_py310.py`. This indicates that any change to dependency resolution will directly impact how OpenAPI generation extracts parameters, requiring corresponding updates in the OpenAPI builder.
*   **Legacy Parameter & API Stability**: The `rationales_in_target_file` signal identifies a `TODO` at line 607: *"remove this parameter later, no longer used, not removing it yet as some"*. This reflects a strict project policy of preserving backwards-compatible signatures in `utils.py` even when internal parameters are technically obsolete.
*   **Safe Type Checking via God Nodes**: The `god_nodes_top5` list includes `lenient_issubclass` (with 28 callers). When checking the type annotations of your new dependency mechanism, you must use `lenient_issubclass` rather than standard Python `issubclass` to ensure compatibility with complex generic types and different Pydantic versions.
*   **Recursive Dependency Execution**: The `callers_of_solve_dependencies` signal points to `solve_dependencies` being internally recurrent within `fastapi/dependencies/utils.py`. Your new mechanism must yield a compatible structure (like a specialized subclass of `Dependant`) so that the execution engine can correctly resolve it down the line.
*   **PR Merge Risk Guidance**: The `pr_triage` signal currently ranks risk as `LOW` (risk score **12.8**) with a `merge-first` order hint because no major system chokepoints are touched. However, altering the base contract of `Depends()` is highly sensitive; you must ensure your changes are fully isolated behind your new custom marker to preserve this low-risk profile.
*   **Scope Resolution Warnings**: `get_dependant` enforces strict scope boundaries (e.g. throwing `DependencyScopeError` if a `"request"` scope dependency relies on a `"function"` scope dependency). Your new mechanism must explicitly define its default lifecycle scope and validate nested dependencies accordingly.
*   **Special Request/Response Type Safeguards**: In `analyze_param`, special types such as `Request`, `WebSocket`, `HTTPConnection`, `Response`, `StarletteBackgroundTasks`, and `SecurityScopes` bypass regular fields. Your priority injector must decide whether to support or disallow co-existence with these auto-injected system types.
*   **Pydantic Compatibility Warnings**: As shown in `all_fastapi_warning_rationales`, there are multiple active `TODOs` in `_compat/v2.py` regarding Pydantic v1 vs. v2 support. Ensure your dependency parser handles both raw types and Pydantic model fields without breaking compatibility across Pydantic versions.
*   **Multipart and Form Parsing Integration**: The `blast_radius_sample` includes `tests/test_multipart_installation.py`. If your new dependency mechanism intercepts form/file parameters, verify that it does not inadvertently bypass `ensure_multipart_is_installed` checks.
*   **Comprehensive Testing Bounds**: Since `tests/test_dependencies_utils.py` is identified in the `blast_radius_sample`, you will need to add detailed test coverage proving that the new mechanism successfully preempts `Depends()` and standard `Body`/`Query` parameters across different declaration methods (e.g., standard defaults vs `Annotated` syntax).
