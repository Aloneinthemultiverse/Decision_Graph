from decisiongraph import code_graph as cg
db = r"storage\workspaces\p4jIEgrAJd33s3qH-DZbhQ\personal\code_graph.db"
all_pred = set()
for f in ["docs/patterns/streaming.rst","docs/templating.rst",
          "src/flask/ctx.py","src/flask/helpers.py"]:
    r = cg.blast_radius(db, f, max_depth=1, strict=True)
    print(f"{f}: total={r['total_affected']} rev={len(r['reverse_imports'])}")
    all_pred.update(r["affected_files"])
    all_pred.add(f)
print("UNION:", len(all_pred))
