"""A built-in fake SaaS to run the whole pipeline against: login, paginated JSON APIs, details,
pictures and the usual traps, with nothing real behind it.

It exists so that anyone can try, demo and test the tool with no account, no credential and no
legal question. ``catalog-sync mock serve`` starts it; ``catalog_sync.plugins.mock`` is the
connector that reads it. It lives in the core package (not in ``examples/``) so that it is there
after ``pip install``, and it uses nothing outside the standard library and Pillow.
"""
