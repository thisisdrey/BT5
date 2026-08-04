## Analog Identified

The core broken invariant in the OpenVM report is: *a monotonically increasing counter used to enforce ordering/replay-protection is allowed to overflow its finite domain, and the consuming logic only checks the **delta** between two counter values, not that the counter itself stays within bounds — so wraparound reintroduces a value that was already valid/used before.*

The direct structural analog in `polkadot-sdk` is Substrate's account transaction **nonce**, used by `CheckNonce` as the replay-protection primitive for every signed extrinsic.### Title
Account nonce silently wraps to zero on overflow, resetting replay protection - (File: `substrate/frame/system/src/extensions/check_nonce.rs`)

### Summary
`CheckNonce::prepare_nonce_for_account` advances an account's transaction nonce using `checked_add(&T::Nonce::one()).unwrap_or(T::Nonce::zero())`. Instead of hard-failing or saturating at the type's maximum when the nonce would overflow, the code silently resets the counter to `0`. This is the same bug class as the OpenVM report: a monotonically increasing counter that is trusted to strictly order/replay-protect events, but whose overflow behavior is not bounded, so wraparound reintroduces a previously-valid value into the active domain.

### Finding Description
`T::Nonce` (typically `u32`, see `substrate/frame/system/src/lib.rs`) is stored per-account in `Account<T>` and is the sole replay-protection mechanism for extrinsics: `validate_nonce_for_account` rejects any submitted nonce `< account.nonce` as `Stale` [1](#0-0) , and any nonce `> account.nonce` in `prepare_nonce_for_account` as `Future` [2](#0-1) . This design assumes the counter is monotonic and bounded away from overflow for the lifetime of the account.

The actual increment step does not enforce that assumption: [3](#0-2) 
When `account.nonce == T::Nonce::MAX`, `checked_add` returns `None` and the `unwrap_or` clause silently sets the new nonce back to `0` instead of erroring out or saturating at `MAX`. This mirrors the OpenVM flaw exactly: the "delta" checks (`Stale`/`Future` comparisons) only look at relative ordering between the submitted nonce and the stored nonce — nothing constrains the *absolute* counter value to stay inside its safe (non-wrapping) range, so once the counter wraps, an old, previously-consumed sequence number (`0`) becomes valid again.

Once the stored nonce for the account is reset to `0`, any historical signed extrinsic from that account with `nonce = 0` (and no expired mortality checkpoint, e.g. an immortal transaction, or one whose mortal era window can be recreated) becomes re-executable as a fresh, valid transaction. Because `validate_nonce_for_account`/`prepare_nonce_for_account` only compare against the current stored value, they will happily accept it as the "next" nonce again.

### Impact Explanation
This breaks the "settle exactly once" invariant for balances/assets: an old transfer, staking, or contract-call extrinsic that was already applied once could be validly re-submitted and re-applied after the account's nonce wraps, causing duplicate settlement/duplicate payout from that account, or unauthorized re-execution of a stale privileged call still signed by the account (e.g., an old `proxy`/`utility` batch, an old approval, etc.). This falls squarely in the "duplicate settlement or payout" and "runtime bugs that compromise intended behavior" categories of the impact gate.

### Likelihood Explanation
Reaching `T::Nonce::MAX` (`u32::MAX` ≈ 4.29 billion) for a single account requires submitting and having accepted an enormous number of extrinsics from that account — extremely expensive in fees and block space, but not impossible, exactly analogous to the OpenVM report's own caveat that the exploit is "computationally expensive... but feasible and still accepted by the verifier." No governance, admin, validator, or malicious-peer assumption is required — an ordinary account holder willing to pay the cost can trigger it themselves.

### Recommendation
Do not wrap the nonce back to a valid value on overflow. `prepare_nonce_for_account` should return an error (e.g. a new `InvalidTransaction::ExhaustsResources`/custom overflow variant) when `checked_add` fails, permanently disabling further nonce advancement for that account rather than recycling `0`. Alternatively, saturate at `T::Nonce::MAX` so the account can never submit another extrinsic, rather than silently reintroducing a previously-used nonce value into the valid range.

### Proof of Concept
1. An attacker-controlled (or any) account submits and gets included `u32::MAX` extrinsics over time (any minimal-cost calls), driving `Account::<T>::get(who).nonce` to `u32::MAX`.
2. The attacker keeps one old, already-executed signed extrinsic with `nonce = 0` from early in the account's history (ideally immortal, or re-signed with a currently valid mortal checkpoint if resigning is possible — otherwise any account-authorized call that remains meaningful, e.g. a `proxy::proxy` or transfer).
3. The attacker submits one more extrinsic with `nonce = u32::MAX`. `validate_nonce_for_account` accepts it (`nonce == account.nonce`), and `prepare_nonce_for_account` computes `checked_add` → `None` → resets stored nonce to `0` via `unwrap_or(T::Nonce::zero())` [4](#0-3) .
4. The attacker resubmits the old `nonce = 0` extrinsic from step 2. `validate_nonce_for_account` now sees `account.nonce == 0`, accepts it as valid (not `Stale`), and it is dispatched a second time, duplicating its effect (e.g. duplicate transfer/payout) on-chain.

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
