"""Bob the Tester quality pipeline tools package — P2 owns.

One module per pipeline stage.  Stages never import each other
(AGENTS.md §6); shared helpers are intentionally duplicated so a stage can
be cut or replaced without touching its siblings.
"""
