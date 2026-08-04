### Title
Permissionless `Crowdloan::refund` can be permanently DoS'd by a single unrefundable contribution, locking a fund's raised balance forever - ([File: polkadot/runtime/common/src/crowdloan/mod.rs])

### Summary
`pallet-crowdloan`'s `refund` extrinsic loops over all child-trie contributions of an ended fund and pushes a native-currency transfer back to each contributor with a bare `?`. If a transfer to any single contributor fails, the whole call returns `Err`, and because FRAME dispatchables execute inside an implicit storage transaction, all progress made earlier in that same call (contribution kills, `fund.raised` updates) is rolled back as well. This is the same "push-refund can revert and block downstream state progression" primitive described in the external report (blacklisted USDC bidder blocking cancel/refund in `KimNFTMarketplace`), applied to Substrate's crowdloan refund/dissolve flow.

### Finding Description
`do_refund`/`refund` iterates `contribution_iterator` and for each `(who, (balance, memo))` does: [1](#0-0) 

```
for (who, (balance, _)) in contributions {
    if refund_count >= T::RemoveKeysLimit::get() {
        all_refunded = false;
        break;
    }
    CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;
    CurrencyOf::<T>::reactivate(balance);
    Self::contribution_kill(fund.fund_index, &who);
    fund.raised = fund.raised.saturating_sub(balance);
    refund_count += 1;
}
```

The `transfer(...)?` is an unconditional push to `who`. If the destination transfer fails for structural reasons (e.g. the contributor's account has since been fully reaped — zero providers/consumers, dust removed — and `balance` is below `ExistentialDeposit`, so a deposit into a non-existent account is rejected with `TokenError::BelowMinimum`/`ExistentialDeposit`), the `?` immediately returns `Err` from `refund`. Because `#[pallet::call]` dispatch bodies execute under FRAME's automatic transactional storage layer, this error rolls back every mutation made earlier in the *same* call — including `contribution_kill` and `fund.raised` decrements for contributors that were already successfully refunded in that batch.

Since `contribution_iterator` walks the child trie in a fixed key order, the same problematic contributor is encountered at the same relative position on every subsequent call to `refund`. Nothing in the pallet provides a way to skip, isolate, or later "claim" a failed refund (unlike the newer `cumulus/pallets/ah-ops` migration pallet, which already treats an equivalent native-currency withdrawal with `.defensive().map_err(...)` per-account instead of a hard, batch-wide `?` — see `do_withdraw_crowdloan_contribution` at `cumulus/pallets/ah-ops/src/lib.rs`). The old `refund` therefore has no fallback path once one contributor becomes structurally unrefundable.

`dissolve` requires `fund.raised.is_zero()` before it will remove the fund and burn/return leftover balances: [2](#0-1) 

If `refund` can never make forward progress past the stuck contributor, `fund.raised` never reaches zero and `dissolve` is permanently blocked, `Error::<T>::NotReadyToDissolve` forever.

Existing guards do not stop this path:
- `withdraw` (single-account withdrawal) also uses a bare `CurrencyOf::<T>::transfer(...)?` (line 490) and would independently fail for the same account, so a contributor can't self-serve their way out either.
- There is no `Preservation`/`Fortitude` handling, no `.defensive()`+continue, no per-account failure isolation, and no permissioned "sweep"/"claim later" mechanism — exactly the gap the external report calls out for the NFT marketplace.

### Impact Explanation
This is a public, unprivileged-triggerable state-lock: once one contribution becomes unrefundable, no further contributor in that fund can ever be refunded via `refund`/`withdraw`, and the fund can never be `dissolve`d. This permanently freezes the remaining raised balance inside the fund's sovereign pot account and blocks completion of the crowdloan lifecycle for every other contributor — a permanent user-fund lock caused by an unprivileged, non-malicious-validator condition (an account being reaped to zero providers), matching the "permanent user-fund or bridge-state lock" impact category.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires a contributor's account balance to be fully drained/reaped to zero providers (a naturally occurring or trivially self-inflictable condition, exactly mirroring the attacker "bid 0.01 USDC then get blacklisted" pattern from the source report, translated to "contribute a small amount, then drain the account to death"), combined with the contribution balance being below `ExistentialDeposit`. Because `contribute` enforces `T::MinContribution` but that minimum is runtime-configured and can be below `ExistentialDeposit` in some deployments, and because nothing prevents an account from being reaped after contributing, this is a realistic, low-cost griefing vector for any user who wants to permanently freeze a specific crowdloan's leftover funds.

### Recommendation
- Make refund failures per-contributor rather than batch-fatal: wrap each `transfer` in its own storage sub-transaction (e.g. `with_storage_layer`) or catch the error and skip/record the failing contributor instead of propagating `?` out of the whole loop.
- Follow the pattern already used in `cumulus/pallets/ah-ops` (`.defensive().map_err(...)` isolated per withdrawal) instead of the legacy unguarded `?`.
- For contributors whose refund transfer structurally cannot succeed (e.g. dead destination account below ED), record the failed amount/beneficiary and expose a permissioned or forced sweep (e.g. `force_transfer`/root-driven cleanup) so `fund.raised` can still reach zero and `dissolve` can proceed, instead of leaving fund state permanently stuck.

### Proof of Concept
1. Create a crowdloan fund and have accounts `A`, `B`, `C` each call `contribute` with amounts near `T::MinContribution` (below `ExistentialDeposit` is possible if `MinContribution < ED` in the runtime config).
2. Contributor `B` subsequently transfers away all other free balance and reduces its account to zero providers/consumers so the account is fully reaped (dead) before the crowdloan ends.
3. Advance to the fund's end block; call `Crowdloan::refund(origin, para)`.
4. When the iterator reaches `B`, `CurrencyOf::<T>::transfer(&fund_account, &B, balance, AllowDeath)` fails (deposit into dead account below ED), the `?` propagates, and the whole `refund` call reverts — including any `A`/earlier contributors already processed in that call.
5. Every subsequent call to `refund` hits the same deterministic child-trie ordering and fails at `B` again; `fund.raised` never reaches zero, so `Crowdloan::dissolve` permanently returns `Error::<T>::NotReadyToDissolve`, locking the pot's remaining balance indefinitely.

Note: I was not able to fully verify from the index whether `Currency::transfer` in this runtime's exact configuration would return an error for a sub-ED deposit into a fully-reaped account in every parameterization (this depends on `ExistentialDeposit`/`MinContribution` relationship in each runtime), so this should be validated with a concrete runtime configuration and a Devin session with test execution before treating it as fully proven.

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L519-536)
```rust

			let mut refund_count = 0u32;
			// Try killing the crowdloan child trie
			let contributions = Self::contribution_iterator(fund.fund_index);
			// Assume everyone will be refunded.
			let mut all_refunded = true;
			for (who, (balance, _)) in contributions {
				if refund_count >= T::RemoveKeysLimit::get() {
					// Not everyone was able to be refunded this time around.
					all_refunded = false;
					break;
				}
				CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;
				CurrencyOf::<T>::reactivate(balance);
				Self::contribution_kill(fund.fund_index, &who);
				fund.raised = fund.raised.saturating_sub(balance);
				refund_count += 1;
			}
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L562-571)
```rust
			// Only allow dissolution when the raised funds goes to zero,
			// and the caller is the fund creator or we are past the end date.
			let permitted = who == fund.depositor || now >= fund.end;
			let can_dissolve = permitted && fund.raised.is_zero();
			ensure!(can_dissolve, Error::<T>::NotReadyToDissolve);

			// Assuming state is not corrupted, the child trie should already be cleaned up
			// and all funds in the crowdloan account have been returned. If not, governance
			// can take care of that.
			debug_assert!(Self::contribution_iterator(fund.fund_index).count().is_zero());
```
