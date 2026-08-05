The claim accurately describes the code as it exists in this repository. `CheckNonce::prepare_nonce_for_account` computes `nonce.checked_add(&T::Nonce::one()).unwrap_or(T::Nonce::zero())`, silently wrapping the stored account nonce back to `0` on overflow instead of erroring or saturating. [1](#0-0) 

The surrounding validity checks in `validate_nonce_for_account` only compare the submitted nonce against the stored value (`Stale` if less, `Future` if greater in `prepare_nonce_for_account`), and never bound the stored counter away from its type's maximum. [2](#0-1) [3](#0-2) 

This matches the structural analog claimed: a monotonic replay-protection counter whose overflow path recycles an already-used value (`0`) back into the valid domain, since nothing besides relative-delta comparisons constrains the absolute counter value. Once wrapped, a historical nonce-`0` signed extrinsic (immortal, or otherwise still dispatchable) would again pass the `Stale`/`Future` checks and be re-executed, enabling duplicate settlement — squarely within the "duplicate settlement or payout" / "runtime bugs that compromise intended behavior" impact gate. The exploit path is reachable purely by an unprivileged account submitting ordinary signed extrinsics (no validator/governance/relayer privilege required), satisfying the "unprivileged external attacker using public extrinsics" requirement.

Audit Report

## Title
Account nonce silently wraps to zero on overflow, resetting replay protection - (File: `substrate/frame/system/src/extensions/check_nonce.rs`)

## Summary
`CheckNonce::prepare_nonce_for_account` advances an account's transaction nonce using `checked_add(&T::Nonce::one()).unwrap_or(T::Nonce::zero())`. When the nonce is at `T::Nonce::MAX`, this silently resets the stored nonce to `0` instead of erroring out or saturating, reintroducing a previously-valid/used nonce value into the account's active replay-protection domain.

## Finding Description
`validate_nonce_for_account` rejects any nonce `< account.nonce` as `Stale`, and `prepare_nonce_for_account` rejects any nonce `> account.nonce` as `Future`; both only compare relative ordering against the currently stored value, never bounding the stored counter itself away from wraparound. The increment step in `prepare_nonce_for_account` computes `nonce.checked_add(&T::Nonce::one()).unwrap_or(T::Nonce::zero())`, so once `account.nonce == T::Nonce::MAX`, the next successful transaction resets the stored nonce to `0`. From that point, `validate_nonce_for_account` will accept a resubmitted historical extrinsic signed with `nonce = 0` as valid (not stale), because it only checks `nonce < account.nonce`, which is now false.

## Impact Explanation
This breaks the "settle exactly once" invariant: a previously-executed signed extrinsic with `nonce = 0` (immortal, or otherwise still dispatchable) becomes replayable, causing duplicate settlement/duplicate payout or unauthorized re-execution of a stale privileged call authorized by that account (e.g., an old transfer, proxy call, or utility batch). This falls under "duplicate settlement or payout" and "runtime bugs that compromise intended behavior" in the impact gate.

## Likelihood Explanation
Reaching `T::Nonce::MAX` for one account (commonly `u32::MAX`, ≈4.29 billion) requires an enormous number of accepted extrinsics from that account, which is expensive in fees/block space but requires no privileged, validator, governance, or off-chain-infrastructure capability — any account holder willing to pay the cost can trigger it unilaterally, matching the "unprivileged external attacker using public extrinsics" requirement.

## Recommendation
On overflow, `prepare_nonce_for_account` should return a `TransactionValidityError` (e.g., a resource-exhaustion or custom overflow variant) rather than wrapping to `0`, or at minimum saturate at `T::Nonce::MAX` so the account can never submit another extrinsic instead of recycling a previously-valid nonce back into range.

## Proof of Concept
1. Drive `Account::<T>::get(who).nonce` to `T::Nonce::MAX` for an account via repeated accepted extrinsics.
2. Retain one already-dispatched signed extrinsic with `nonce = 0` from that account's early history (immortal, or one whose mortal window can be recreated).
3. Submit one more extrinsic at `nonce = T::Nonce::MAX`; `validate_nonce_for_account` accepts it (`nonce == account.nonce`), and `prepare_nonce_for_account`'s `checked_add` overflows, resetting the stored nonce to `0` via `unwrap_or(T::Nonce::zero())`.
4. Resubmit the retained `nonce = 0` extrinsic; `validate_nonce_for_account` now sees `account.nonce == 0`, treats it as non-stale, and it dispatches a second time, duplicating its on-chain effect.

### Citations

**File:** substrate/frame/system/src/extensions/check_nonce.rs (L70-81)
```rust
	pub fn validate_nonce_for_account(
		who: &T::AccountId,
		nonce: T::Nonce,
	) -> Result<ValidNonceInfo, TransactionValidityError> {
		let account = crate::Account::<T>::get(who);
		if account.providers.is_zero() && account.sufficients.is_zero() {
			// Nonce storage not paid for
			return Err(InvalidTransaction::Payment.into());
		}
		if nonce < account.nonce {
			return Err(InvalidTransaction::Stale.into());
		}
```

**File:** substrate/frame/system/src/extensions/check_nonce.rs (L94-105)
```rust
	pub fn prepare_nonce_for_account(
		who: &T::AccountId,
		mut nonce: T::Nonce,
	) -> Result<(), TransactionValidityError> {
		let account = crate::Account::<T>::get(who);
		if nonce > account.nonce {
			return Err(InvalidTransaction::Future.into());
		}
		nonce = nonce.checked_add(&T::Nonce::one()).unwrap_or(T::Nonce::zero());
		crate::Account::<T>::mutate(who, |account| account.nonce = nonce);
		Ok(())
	}
```
