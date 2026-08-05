Audit Report

## Title
Meta-transaction replay after account reaping resets `frame_system` nonce to zero, bypassing `pallet-meta-tx`/`pallet-verify-signature` replay protection - ([File: substrate/frame/system/src/extensions/check_nonce.rs])

## Summary
`pallet-meta-tx` and `pallet-verify-signature` rely exclusively on `frame_system::CheckNonce` for replay protection of off-chain signed meta-transactions, comparing the submitted nonce against `Account::<T>::get(who).nonce`. When an account is fully reaped (`providers`/`sufficients` both zero) and later recreated by any unrelated funding event, its `AccountInfo` — including `nonce` — is reset to `0`, and `CheckNonce::validate_nonce_for_account` only checks staleness relative to this reset value, allowing a previously-consumed signed meta-tx to be resubmitted and re-executed.

## Finding Description
`CheckNonce::validate_nonce_for_account` in `substrate/frame/system/src/extensions/check_nonce.rs:70-91` implements the only replay-protection gate used by `pallet_meta_tx::Pallet::dispatch` (`substrate/frame/meta-tx/src/lib.rs:191-229`) via its `VerifySignature` + `CheckNonce` extension pipeline [1](#0-0) . The function contains a guard specifically intended to prevent nonce reuse on reaped accounts: `if account.providers.is_zero() && account.sufficients.is_zero() { return Err(InvalidTransaction::Payment.into()); }` [2](#0-1) . However, this guard only blocks transactions *while the account remains reaped* (i.e., `providers == 0 && sufficients == 0`). Once any unrelated party touches the account again (e.g., sends it funds, which increments `providers` back to a nonzero value via the normal account-creation path), a fresh `AccountInfo` is created with `nonce = 0` — as confirmed by `test_default_account_nonce` (`substrate/frame/system/src/tests.rs:890-905`) and the dust-removal tests in `substrate/frame/balances/src/tests/*` which assert `System::account_nonce(&2) == 0` after reaping. At that point, `providers` is nonzero again, so the "Payment" guard no longer triggers, and the remaining check `if nonce < account.nonce { return Err(InvalidTransaction::Stale.into()); }` [3](#0-2)  passes trivially for any old nonce `N ≥ 0` compared against the reset `account.nonce = 0`. `prepare_nonce_for_account` (`substrate/frame/system/src/extensions/check_nonce.rs:94-105`) then accepts and increments from `0`, and `pallet_verify_signature::VerifySignature`'s signature check is unaffected since the signed payload (call + nonce + extension data) is unchanged. Thus a relayer holding an old, previously-executed meta-tx signed with nonce `N` can resubmit it after the signer's account is drained and refunded, and both `VerifySignature` and `CheckNonce` pass.

## Impact Explanation
This matches the "unauthorized execution" impact category: a signed, previously-executed action can be re-dispatched on the signer's behalf without new authorization, purely as a consequence of a normal, unprivileged balance/account lifecycle event (dust removal and later re-funding) combined with an unprivileged actor (any relayer/observer) resubmitting a publicly shared meta-tx payload. Depending on the replayed call this could cause duplicate execution of state-changing effects authorized by the original signer.

## Likelihood Explanation
Exploitation requires: (1) the signer's account to be fully reaped (`providers == 0 && sufficients == 0`) at some point after signing/executing a meta-tx, and (2) the account to subsequently be re-funded/touched by any party, and (3) an attacker to have retained the old signed meta-tx and resubmit it. Meta-transactions are explicitly designed as shareable, publicly re-broadcastable payloads, and dust removal via `pallet-balances` is a routine, easily triggerable event (self-drain or counterparty transfer below existential deposit), making the preconditions realistic though not automatic — they depend on the specific account lifecycle sequence occurring around the meta-tx's validity window.

## Recommendation
Do not rely solely on the reapable `frame_system` nonce for meta-tx replay protection. Either (a) prevent the "Payment" guard from being bypassable by cheap re-funding — e.g., require a minimum holding period or additional non-resettable state — or (b) bind meta-tx/verify-signature replay protection to a dedicated, monotonic nonce store that is never cleared by account reaping (e.g., a separate `MetaTxNonce` map keyed by signer that persists independent of `AccountInfo` removal).

## Proof of Concept
1. Alice signs meta-tx `M` (e.g. `remark_with_event`) at nonce `N`; a relayer submits it via `pallet_meta_tx::Pallet::dispatch`, incrementing `Account::<Runtime>::get(alice).nonce` to `N+1`.
2. Alice's account is drained below the existential deposit by any transfer, triggering dust removal: `providers`/`sufficients` fall to zero and `AccountInfo` (including nonce) is removed — reproduced by `dust_account_removal_should_work` (`substrate/frame/balances/src/tests/dispatchable_tests.rs:47-61`).
3. Any account funds Alice again, recreating `AccountInfo` with `providers = 1`, `nonce = 0` (per `test_default_account_nonce`, `substrate/frame/system/src/tests.rs:890-905`).
4. A relayer resubmits the original meta-tx `M` (nonce `N`) through `pallet_meta_tx::Pallet::dispatch`. `VerifySignature` succeeds; `CheckNonce::validate_nonce_for_account` passes because `providers != 0` (bypassing the Payment guard) and `N >= 0` (bypassing the Stale check), so the call dispatches a second time.

### Citations

**File:** substrate/frame/meta-tx/src/lib.rs (L206-215)
```rust
			let meta_dispatch_res = meta_tx
				.extension
				.dispatch_transaction(
					origin.into(),
					meta_tx.call,
					&info,
					meta_tx_size,
					meta_tx.extension_version,
				)
				.map_err(Error::<T>::from)?;
```

**File:** substrate/frame/system/src/extensions/check_nonce.rs (L74-81)
```rust
		let account = crate::Account::<T>::get(who);
		if account.providers.is_zero() && account.sufficients.is_zero() {
			// Nonce storage not paid for
			return Err(InvalidTransaction::Payment.into());
		}
		if nonce < account.nonce {
			return Err(InvalidTransaction::Stale.into());
		}
```
