# Different import styles to exercise scope resolution
from subdir.helpers import fetch_data, parse_response
from subdir.helpers import Database as DB
from subdir import helpers as h
import subdir.helpers


def case_a():
    # `from X import Y` — bare name
    return fetch_data()


def case_b():
    # aliased import — `Y as Z`
    db = DB()
    return db.query("SELECT 1")


def case_c():
    # module-aliased — `import X as h`, called h.foo()
    return h.parse_response(42)


def case_d():
    # qualified — `subdir.helpers.fetch_data()`
    return subdir.helpers.fetch_data()
