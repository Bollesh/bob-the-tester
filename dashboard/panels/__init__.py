"""
P4 dashboard panels — one module per panel.

Every panel exposes a single `render(...)` function taking already-fetched
rows.  Panels never open a database connection themselves: app.py does all
the reading (read-only) and passes the data down.  That keeps each panel
trivially testable and guarantees one consistent snapshot per page render.
"""
