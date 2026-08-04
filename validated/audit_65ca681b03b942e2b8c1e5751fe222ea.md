### Title
`bond_extra`/`claim_payout` force an unrelated reward-payout transfer that reverts and DoSes pool deposits when the reward account is frozen/near-ED - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
In `pallet-nomination-pools`, `Pallet::do_bond_extra` (`bond_extra`/`bond_extra_other` dispatchables) and `Pallet::do_claim_payout` both unconditionally invoke `Self::do_reward_payout` before performing the member's requested action [1](#0-0) . `do_reward_payout` performs a `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)` [2](#0-1) . This mirrors the reported StakedEXA bug class exactly: a public deposit-style entrypoint is coupled to an unrelated "harvest"/claim action against an external-state-dependent account, and if that side transfer fails, the whole deposit reverts.

### Finding Description
`do_reward_payout` is not itself guarded against transfer failure conditions in the reward account: it uses `Preservation::Preserve`, meaning the transfer must leave the reward account above its existential/frozen threshold. The reward account additionally carries a `FreezeReason::PoolMinBalance` freeze via `freeze_pool_deposit` (seen in `do_create`/`do_adjust_pool_deposit`) [3](#0-2) . Whenever the reward account's spendable (non-frozen) balance cannot cover both the ED-preserving requirement and `pending_rewards` — e.g. after an ED increase (a scenario the pallet itself models as `Deficit` in `tests.rs::ed_change_causes_reward_deficit`) — the `T::Currency::transfer(...)?` inside `do_reward_payout` returns an error and propagates via `?` out of `do_bond_extra`/`do_claim_payout`.

Because `bond_extra` always claims pending rewards first ("Bonding extra funds implies an automatic payout of all pending rewards as well" — per the call's own doc comment) [4](#0-3) , there is no way for a member to add `FreeBalance` to their bond while the pool has any non-zero pending reward and the reward account is in a deficit/frozen state — the deposit path is blocked purely by the coupled, unrelated reward-claim step, exactly as in the StakedEXA `harvest()`-blocks-`deposit()` pattern. The pallet's own test `ed_change_causes_reward_deficit` demonstrates the deficit condition can arise from a routine ED change with no admin/attacker action needed, and separately `smallest_claimable_reward` shows `pending_rewards.is_zero()` is the only skip condition in `do_reward_payout` — a non-zero pending reward always attempts the transfer [5](#0-4) .

Existing guards do not stop this: `do_bond_extra` only checks `T::Filter::contains`, `ClaimPermissions`, and pool existence before calling `do_reward_payout` [6](#0-5)  — none of them validate that the reward account has sufficient spendable balance to satisfy the forced payout.

### Impact Explanation
This blocks a normal, permissionless user action (`bond_extra` with `BondExtra::FreeBalance`) as a side effect of a coupled reward-claim transfer that can fail for reasons unrelated to the deposit itself (reward-account balance/ED state). This matches the accepted "public underpriced work/DoS deposits leading to loss of yield" impact category: members who want to compound or add stake are stuck, and any pool member action that must first pay pending rewards (bond_extra, claim_payout, and internally unbond/withdraw paths that call `do_reward_payout`) is similarly impacted, freezing legitimate stake/reward flows until the reward pool deficit is manually repaired via `adjust_pool_deposit`.

### Likelihood Explanation
The deficit state is not attacker-controlled maliciousness — it is a normal runtime-parameter effect (ED increase) explicitly tested in-repo (`ed_change_causes_reward_deficit`), so it can occur during ordinary chain operation without any privileged or malicious actor, satisfying the "no malicious peer/admin" requirement. Any member with nonzero pending rewards calling `bond_extra` while the reward pool is in deficit will hit this revert deterministically.

### Recommendation
Decouple the mandatory reward-claim from the deposit path, or make `do_reward_payout`'s failure non-fatal to `do_bond_extra`/`do_claim_payout` for the `FreeBalance` deposit case: e.g. catch/ignore (with an event) a failed reward transfer when the caller's intent is only to add `FreeBalance`, or require the reward account to top up (via `adjust_pool_deposit`-style logic) before executing pending payouts, rather than letting the transfer failure bubble up and abort the entire bonding operation.

### Proof of Concept
Extend the existing pallet test `ed_change_causes_reward_deficit` (`substrate/frame/nomination-pools/src/tests.rs:320-394`): after `ExistentialDeposit::set(50)` produces `reward_imbalance(1) == Deficit(45)`, have member `11` (who has pending rewards of `90`) call `Pools::bond_extra(RuntimeOrigin::signed(11), BondExtra::FreeBalance(10))`. Because `do_bond_extra` calls `do_reward_payout` first, and the reward account cannot satisfy `Preservation::Preserve` for the full pending payout while in deficit, the `T::Currency::transfer` inside `do_reward_payout` returns `Err`, causing `bond_extra` to revert entirely — demonstrating that member 11's otherwise valid `FreeBalance` deposit of `10` is blocked by the unrelated forced reward claim.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2176-2182)
```rust
		/// Bond `extra` more funds from `origin` into the pool to which they already belong.
		///
		/// Additional funds can come from either the free balance of the account, of from the
		/// accumulated rewards, see [`BondExtra`].
		///
		/// Bonding extra funds implies an automatic payout of all pending rewards as well.
		/// See `bond_extra_other` to bond pending rewards of `other` members.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3547-3550)
```rust
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3556-3563)
```rust
		T::Currency::transfer(
			&bonded_pool.reward_account(),
			member_account,
			pending_rewards,
			// defensive: the depositor has put existential deposit into the pool and it stays
			// untouched, reward account shall not die.
			Preservation::Preserve,
		)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3607-3616)
```rust
		// Transfer the minimum balance for the reward account.
		T::Currency::transfer(
			&who,
			&bonded_pool.reward_account(),
			T::Currency::minimum_balance(),
			Preservation::Expendable,
		)?;

		// Restrict reward account balance from going below ED.
		Self::freeze_pool_deposit(&bonded_pool.reward_account())?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3657-3670)
```rust
		// ensure account is not restricted from joining the pool.
		ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted);

		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_bond_extra(),
				Error::<T>::DoesNotHavePermission
			);
			ensure!(extra == BondExtra::Rewards, Error::<T>::BondExtraRestricted);
		}

		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3671-3683)
```rust
		// payout related stuff: we must claim the payouts, and updated recorded payout data
		// before updating the bonded pool points, similar to that of `join` transaction.
		reward_pool.update_records(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
		let claimed = Self::do_reward_payout(
			&member_account,
			&mut member,
			&mut bonded_pool,
			&mut reward_pool,
		)?;
```
