"""Verdicts for the numbered bursts the receiver sees.

A burst starts with an announcement vector (how many data vectors follow) and
continues with data vectors numbered from 1. A run that never saw an
announcement is still tracked; its gap check then covers the sequence numbers
that were observed.
"""
from __future__ import annotations

from . import vectors

MISSING_SAMPLE = 50           # missing sequence numbers echoed in a verdict
MS_IN_NS = 1_000_000
S_IN_NS = 1_000_000_000


class VectorRun:
    """One numbered burst as the receiver saw it."""

    def __init__(self, started_ns, expected=None):
        self.expected = expected      # None until an announcement is seen
        self.state = "running"
        self.ok = self.corrupt = self.dup = self.out_of_order = 0
        self.payload_bad = self.unexpected = 0
        self.seen = set()
        self._in_range = 0
        self.low = None
        self.high = 0
        self.started_ns = started_ns
        self.last_ns = started_ns
        self.finished_ns = None
        self.reason = None
        self.verdict = None
        self.missing = []
        self.head_kept = None
        self.tail_kept = None
        self.archived = False    # set by the owner once it copied the verdict

    @classmethod
    def idle(cls):
        run = cls(0)
        run.state = "idle"
        return run

    def add(self, vec, now_ns):
        """Account for one data vector."""
        seq = vec.seq
        self.last_ns = now_ns
        if seq in self.seen:
            self.dup += 1
        else:
            self.seen.add(seq)
            self.ok += 1
            self.low = seq if self.low is None else min(self.low, seq)
            self.high = max(self.high, seq)
            if self.expected is not None and 1 <= seq <= self.expected:
                self._in_range += 1
            elif self.expected is not None:
                self.unexpected += 1
        if seq < self.high:
            self.out_of_order += 1
        if vec.payload != vectors.payload_for(seq, len(vec.payload)):
            self.payload_bad += 1

    def complete(self):
        """True once every announced sequence number has arrived."""
        return self.expected is not None and self._in_range >= self.expected

    def finalize(self, now_ns, reason):
        """Close the run and decide its verdict; a closed run stays closed."""
        if self.state != "running":
            return
        self.state = "done"
        self.finished_ns = now_ns
        self.reason = reason
        if self.expected is not None:
            wanted, first, last = range(1, self.expected + 1), 1, self.expected
        elif self.low is None:
            wanted, first, last = range(0), None, None
        else:
            wanted = range(self.low, self.high + 1)
            first, last = self.low, self.high
        self.missing = sorted(set(wanted) - self.seen)
        problems = (len(self.missing) + self.dup + self.out_of_order
                    + self.corrupt + self.payload_bad + self.unexpected)
        self.verdict = "pass" if problems == 0 else "fail"
        self.head_kept = first is not None and first in self.seen
        self.tail_kept = last is not None and last in self.seen

    def to_dict(self):
        done = self.state != "running"
        if self.expected is None:
            missing_count = len(self.missing) if done else None
        else:
            missing_count = (len(self.missing) if done
                             else self.expected - self._in_range)
        end = self.finished_ns if done and self.finished_ns is not None \
            else self.last_ns
        return {
            "state": self.state,
            "expected": self.expected,
            "ok": self.ok,
            "corrupt": self.corrupt,
            "dup": self.dup,
            "out_of_order": self.out_of_order,
            "payload_bad": self.payload_bad,
            "unexpected": self.unexpected,
            "missing_count": missing_count,
            "missing": self.missing[:MISSING_SAMPLE],
            "head_kept": self.head_kept,
            "tail_kept": self.tail_kept,
            "reason": self.reason,
            "verdict": self.verdict,
            "duration_ms": (end - self.started_ns) / MS_IN_NS,
        }
