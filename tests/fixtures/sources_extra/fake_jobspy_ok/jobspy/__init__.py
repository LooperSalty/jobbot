"""Fake python-jobspy used by the worker subprocess test (no network)."""
import json


class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def to_json(self, **kwargs):
        return json.dumps(self.rows)


def scrape_jobs(**kwargs):
    if kwargs.get("search_term") == "boom":
        raise ValueError("cassé")
    return _Frame([{"id": "in-x", "title": kwargs["search_term"]}])
