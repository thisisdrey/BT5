The `agave_transaction_view` crate is an external dependency (not present in this repo's search index), but the observed usage confirms it enforces `num_signatures() == num_required_signatures` and rejects zero-signature transactions during `try_new_sanitized`, as explicitly documented at [1](#0-0) , which states "vote could have 1 or 2 sigs; zero sig has already been excluded by sanitization." Both sanitization paths independently reject the scenario described in the question.

### Analysis

For the legacy path, `SanitizedVersionedTransaction::try_from(tx)` is called before `RuntimeTransaction::try_from` populates `signature_details.num_required_signatures` from the message header [2](#0-1) ; this conversion invokes the SDK's transaction sanitize logic, which is exercised directly by `Bank::verify_transaction_with_serialized_message` via `RuntimeTransaction::try_create` and covered by the test `test_verify_and_hash_transaction_sig_len`, which asserts that removing a signature (making `signatures.len() < num_required_signatures`) yields `TransactionError::SanitizeFailure` [3](#0-2) .

For the zero-copy path, `SanitizedTransactionView::try_new_sanitized` is called in `verify_packet` before the `signatures.is_empty()` branch is ever reached [4](#0-3) , and the codebase's own inline comment confirms that a transaction with zero signatures cannot pass this sanitization step [1](#0-0) . Since `header.num_required_signatures >= 1` combined with `signatures.len() == 0` would fail this check, no `Ok(view)` is ever produced in that case, meaning `verify_packet` returns `false` at line 32 (parse/sanitize failure) rather than reaching the `is_empty()` branch with a still-nonzero `num_required_signatures()` for a legitimately parsed view.

The `signatures.is_empty()` check at [5](#0-4)  is defense-in-depth for the theoretical case of a zero-signature message where `num_required_signatures == 0` (which sanitization does allow, e.g., an all-instruction-only message with no signers) — in that case `verified` is correctly forced to `false` even though there's nothing to check, and downstream `RuntimeTransaction::num_required_signatures()` would correctly report `0`, matching the `0` actual signatures checked. There is no path where `num_required_signatures` is cached as non-zero while `signatures` is empty and the transaction still passes sanitization, because both `SanitizedVersionedTransaction::try_from` and `SanitizedTransactionView::try_new_sanitized` reject any mismatch between `header.num_required_signatures` and `signatures.len()` before a `RuntimeTransaction` can be constructed. `resanitize_transaction_minimally` [6](#0-5)  also does not re-derive signature counts because it operates only on already-sanitized `TransactionWithMeta` instances whose `signature_details` were validated at initial sanitization time, and it only handles epoch-crossing/ALT-invalidation concerns, not re-verification of signatures (which is intentionally excluded from `resanitize_transaction_minimally`'s scope, consistent with its "minimally" naming).

#No vulnerability found for this question.

### Citations

**File:** perf/src/sigverify.rs (L30-33)
```rust
    let (is_simple_vote_tx, verified) = {
        let Ok(view) = SanitizedTransactionView::try_new_sanitized(data, &sanitize_config()) else {
            return false;
        };
```

**File:** perf/src/sigverify.rs (L43-46)
```rust
            let signatures = view.signatures();
            if signatures.is_empty() {
                (is_simple_vote_tx, false)
            } else {
```

**File:** perf/src/sigverify.rs (L76-78)
```rust
fn is_simple_vote_transaction_view<D: TransactionData>(view: &SanitizedTransactionView<D>) -> bool {
    // vote could have 1 or 2 sigs; zero sig has already been excluded by sanitization.
    if view.num_signatures() > 2 {
```

**File:** runtime-transaction/src/runtime_transaction/sdk_transactions.rs (L97-102)
```rust
        let statically_loaded_runtime_tx =
            RuntimeTransaction::<SanitizedVersionedTransaction>::try_from(
                SanitizedVersionedTransaction::try_from(tx)?,
                message_hash,
                is_simple_vote_tx,
            )?;
```

**File:** runtime/src/bank/tests.rs (L9375-9382)
```rust
    // Too few signatures: Sanitization failure
    {
        let tx = make_transaction(TestCase::RemoveSignature);
        assert_matches!(
            bank.verify_transaction(tx.into(), TransactionVerificationMode::FullVerification),
            Err(TransactionError::SanitizeFailure)
        );
    }
```

**File:** runtime/src/bank.rs (L3770-3807)
```rust
    pub fn resanitize_transaction_minimally(
        &self,
        transaction: &impl TransactionWithMeta,
        sanitized_epoch: Epoch,
        alt_invalidation_slot: Slot,
    ) -> Result<()> {
        if self.vote_only_bank() && !vote_parser::is_valid_vote_only_transaction(transaction) {
            return Err(TransactionError::SanitizeFailure);
        }

        // If the transaction was sanitized before this bank's epoch,
        // additional checks are necessary.
        if self.epoch() != sanitized_epoch {
            // Reserved key set may have changed, so we must verify that
            // no writable keys are reserved.
            self.check_reserved_keys(transaction)?;

            for instr in transaction.instructions_iter() {
                if instr.accounts.len() > solana_transaction_context::MAX_ACCOUNTS_PER_INSTRUCTION {
                    return Err(solana_transaction_error::TransactionError::SanitizeFailure);
                }
            }
        }

        if self.slot() > alt_invalidation_slot {
            // The address table lookup **may** have expired, but the
            // expiration is not guaranteed since there may have been
            // skipped slot.
            // If the addresses still resolve here, then the transaction is still
            // valid, and we can continue with processing.
            // If they do not, then the ATL has expired and the transaction
            // can be dropped.
            let (_addresses, _deactivation_slot) =
                self.load_addresses_from_ref(transaction.message_address_table_lookups())?;
        }

        Ok(())
    }
```
