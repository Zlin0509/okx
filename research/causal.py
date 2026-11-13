"""Streaming synthetic adapter reusing the frozen R2 ledger primitives."""

from bisect import bisect_right
from decimal import localcontext

from .engine import ZERO, Engine, Pending, Rejected


class CausalEngine(Engine):
    def run_causal(self, decide):
        with localcontext() as context:
            context.prec = 34
            return self._causal(decide)

    def _causal(self, decide):
        if self.used:
            raise Rejected("engine_reuse", "Each adapter is single-use")
        self.used = True
        if self.market.provenance != "synthetic_test":
            raise Rejected("real_window_disabled", "M3 accepts synthetic provenance only")
        self.market = self.market.normalized()
        self.market.validate()
        self.costs.validate()
        if self.initial <= 0:
            raise Rejected("invalid_capital", "Positive initial equity required")
        pending = []
        seen = set()
        self.cancel_books = set()
        unresolved_signals = []
        for frame in self.market.frames:
            self.record(frame, "mark")
            for leg, position in sorted(self.positions.items()):
                if position.quantity and frame.ts in self.market.funding_calendar[leg.symbol]:
                    rate = self.market.realized_rates[frame.ts, leg.symbol]
                    flow = -position.quantity * frame.marks[leg.symbol] * rate
                    self.cash += flow
                    self.funding += flow
                    self.record(
                        frame,
                        "funding",
                        book=leg.book,
                        symbol=leg.symbol,
                        rate=rate,
                        settlement_mark=frame.marks[leg.symbol],
                        cash_delta=flow,
                    )
            value = self.valuation(frame.marks)
            if value["equity"] <= 0:
                raise Rejected("insolvent", "No synthetic liquidation recovery is fabricated")
            if self.breached(value) and not self.halted:
                self.halted = True
                self.record(
                    frame,
                    "risk_halt",
                    cancelled_pending=len(pending),
                    cancelled_known_pending=len(pending),
                    cancelled_preloaded_future_signals=0,
                )
                pending = []
                for leg, position in sorted(self.positions.items()):
                    if position.quantity:
                        times = self.market.execution_calendar[leg.symbol]
                        index = bisect_right(times, frame.ts)
                        if index == len(times):
                            self.risk_exit_unresolved = True
                        else:
                            pending.append(
                                Pending(times[index], "risk_exit", frame.ts, leg, ZERO, risk_exit=True)
                            )
            for order in sorted(
                (p for p in pending if p.ts == frame.ts),
                key=lambda p: (not p.risk_exit, p.known_at, p.signal, p.leg),
            ):
                self.execute(frame, order)
            pending = [p for p in pending if p.ts > frame.ts]
            self.record(frame, "frame_end", risk_halted=self.halted)
            # Callback sees only current ledger/positions; no preloaded future decisions.
            if not self.halted:
                decisions = decide(frame, self, tuple(pending))
                if self.cancel_books:
                    cancelled = [p for p in pending if p.leg.book in self.cancel_books]
                    pending = [p for p in pending if p.leg.book not in self.cancel_books]
                    self.record(
                        frame,
                        "cancel_book_pending",
                        books=sorted(self.cancel_books),
                        cancelled_pending=len(cancelled),
                    )
                    self.cancel_books.clear()
                for signal in decisions:
                    if signal.id in seen or signal.known_at != frame.ts:
                        raise Rejected("invalid_signal", "Unique current-frame signals required")
                    seen.add(signal.id)
                    busy = {p.leg for p in pending}
                    if busy.intersection(signal.targets):
                        raise Rejected("overlapping_signal", "Pending legs cannot receive another decision")
                    try:
                        compiled = self.compile((signal,))
                    except Rejected as exc:
                        if exc.code != "missing_execution":
                            raise
                        unresolved_signals.append(signal.id)
                        self.record(
                            frame,
                            "unexecutable_signal",
                            signal=signal.id,
                            known_at=signal.known_at,
                            reason=exc.code,
                        )
                    else:
                        pending.extend(compiled)
        self.risk_exit_unresolved |= self.halted and any(p.quantity for p in self.positions.values())
        final = self.valuation(self.market.frames[-1].marks)
        return {
            "scope": "synthetic_interface_only",
            "full_research_conclusion_permitted": False,
            "initial_equity": self.initial,
            "cash": self.cash,
            **final,
            "fees": self.fees,
            "funding_cashflow": self.funding,
            "realized_pnl": self.realized,
            "slippage_diagnostic": self.slippage,
            "reconciliation_error": final["equity"]
            - (self.initial + self.realized + final["unrealized_pnl"] + self.funding - self.fees),
            "risk_halted": self.halted,
            "risk_exit_unresolved": self.risk_exit_unresolved,
            "unresolved_signal_ids": unresolved_signals,
            "ledger": self.ledger,
        }
