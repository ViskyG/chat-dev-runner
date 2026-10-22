"""Fresh disposable SQLite database in this request's bounded quota mount."""
import sqlite3

with sqlite3.connect("/data/fixture.sqlite") as database:
    database.execute("CREATE TABLE example (answer INTEGER NOT NULL)")
    database.execute("INSERT INTO example VALUES (42)")
print("migration complete", flush=True)
