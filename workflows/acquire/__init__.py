"""The ``acquire`` stage (§3.1, §5.4-§5.6; D5, D32): ``documents`` rows → snapshots, roles and the
text pointer of the ``content`` block, written as a ``documents`` part file.

Modules: :mod:`.main` (the stage: scope, substages, part writer), :mod:`.scope` (D5's scope and
due-ness), :mod:`.carry` (regroup by member, wanted roles, gate), :mod:`.inband` (in-band
snapshots, C20), :mod:`.point` (the §5.5 pointer), :mod:`.lanes` (the lane protocol and
registry), :mod:`.history` and :mod:`.convert` (seams filled by T3.3 / T3.5).

The runner loads ``main.py`` by path; the siblings are imported as the package
``workflows.acquire`` (the repository root is on ``sys.path`` through the editable install).
"""
