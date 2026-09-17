import os

# Must run before anything imports app.config/app.db - those read
# DATABASE_URL at module-import time, so setting this after the fact
# wouldn't retroactively redirect an already-created engine. setdefault
# (not a hard override) so DATABASE_URL set explicitly in the environment
# - e.g. by CI, or a developer who genuinely wants something else - still
# wins; this only supplies the default when nothing else is set.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://idp:idp@localhost:5432/idp_pipeline_test",
)
