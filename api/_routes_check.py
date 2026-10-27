from app_factory import create_app
app = create_app()
for r in app.url_map.iter_rules():
    if "async-tasks" in str(r):
        print(r, sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"}))
