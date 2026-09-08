"""
P4 dashboard — a static HTML report of a Bob the Tester run.

`build.py` reads the database READ-ONLY and writes one self-contained HTML
file; `render.py` turns rows into markup; `serve.py` serves the same page
over HTTP with auto-refresh while a run is in progress.  Nothing here
imports the tool layer and nothing here writes, so opening the dashboard
can never disturb a pipeline run.
"""
