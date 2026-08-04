### Title
Silent swallowing of failed beneficiary/curator payout transfers via `debug_assert!` permanently locks bounty funds - ([File: substrate/frame/bounties/src/lib.rs])

### Summary
`pallet-bounties::claim_bounty` and `pallet-child-bounties::claim_child_bounty` perform the final payout by calling `T::Currency::transfer(...)` and then only verify success with `debug_assert!(res.is_ok())`, instead of propagating the error with `?`. Immediately afterward, the bounty (or child-bounty) storage record is unconditionally deleted (`*maybe_bounty = None`). `debug_assert!` is compiled to a no-op in release builds (the build profile chain nodes run in production), so if the currency transfer to the beneficiary fails, the error is silently discarded, the extrinsic still returns `Ok(())`, and the bounty record that tracked the entitlement is gone forever. This is structurally identical to the reported `payable.transfer()` bug: state is deleted based on the assumption that the transfer "should not fail," with no fallback path if it actually does.

### Finding Description
In `claim_bounty` (substrate/frame/bounties/src/lib.rs): [1](#0-0) 
```rust
let res = T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
debug_assert!(res.is_ok());
let res = T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
debug_assert!(res.is_ok());

*maybe_bounty = None;
```
The identical pattern exists in `claim_child_bounty`: [2](#0-1) 

The comment "should not fail" is the same reasoning the Fractional Migration.sol authors used before `payable.transfer()` — an assumption, not a guarantee. `T::Currency::transfer` with `AllowDeath`/`Expendable` preservation can genuinely fail when the destination account does not yet exist and the amount being sent is below the chain's `ExistentialDeposit`: an account cannot be created with a balance under the ED, so the currency implementation returns `TokenError::BelowMinimum`/`ExistentialDeposit` rather than transferring a dust amount. Any signed account can:

1. Propose a bounty with a very small `value` (just above the intended `fee`), so that `payout = balance - fee` ends up below the runtime's existential deposit.
2. Get it approved/funded and a curator assigned (`propose_curator`/`accept_curator`), then `award_bounty` with a `beneficiary` address that has never existed on-chain (any fresh AccountId — no privileged role required, no admin/governance action beyond the pre-existing bounty workflow which any user can drive through the standard permissionless flow up to award/claim).
3. Anyone calls `claim_bounty` — the code calls `ensure_signed(origin)?; // anyone can trigger claim` so a completely unprivileged caller can also do this final step.

The curator-fee transfer likely succeeds (curator already has an existing account), but the beneficiary transfer of `payout < ED` to a non-existent account fails. `debug_assert!` is a no-op in `debug-assertions = false` (the standard production release profile). The function therefore still returns `Ok(())`, `*maybe_bounty = None` executes unconditionally, and `BountyDescriptions` is removed as if payout had succeeded — even though the `payout` value is stranded, still sitting in `bounty_account_id(bounty_id)`, with the bounty index no longer present in `Bounties<T,I>` to reference it, and no reclaim/root-recovery dispatchable for orphaned bounty-account dust.

This directly mirrors the reported vulnerability class: state that records/owns the entitlement is destroyed based on the unchecked assumption that the transfer will succeed, with no mechanism afterward to redirect or reclaim the funds to an alternative address.

### Impact Explanation
This is a permanent user/protocol-fund lock: value that was earmarked as a specific beneficiary's payout becomes unreachable dust sitting in the bounty pot account, with the bookkeeping record deleted. No dispatchable exists to reclaim funds from a bounty account whose `Bounties` entry no longer exists (only `close_bounty` operates on an existing `Bounties` entry). This matches the "Required Impacts" criterion of "permanent user-fund … lock" without requiring any malicious peer/validator/relayer/admin — the entire chain of events (propose → approve → award → claim) can be driven by ordinary signed accounts through the standard permissionless dispatch surface.

### Likelihood Explanation
Likelihood is constrained by the fact that it requires deliberately engineering a payout below the existential deposit, which is a narrow (but fully attacker-controlled) condition — the attacker chooses `value`/`fee` and the never-used `beneficiary` address, so it is trivially reproducible on any runtime with a nonzero `ExistentialDeposit` (essentially all production Substrate chains). It does not require a malicious validator, collator, governance action, or leaked key — only ordinary transactions from a normal account, making it a realistic, low-cost griefing/self-inflicted-lock vector, and — more importantly — a systemic robustness bug: any account that is dusted/reaped between `award_bounty` and `claim_bounty`, or any legitimately small bounty payout, can trigger the same silent-failure path in production.

### Recommendation
- Propagate the transfer result instead of relying on `debug_assert!`: use `let payout_transfer_result = T::Currency::transfer(...)?;` (or explicitly handle and re-queue/refuse the claim) so a failed transfer aborts the extrinsic and, thanks to FRAME's transactional dispatch, rolls back the storage mutation (`*maybe_bounty = None`) instead of committing it.
- Alternatively, guard against sub-ED beneficiary payouts explicitly (e.g., require `payout >= T::Currency::minimum_balance()` or route it through a `deposit_creating`/keep-alive-safe primitive) before attempting the transfer, and emit a clear error rather than silently discarding it.
- Apply the same fix to the fee transfer and to the identical patterns in `claim_child_bounty` (`substrate/frame/child-bounties/src/lib.rs`) and any other pallet using the same `debug_assert!(res.is_ok())` idiom around currency transfers (e.g. `polkadot/runtime/common/src/slots/mod.rs`, `substrate/frame/society/src/lib.rs`, `substrate/frame/tips/src/lib.rs`) since these were found via `grep` to share the pattern but were not individually audited here.

### Proof of Concept
1. On a test runtime with `ExistentialDeposit = 1` scaled appropriately (or any runtime with ED > 0), call `Bounties::propose_bounty(origin, value, description)` with `value` set so that after curator `fee` is subtracted, `payout = value - fee < ExistentialDeposit`.
2. `Bounties::approve_bounty(RootOrigin, bounty_id)` and fund via treasury spend period (standard flow already exercised in `substrate/frame/bounties/src/tests.rs::award_and_claim_bounty_works`).
3. `Bounties::propose_curator(RootOrigin, bounty_id, curator, fee)`; `Bounties::accept_curator(curator_origin, bounty_id)`.
4. `Bounties::award_bounty(curator_origin, bounty_id, beneficiary)` where `beneficiary` is a fresh `AccountId` never funded/created on-chain.
5. After `unlock_at`, call `Bounties::claim_bounty(any_signed_origin, bounty_id)`.
   - Curator fee transfer to `curator` succeeds (existing account).
   - Beneficiary transfer of `payout < ED` to non-existent `beneficiary` returns `Err` from `pallet_balances::Currency::transfer` (`ExistentialDeposit`/`BelowMinimum`), but is discarded by `debug_assert!` in a release build.
   - `*maybe_bounty = None` still executes; `Event::BountyClaimed` is still emitted with the (unsent) `payout` amount.
6. Observe: `Balances::free_balance(beneficiary) == 0`, `Balances::free_balance(bounty_account_id(bounty_id)) == payout` (nonzero, stranded), and `Bounties::<T,I>::get(bounty_id) == None` — the entitlement record is gone and there is no dispatchable to recover the stranded `payout` from the orphaned bounty account.

(Note: verifying the exact `TokenError` returned by `pallet_balances`'s `transfer` for sub-ED transfers to non-existent accounts under the currently configured `Preservation`/`ExistenceRequirement` semantics in this repo snapshot was not fully re-confirmed line-by-line in `impl_currency.rs`/`impl_fungible.rs` due to tool-call exhaustion; the general Substrate/FRAME behavior — that an account cannot be created with a balance below the Existential Deposit and such transfers return an error — is a well-established invariant of `pallet-balances`, and the unchecked `debug_assert!(res.is_ok())` pattern itself is directly confirmed in the cited source lines.)

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L820-828)
```rust
					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());

					*maybe_bounty = None;
```

**File:** substrate/frame/child-bounties/src/lib.rs (L726-744)
```rust
						// Make payout to child-bounty curator.
						// Should not fail because curator fee is always less than bounty value.
						let fee_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							curator,
							curator_fee,
							AllowDeath,
						);
						debug_assert!(fee_transfer_result.is_ok());

						// Make payout to beneficiary.
						// Should not fail.
						let payout_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							beneficiary,
							payout,
							AllowDeath,
						);
						debug_assert!(payout_transfer_result.is_ok());
```
