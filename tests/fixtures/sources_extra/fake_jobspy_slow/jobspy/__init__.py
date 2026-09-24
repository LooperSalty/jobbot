"""Fake python-jobspy that hangs (like the Indeed page-2 bug, JobSpy issue #385)."""
import time


def scrape_jobs(**kwargs):
    time.sleep(30)
