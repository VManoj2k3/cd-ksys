# Regular package marker. Without this, `tests` is a namespace package and
# ANY site-packages that ships a real top-level `tests` package shadows it
# (regular packages beat namespace portions regardless of sys.path order) —
# exactly what happened on Kaggle's preinstalled Python.
