### Title
Silent transfer-failure in `pallet-society` payout reservation breaks pot/account balance invariant - (File: `substrate/frame/society/src/lib.rs`)

### Summary
`pallet-society`'s internal `reserve_payout` and `unreserve_payout` helpers mutate the `Pot` storage value unconditionally and then perform a `T::Currency::transfer` whose result is only checked via `debug_assert!`, which is compiled out entirely in release/production builds. This mirrors the Solmate `safeTransfer`/`safeTransferFrom` bug class from the external report: a transfer's success is assumed rather than enforced, so accounting state (the `Pot` and the `Payouts` bookkeeping) can diverge from the real token balances held by the society's accounts.

### Finding Description
`reserve_payout` decreases `Pot` and then calls `T::Currency::transfer(&Self::account_id(), &Self::payouts(), amount, AllowDeath)`, only asserting the result with `debug_assert!(res.is_ok())`: [1](#0-0) 

The same pattern exists symmetrically in `unreserve_payout`, which increases `Pot` and transfers funds back from the payouts account to the main account, again only `debug_assert!`-checking the result.

Both functions are called from `strike_member`/`slash_payout` and from the reward path when a bidder is inducted (`reward_bidder`), i.e., they run inside normal, permissionless flows (candidacy acceptance, strikes accumulating, member removal) rather than any privileged/governance-only call. `debug_assert!` macros are no-ops in release builds (the builds used for live runtimes), so in production any failure of the underlying `transfer` (e.g., the source account lacking funds to satisfy the existential deposit under `AllowDeath` semantics in edge cases, arbitrary `Currency` trait implementations returning `Err`, or future changes to `T::Currency`) is silently swallowed. The `Pot`/`Payouts` storage has already been mutated before the transfer is attempted, so a failed transfer leaves the pallet's internal accounting inconsistent with the real balances of `Self::account_id()` and `Self::payouts()`.

This is exactly the underlying invariant break identified in the external report: a transfer call's return value/success is not enforced, so the caller's bookkeeping records a transfer as done when the underlying token movement did not actually occur.

### Impact Explanation
If the debug-assert is silently skipped in production and the underlying transfer fails, the pallet's `Payouts` storage will record payout entitlements that are not backed by real reserved funds in the `payouts()` sub-account, or the `Pot` will be adjusted without funds actually moving. Later payout claims (which read from `Payouts` and pay out from the `payouts()` account) can then fail or pay out less than expected, and the pallet's own `do_try_state` invariant check (`T::Currency::free_balance(&Self::payouts()) == Self::pending_payouts_total()`) — which only runs under `try-runtime`/`test` features, not in production — would catch this only outside of live conditions. In production this manifests as a genuine break of the "funds conserve and settle exactly once" invariant for society reward payouts, with no forced-error path in normal builds.

### Likelihood Explanation
The precondition (`Currency::transfer` returning `Err` after the `can_deposit`/pot bookkeeping already succeeded) is intended to be unreachable by the code comment ("this should never fail since we ensure we can afford the payouts in a previous block"), but the pallet does not enforce this via `ensure!`/propagated `DispatchResult` — it only relies on a debug assertion that vanishes in release. Any drift in that assumption (custom `Currency` implementations, ED edge cases, future runtime upgrades introducing new failure modes such as freezes/holds on the pot accounts) silently corrupts state rather than aborting the extrinsic, which is a low-cost, code-path-only condition to trigger compared to genuinely exploitable bugs, but is a real defensive gap directly analogous to the reported bug class.

### Recommendation
Propagate the `Result` from `T::Currency::transfer` in both `reserve_payout` and `unreserve_payout` instead of using `debug_assert!`, returning a `DispatchResult`/`Result` up to callers (`strike_member`, `slash_payout`, `reward_bidder`, `remove_member`, etc.) so failures abort the extrinsic and revert the `Pot`/`Payouts` mutation atomically, rather than allowing production builds to silently record a payout that never happened.

### Proof of Concept
1. In a production (release, non-`try-runtime`) build, construct a scenario where `T::Currency::transfer(&Self::account_id(), &Self::payouts(), amount, AllowDeath)` returns `Err` (e.g., a `Currency`/`fungible` implementation that fails a transfer due to a hold/freeze on the source account balance that isn't accounted for in the "previous block" affordability check).
2. Trigger `reserve_payout` via the normal reward path (`induct_member` → `reward_bidder` → `reserve_payout`) with an unprivileged bid/candidacy flow.
3. Because `debug_assert!` compiles to nothing in release, the extrinsic completes successfully: `Pot` has been reduced and `Payouts` records the new payout entry, even though no tokens moved into `Self::payouts()`.
4. A later claim against `Payouts` for that account will attempt to pay out from `Self::payouts()`, which lacks the backing funds — surfacing as underflow/failed payout only downstream, after the incorrect accounting state has already been committed on-chain. [2](#0-1)

### Citations

**File:** substrate/frame/society/src/lib.rs (L2184-2206)
```rust
	/// Transfer some `amount` from the main account into the payouts account and reduce the Pot
	/// by this amount.
	fn reserve_payout(amount: BalanceOf<T, I>) {
		// Transfer payout from the Pot into the payouts account.
		Pot::<T, I>::mutate(|pot| pot.saturating_reduce(amount));

		// this should never fail since we ensure we can afford the payouts in a previous
		// block, but there's not much we can do to recover if it fails anyway.
		let res = T::Currency::transfer(&Self::account_id(), &Self::payouts(), amount, AllowDeath);
		debug_assert!(res.is_ok());
	}

	/// Transfer some `amount` from the main account into the payouts account and increase the Pot
	/// by this amount.
	fn unreserve_payout(amount: BalanceOf<T, I>) {
		// Transfer payout from the Pot into the payouts account.
		Pot::<T, I>::mutate(|pot| pot.saturating_accrue(amount));

		// this should never fail since we ensure we can afford the payouts in a previous
		// block, but there's not much we can do to recover if it fails anyway.
		let res = T::Currency::transfer(&Self::payouts(), &Self::account_id(), amount, AllowDeath);
		debug_assert!(res.is_ok());
	}
```
