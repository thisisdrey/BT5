## Title
On-demand revenue for a timeslice with zero InstaPool contributions is silently dropped and permanently stranded in the Broker pallet account - ([File: substrate/frame/broker/src/tick_impls.rs])

### Summary
`Pallet::process_revenue` in `pallet-broker` converts relay-chain on-demand core revenue into local balance and attempts to pay it out to system/private InstaPool contributors for the corresponding timeslice. When a timeslice has **no InstaPool contributions at all** (`system_contributions == 0` and `private_contributions == 0`), the function takes the "no claimants" branch, discards the `InstaPoolHistory` record and emits `HistoryDropped`, without ever crediting the amount to any account or claimant. The underlying tokens, however, were already minted/teleported into the Broker pallet's pot account when the revenue was received from the relay chain, so they remain in that account with no code path (permissionless or governance) that lets them be recovered or re-attributed. This mirrors the reported `SpiceAuction` bug class: value that is nominally owed to "no one" (an auction with no bids / a timeslice with no pool participants) becomes permanently locked because the settlement logic has no fallback recovery path.

### Finding Description
`process_revenue` is called once per timeslice from `do_tick` (`substrate/frame/broker/src/tick_impls.rs:35-86`) and is entirely permissionless in effect — it runs automatically as part of block-by-block pallet ticking, driven only by relay-chain-reported revenue arriving in `RevenueInbox`. [1](#0-0) 

The relevant logic:
```
let total_contrib = r.system_contributions.saturating_add(r.private_contributions);
let system_payout = if !total_contrib.is_zero() {
    let system_payout = revenue.saturating_mul(r.system_contributions.into()) / total_contrib.into();
    Self::charge(&Self::account_id(), system_payout).defensive_ok();
    revenue.saturating_reduce(system_payout);
    system_payout
} else {
    Zero::zero()
};

if !revenue.is_zero() && r.private_contributions > 0 {
    r.maybe_payout = Some(revenue);
    InstaPoolHistory::<T>::insert(when, &r);
    Self::deposit_event(Event::<T>::ClaimsReady { when, system_payout, private_payout: revenue });
} else {
    InstaPoolHistory::<T>::remove(when);
    Self::deposit_event(Event::<T>::HistoryDropped { when, revenue });
}
```

If `total_contrib` is zero (i.e. no core was pooled into the InstaPool during that timeslice — the analog of an "auction with no bids"), then:
- `system_payout` is `Zero::zero()`, so `Self::charge` is never called and nothing is credited to the system pot.
- `r.private_contributions` is `0` (since `total_contrib == system_contributions + private_contributions == 0` implies both are 0), so the `ClaimsReady` branch is skipped.
- The function falls into the `else` branch: it removes the `InstaPoolHistory` entry and emits `HistoryDropped` for the *entire* `revenue` amount, without transferring it anywhere.

The revenue value itself was already brought on-chain before this accounting step runs (the amount arrives via `RevenueInbox`/`OnDemandRevenueRecord`, populated from relay-chain revenue reports and settled through the pallet's teleport/mint machinery into the Broker pallet's account, `Self::account_id()`). Because `process_revenue`'s only job is bookkeeping over already-received funds, dropping the `InstaPoolHistory` record does not return, burn, or redirect the tokens — it just stops tracking who (if anyone) is owed them. There is no `recoverToken`-style function, no governance call, and no other dispatchable in `substrate/frame/broker/src/dispatchable_impls.rs` that sweeps stranded/unclaimed InstaPool revenue back to a beneficiary (e.g. treasury) when `total_contrib == 0`. The condition is reachable whenever a sale period rotates or a timeslice ticks with zero cores placed into the pool (which is a normal, permissionless-triggerable operational state, not requiring any privileged action, malicious peer, or admin misbehavior).

### Impact Explanation
This causes permanent, unbacked accounting loss of on-demand coretime revenue: funds are teleported/minted onto the parachain and held in the Broker pallet's account, but the bookkeeping record that would let `do_claim_revenue` (dispatchable_impls.rs) pay them out to any contributor is deleted. Because no contributor claim exists for that timeslice, and no code path allows the treasury or governance to sweep this specific stranded balance, the tokens are effectively locked in the pallet account forever — a "permanent user-fund or bridge-state lock" as defined in the impact gate. Over many timeslices with intermittently zero InstaPool contributions, this can silently accumulate un-recoverable balance inside the Broker pot, degrading the ability to fully account for and distribute Coretime revenue as intended by protocol design.

### Likelihood Explanation
This is not an edge case requiring an attacker: it triggers deterministically any time a timeslice elapses in which nobody (neither system reservations nor private buyers) placed a core into the InstaPool while on-demand revenue for that timeslice is still non-zero (e.g., due to timing/ordering between region purchases, pool contributions expiring, or partitioning as seen in the related test `instapool_payouts_cannot_be_duplicated_through_partition`). No special privileges, malicious relayer, or governance action are needed — it is a normal consequence of the automatic `do_tick`/`process_revenue` flow processing relay-chain-reported revenue against pool-contribution state.

### Recommendation
Add a fallback settlement path in `process_revenue` (or a separate permissionless dispatchable, mirroring the `reclaim_bounty_funds`/pool "trapped balance" fixes already present elsewhere in this codebase) so that when `total_contrib.is_zero()`, the un-attributable revenue is not silently dropped but is instead swept to a well-defined beneficiary (e.g., the system InstaPool account, treasury, or burned deterministically) instead of being left unaccounted for in the Broker pallet's account with no record and no recovery mechanism.

### Proof of Concept
1. Reserve/offer coretime cores via `do_reserve`/`do_start_sales`, but ensure no core is ever placed into the InstaPool for a specific timeslice `T` (`InstaPoolIo`/contributions remain zero for `T`).
2. Let on-demand orders be placed against the on-demand assignment provider so that relay-chain revenue accrues for timeslice `T` and is reported into `RevenueInbox` via `OnDemandRevenueRecord { until, amount }` (mirrors the existing `coretime_revenue.rs` zombienet test flow at `polkadot/zombienet-sdk-tests/tests/smoke/coretime_revenue.rs:463-489`, but skip the "Alice contributes to pool" step).
3. Allow `do_tick` to run at the timeslice boundary, invoking `process_revenue()` in `substrate/frame/broker/src/tick_impls.rs:97-151`.
4. Observe: `system_contributions == 0` and `private_contributions == 0` ⇒ `total_contrib.is_zero()` ⇒ `system_payout = 0` and the private-payout branch is skipped ⇒ `InstaPoolHistory::<T>::remove(when)` and `Event::HistoryDropped { when, revenue }` fire, with `revenue > 0`.
5. Verify no subsequent call (`do_claim_revenue`, or any other dispatchable) can recover this amount — the pallet account's balance corresponding to `revenue` remains, but there is no storage record referencing it and no extrinsic to reclaim it, permanently stranding the funds.

### Citations

**File:** substrate/frame/broker/src/tick_impls.rs (L97-151)
```rust
	pub(crate) fn process_revenue() -> bool {
		let Some(OnDemandRevenueRecord { until, amount }) = RevenueInbox::<T>::take() else {
			return false;
		};
		let when: Timeslice =
			(until / T::TimeslicePeriod::get()).saturating_sub(One::one()).saturated_into();
		let mut revenue = T::ConvertBalance::convert_back(amount.clone());
		if revenue.is_zero() {
			Self::deposit_event(Event::<T>::HistoryDropped { when, revenue });
			InstaPoolHistory::<T>::remove(when);
			return true;
		}

		log::debug!(
			target: "pallet_broker::process_revenue",
			"Received {amount:?} from RC, converted into {revenue:?} revenue",
		);

		let mut r = InstaPoolHistory::<T>::get(when).unwrap_or_default();
		if r.maybe_payout.is_some() {
			Self::deposit_event(Event::<T>::HistoryIgnored { when, revenue });
			return true;
		}
		// Payout system InstaPool Cores.
		let total_contrib = r.system_contributions.saturating_add(r.private_contributions);
		let system_payout = if !total_contrib.is_zero() {
			let system_payout =
				revenue.saturating_mul(r.system_contributions.into()) / total_contrib.into();
			Self::charge(&Self::account_id(), system_payout).defensive_ok();
			revenue.saturating_reduce(system_payout);

			system_payout
		} else {
			Zero::zero()
		};

		log::debug!(
			target: "pallet_broker::process_revenue",
			"Charged {system_payout:?} for system payouts, {revenue:?} remaining for private contributions",
		);

		if !revenue.is_zero() && r.private_contributions > 0 {
			r.maybe_payout = Some(revenue);
			InstaPoolHistory::<T>::insert(when, &r);
			Self::deposit_event(Event::<T>::ClaimsReady {
				when,
				system_payout,
				private_payout: revenue,
			});
		} else {
			InstaPoolHistory::<T>::remove(when);
			Self::deposit_event(Event::<T>::HistoryDropped { when, revenue });
		}
		true
	}
```
