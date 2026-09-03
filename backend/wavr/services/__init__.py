"""Domain services: the work `app.py` used to do inline.

`app.py` is a wiring file that grew a body. Everything here is logic that
coordinates several stores and has nothing to do with HTTP — the kind of thing
that was only reachable by standing up an entire application, and therefore only
tested through one.

The rule for this package: a service takes its dependencies as arguments and
returns a value. No FastAPI, no request, no global. That is what makes it
testable without an app, and it is the boundary that keeps `app.py` from
becoming the place every new feature lands.
"""
