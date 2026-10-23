"""Actual unittest results, original JUnit and a finite application output."""
import json
import sqlite3
import sys
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "pass"
Path("/out/partial.json").write_text(json.dumps({"started": True, "mode": mode}))
if mode in {"timeout", "cancel"}:
    print("client waiting; partial output already retained", flush=True)
    time.sleep(120)


class Integration(unittest.TestCase):
    def test_service_health_and_answer(self):
        with urllib.request.urlopen("http://api:8080/", timeout=2) as response:
            self.assertEqual(response.read(), b"42\n")

    def test_database_migration(self):
        with sqlite3.connect("/data/fixture.sqlite") as database:
            self.assertEqual(database.execute("SELECT answer FROM example").fetchone()[0],
                             99 if mode == "fail" else 42)


result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Integration))
suite = ET.Element("testsuite", tests=str(result.testsRun), failures=str(len(result.failures)),
                   errors=str(len(result.errors)), skipped=str(len(result.skipped)))
failures = {case.id(): trace for case, trace in result.failures}
errors = {case.id(): trace for case, trace in result.errors}
for name in ("test_service_health_and_answer", "test_database_migration"):
    identifier = "__main__.Integration." + name
    case = ET.SubElement(suite, "testcase", classname="compose.Integration", name=name)
    if identifier in failures:
        ET.SubElement(case, "failure").text = failures[identifier]
    if identifier in errors:
        ET.SubElement(case, "error").text = errors[identifier]
ET.ElementTree(suite).write("/out/junit.xml", encoding="utf-8", xml_declaration=True)
Path("/out/result.json").write_text(json.dumps({"answer": 42, "successful": result.wasSuccessful()}))
raise SystemExit(0 if result.wasSuccessful() else 1)
