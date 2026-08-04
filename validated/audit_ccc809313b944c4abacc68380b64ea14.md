## Title
`unbond()` permissionless-kick path can be permanently DoS'd via a blocked reward-pool transfer - (`substrate/frame/nomination-pools/src/lib.rs`)

## Summary
`pallet-nomination-pools`'s `unbond` extrinsic unconditionally calls `Self::do_reward_payout(...)?` before performing the actual unbond/kick logic [1](#0-0) . `do_reward_payout` performs a direct, synchronous `T::Currency::transfer` from the pool's reward account to the member being unbonded, and propagates any transfer error with `?` [2](#0-1) . This mirrors the pattern in the PoolTogether report: a payout is pushed directly to a recipient inside a function that other, unrelated actors depend on, so if the recipient's account cannot accept the transfer, the entire call reverts.

## Finding Description
`unbond` is documented as being callable permissionlessly under specific "kick" conditions — e.g. when the pool is blocked and the caller is the pool's `root` or `bouncer` — precisely so that governance/pool operators can forcibly remove a malicious or unwanted member [3](#0-2) . Before any unbonding state is mutated, the call always attempts to pay out the target member's pending reward via `do_reward_payout`, which does:
```rust
T::Currency::transfer(
    &bonded_pool.reward_account(),
    member_account,
    pending_rewards,
    Preservation::Preserve,
)?;
``` [4](#0-3) 

Because this uses `?`, any failure of the transfer (e.g. the target member's account being blocked/frozen by the underlying `Currency`/asset implementation, or a `Preservation::Preserve` failure due to the destination not meeting existential-deposit-like constraints) causes `do_reward_payout` to return `Err`, which then aborts the entire `unbond` call in `substrate/frame/nomination-pools/src/lib.rs` at line 2288 before the kick/unbond logic executes.

The same forced push-then-propagate pattern exists in the `claim_payout`/`bond_extra` paths as well [5](#0-4) , but those only affect the caller's own funds. The `unbond` permissionless-kick path is different: it is invoked by `root`/`bouncer` *against* a member, not by the member themselves, making it a genuine third-party dependency on the targeted account's ability to receive funds — directly analogous to `RngRelayAuction.rngComplete()` being blocked by a blacklisted `recipient`.

## Impact Explanation
If a pool's runtime configuration backs `T::Currency` with an asset that supports account blocking/freezing (any `fungible::Mutate` implementation with a blacklist-like mechanism, analogous to USDC), a malicious pool member can deliberately make their own account non-receiving. This permanently prevents `root`/`bouncer` from executing the permissionless "kick" via `unbond`, because the mandatory reward payout inside `unbond` will always fail and abort the whole extrinsic. This blocks the only listed mechanism for removing a malicious member from a blocked pool, degrading pool governance and potentially trapping honest members' funds/exposure to that malicious actor indefinitely. This matches the "permanent user-fund or bridge-state lock" and "public underpriced work / stalls processing" impact classes.

## Likelihood Explanation
Exploitability depends entirely on whether a given deployment's nomination-pools `Currency` type can enter a state where transfers to a specific account fail deterministically (e.g. a frozen/blacklisted asset backend). With the plain native `Balances` pallet (no blacklist concept), this path is not exploitable, so likelihood is conditional on runtime configuration — this is the main open uncertainty; I was not able to confirm within this repo whether any current runtime configures `nomination-pools`' `Currency` with a blockable asset. If such a configuration exists (or is later added, e.g. via a fungible-asset backend with account freezing), the bug is a straightforward, unprivileged, self-inflicted-block DoS with no `?`-based recovery path in `unbond`.

## Recommendation
Do not let a reward-payout failure block administrative/kick operations that must succeed for pool integrity. Options:
- In `unbond` (and any permissionless-kick call path), separate "best-effort" reward payout from the core state transition: catch/ignore `do_reward_payout` errors (e.g. via `let _ = Self::do_reward_payout(...)`) when the call is being made non-self (kick context), or skip payout attempts entirely for kicks and let the member claim rewards separately (or forfeit) if their account cannot receive funds.
- Alternatively, adopt a claim-based model for the reward transfer analogous to the recommended fix in the source report: record `pending_rewards` and require the member to explicitly claim it themselves, rather than having admin-triggered code paths push funds to them.

## Proof of Concept
1. Configure (hypothetically) `pallet-nomination-pools`'s `Currency` to a fungible asset implementation that supports blocking accounts from receiving transfers (freeze/blacklist).
2. Member `M` joins a pool and accrues pending rewards.
3. `M` gets their account blocked/frozen for the pool's reward asset (self-inflicted or externally imposed).
4. Pool is later `Blocked`, and the pool's `root`/`bouncer` calls `unbond(M, unbonding_points)` to kick `M` out.
5. `do_reward_payout` attempts `T::Currency::transfer(&reward_account, &M, pending_rewards, Preserve)`, which fails because `M`'s account cannot receive the asset.
6. The `?` in `unbond` (`substrate/frame/nomination-pools/src/lib.rs:2288`) propagates the error, aborting the entire extrinsic — `M` is never unbonded/kicked, and the block/pool remains stuck with `M` inside it indefinitely.

Note: I was unable to verify, within the available index, whether any currently shipped runtime actually wires a blockable/frozen-account-capable asset as the `Currency` for `nomination-pools`; that configuration detail determines real-world exploitability and could not be confirmed from this repository alone.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2224-2244)
```rust
		/// Unbond up to `unbonding_points` of the `member_account`'s funds from the pool. It
		/// implicitly collects the rewards one last time, since not doing so would mean some
		/// rewards would be forfeited.
		///
		/// Under certain conditions, this call can be dispatched permissionlessly (i.e. by any
		/// account).
		///
		/// # Conditions for a permissionless dispatch.
		///
		/// * The pool is blocked and the caller is either the root or bouncer. This is refereed to
		///   as a kick.
		/// * The pool is destroying and the member is not the depositor.
		/// * The pool is destroying, the member is the depositor and no other members are in the
		///   pool.
		///
		/// ## Conditions for permissioned dispatch (i.e. the caller is also the
		/// `member_account`):
		///
		/// * The caller is not the depositor.
		/// * The caller is the depositor, the pool is destroying and no other members are in the
		///   pool.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2275-2288)
```rust
			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3552-3563)
```rust
		// IFF the reward is non-zero alter the member and reward pool info.
		member.last_recorded_reward_counter = current_reward_counter;
		reward_pool.register_claimed_reward(pending_rewards);

		T::Currency::transfer(
			&bonded_pool.reward_account(),
			member_account,
			pending_rewards,
			// defensive: the depositor has put existential deposit into the pool and it stays
			// untouched, reward account shall not die.
			Preservation::Preserve,
		)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3753-3769)
```rust
	pub(crate) fn do_claim_payout(
		signer: T::AccountId,
		member_account: T::AccountId,
	) -> DispatchResult {
		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_claim_payout(),
				Error::<T>::DoesNotHavePermission
			);
		}
		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?;

		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);
		Ok(())
```
