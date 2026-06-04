# Relative import
from .subdir.helpers import fetch_data


def case_relative():
    return fetch_data()
