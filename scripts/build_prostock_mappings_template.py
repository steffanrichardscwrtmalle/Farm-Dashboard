from pathlib import Path
import re

src = Path("templates/mappings.html").read_text(encoding="utf-8")
text = src
replacements = [
    ("Category &amp; farm descriptions", "Groups &amp; products"),
    ("Categories (<span", "Groups (<span"),
    ("Farm descriptions (<span", "Products (<span"),
    ("New category", "New group"),
    ("New farm description", "New product"),
    ("Search categories", "Search groups"),
    ("Search farm descriptions", "Search products"),
    ("<th>Category</th>", "<th>Group</th>"),
    ("<th>Farm description</th>", "<th>Product</th>"),
    ("<th>Farm</th>", "<th>Product</th>"),
    ("Category and Farm Description", "Group and Product"),
    ("product descriptions that did not match", "drug names that did not match"),
    ("Product description", "Drug name"),
    ("unknown products", "unknown drugs"),
    ("No unknown products", "No unknown drugs"),
    ("Select category", "Select group"),
    ("Select farm description", "Select product"),
    ("category and farm description", "group and product"),
    ("Category is required", "Group is required"),
    ("Farm description is required", "Product is required"),
    ("applied mappings to", "applied mappings to"),
    ("/api/mapping-options", "/api/prostock/mapping-options"),
    ("/api/mappings", "/api/prostock/mappings"),
    ("unknown-products", "unknown-drugs"),
]
for old, new in replacements:
    text = text.replace(old, new)
text = re.sub(
    r'<label style="margin:0;font-weight:500"><input type="file" id="import-file"[^<]*</label>\s*',
    "",
    text,
)
text = re.sub(
    r"document\.getElementById\(\"import-file\"\)\.addEventListener\([\s\S]*?\}\);\s*",
    "",
    text,
)
Path("templates/prostock/mappings.html").write_text(text, encoding="utf-8")
print("written prostock mappings template")
