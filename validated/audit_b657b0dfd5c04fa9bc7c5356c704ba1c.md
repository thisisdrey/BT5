This confirms the key point: `execute_intents` calls `.unwrap_or_else(|e| e.panic())` on any error from `execute_signed_intents`, which includes the `InvariantViolated::Overflow` path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-284)
```rust
    // Finalizes all transfers, or returns unmatched deltas.
    // If unmatched deltas overflow, then Err(None) is returned.
    pub fn finalize(self) -> Result<Transfers, InvariantViolated> {
        let mut transfers = Transfers::default();
        let mut deltas = TokenDeltas::default();
        for (token_id, transfer_matcher) in self.0 {
            if let Err(unmatched) = transfer_matcher.finalize_into(&token_id, &mut transfers)
                && (unmatched == 0 || deltas.apply_delta(token_id, unmatched).is_none())
            {
                return Err(InvariantViolated::Overflow);
            }
        }
        if !deltas.is_empty() {
            return Err(InvariantViolated::UnmatchedDeltas {
                unmatched_deltas: deltas,
            });
        }
        Ok(transfers)
    }
}
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L374-388)
```rust
        // only sender(s) left
        if let Some((_, send)) = withdraw {
            return Err(withdrawals
                .try_fold(send, |total, (_, s)| total.checked_add(s))
                .and_then(|total| i128::try_from(total).ok())
                .and_then(i128::checked_neg)
                .unwrap_or_default());
        }
        // only receiver(s) left
        if let Some((_, receive)) = deposit {
            return Err(deposits
                .try_fold(receive, |total, (_, r)| total.checked_add(r))
                .and_then(|total| i128::try_from(total).ok())
                .unwrap_or_default());
        }
```

**File:** contracts/defuse/core/src/engine/mod.rs (L113-118)
```rust
    #[inline]
    fn finalize(self) -> Result<Transfers> {
        self.state
            .finalize()
            .map_err(DefuseError::InvariantViolated)
    }
```

**File:** contracts/defuse/src/contract/intents/mod.rs (L27-31)
```rust
    fn execute_intents(&mut self, signed: Vec<MultiPayload>) {
        if let Some(event) = Engine::new(self, ExecuteInspector::default())
            .execute_signed_intents(signed)
            .unwrap_or_else(|e| e.panic())
            .as_mt_event()
```
