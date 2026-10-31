"""Scope node source queries before pagination to the center's current owners."""


def scope_query(query, mapping):
    # Explicit shop IDs have already been checked and translated by the caller.
    if any(key == "shop_id" and value for key, value in query):
        return query
    return [(key, value) for key, value in query if key != "shop_id"] + [
        ("shop_id", ",".join(str(value) for value in sorted(set(mapping.values()))))
    ]


def empty_sources():
    from ..source_pages import source_page

    return source_page({"sources": []}, snapshot=True)
