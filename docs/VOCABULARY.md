# Wavr's words

One concept, one word, on every surface a person reads: the dashboard, the
Windows tray, the Android notification, the Core Panel, the CLI and the MCP
tools.

The reason is not tidiness. A person who learns that "offline" means a sensor
stopped, and then reads "disconnected" somewhere else, has to work out whether
those are the same thing — and the honest answer, for most products, is that
nobody knows. Once that happens they stop trusting any of the words.

**Force identical meaning, not identical code.** Each surface renders in its own
idiom; none of them may invent a second name for a thing that already has one.

---

## Two vocabularies, deliberately

Wavr has real architectural nouns — Core, Node, provider, manifest, anchor,
coordinate frame — and a household did not sign up to learn them.

| Concept | A normal person is shown | An admin or developer sees |
|---|---|---|
| The machine doing the thinking | *Wavr* (or nothing at all) | **Core** |
| A small sensor board | *sensor* | **Node** |
| A phone or tablet that only views | *device* | **Client** |
| A thing that produces evidence | *sensor* | **provider** |
| What an application declares it needs | — | **Experience Manifest** |
| A named place in a room | *place* | **anchor** |
| How detailed an answer can be | *detail* | **precision** |
| How sure Wavr is | *confidence* | **confidence** |
| What a room can perceive | *what Wavr can see here* | **coverage** |
| A guided walk that measures sensors | *check* | **validation** |

The right-hand column is not hidden — it appears in Developer Mode, in the API
and in this repository. It is simply not the first thing a household reads.

---

## State words, and what each one promises

These are the words that must never drift, because each is a different claim
about what Wavr knows.

### About a room

| Word | Means | Must NOT be used for |
|---|---|---|
| **Occupied** | A sensor is reporting somebody there. | A guess, a stale reading. |
| **Empty** | A working sensor looked and saw nobody. | A room nothing is watching. |
| **Not right now** | A sensor is here and is not reporting. | A room that has no sensor. |
| **No coverage** | Nothing watches this room at all. | A sensor that is switched off. |
| **Unknown** | Wavr cannot tell. | Zero, false, or empty. |

The last four are the ones that get collapsed, and collapsing any of them into
**Empty** is the product claiming a check it did not perform.

### About a sensor

| Word | Means |
|---|---|
| **Reporting** | Producing evidence now. |
| **Not reporting** | Should be producing and is not. A fault. |
| **Switched off** | A person turned it off. Not a fault. |
| **Covered** | A camera is deliberately covered. Not a fault. |
| **Cannot tell** | Wavr does not know its state. |

"Offline" is the machine-readable value on the wire; **"not reporting"** is what
a person is shown, because "offline" is also what a laptop is when it is asleep.

### About the Core itself

Produced by `wavr/runtime_status.py` and rendered — never re-derived — by the
tray, the header chip, the Android notification and `wavr status`.

| State | Shown as | Means |
|---|---|---|
| `healthy` | **Live** | Producing room readings now. |
| `starting` | **Starting** | Up, no reading yet, not yet late. |
| `updating` | **Updating** | An update is in progress. |
| `paused` | **Paused** | Running and deliberately not watching. |
| `degraded` | **1 issue** / **N issues** | Working, less well, fixably. |
| `attention` | **Needs attention** | Something needs a person. |
| `unavailable` | **Not responding** | No answer, or nothing produced for an hour. |

**"Not responding" is about the Core.** "Not reporting" is about a sensor. They
are different failures with different errands and the two words are not
interchangeable.

---

## Rules

1. **The producer names it once.** If a state comes from `runtime_status`,
   every surface renders that state's word. A surface that computes its own
   verdict has invented a second vocabulary, and the two will disagree in front
   of somebody with no way to tell which is right.

2. **Never colour alone.** Every state carries its word as text. A colour is
   nothing to a screen reader and ambiguous to anyone who has learned a
   different product's palette.

3. **Absence is a word, not a blank.** "Cannot tell" is an answer; an empty
   cell is a question the interface declined to answer.

4. **Say the cost, not the status.** "Kitchen radar not reporting" is a status.
   "While it is down, Wavr may not be able to count people in the kitchen" is
   the reason to go and fix it, and Wavr is uniquely able to write it because it
   knows what that sensor was contributing.

5. **A word that changes meaning between surfaces is a bug**, and
   `test_vocabulary.py` fails on the ones that can be checked mechanically.
