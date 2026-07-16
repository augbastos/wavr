"""Per-person arrived/left edges: baseline at boot (no spurious arrival), debounced
departures, and consent inherited from the caller's feed."""
from wavr.person_presence import PersonPresence


def _tracker(grace=2):
    edges = []
    return PersonPresence(on_edge=lambda p, home: edges.append((p, home)), grace=grace), edges


def test_boot_baseline_does_not_fire_for_already_present_people():
    t, edges = _tracker()
    t.update({"Augusto"})            # first update = baseline
    assert edges == [], "someone already home at boot did NOT just arrive"
    assert t.home_persons() == {"Augusto"}


def test_arrival_after_baseline_fires_once():
    t, edges = _tracker()
    t.update({"Augusto"})            # baseline: Augusto home
    t.update({"Augusto", "Bea"})     # Bea arrives
    assert edges == [("Bea", True)]
    t.update({"Augusto", "Bea"})     # still both -> no repeat
    assert edges == [("Bea", True)]


def test_departure_is_debounced_by_grace():
    t, edges = _tracker(grace=2)
    t.update({"Augusto"})            # baseline
    t.update(set())                  # miss #1 -> not yet left (grace 2)
    assert edges == []
    t.update(set())                  # miss #2 -> left edge
    assert edges == [("Augusto", False)]
    assert t.home_persons() == set()


def test_brief_miss_does_not_fire_a_false_departure():
    t, edges = _tracker(grace=2)
    t.update({"Augusto"})            # baseline
    t.update(set())                  # miss #1
    t.update({"Augusto"})            # back before grace -> streak resets, no left edge
    t.update(set())                  # miss #1 again
    assert edges == [], "a one-cycle ARP drop never fired a false 'left'"


def test_round_trip_leave_then_return():
    t, edges = _tracker(grace=1)
    t.update({"Augusto"})            # baseline home
    t.update(set())                  # grace 1 -> left
    t.update({"Augusto"})            # arrived again
    assert edges == [("Augusto", False), ("Augusto", True)]


def test_only_persons_the_caller_feeds_ever_edge():
    # Consent is the caller's job: an anonymous/withdrawn device is simply never in the
    # fed set, so it can never produce a named edge here.
    t, edges = _tracker()
    t.update({"Augusto"})            # baseline
    t.update({"Augusto"})            # an anonymous device present but NOT named -> not fed
    assert edges == [], "no named edge for anyone the caller didn't include"
