### Title
`ProxyType::Governance` unrestricted `ConvictionVoting` pass-through lets a proxy delegate lock the delegator's entire balance far beyond the proxy relationship - ([File: cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs])

### Summary
`TemporaryHoldings.sol`'s bug class is: a time-boxed, limited-authority actor (`beneficiary`) is allowed to call a "whitelisted" external contract through a generic pass-through (`execute`), and one of the whitelisted targets accepts an attacker-supplied duration parameter that is never validated against the outer authority window, letting the limited actor create a lock that outlives the intended control period and that the rightful `admin` cannot undo. The same broken invariant — *a generic call-forwarding wrapper whitelists an entire pallet without capping the duration/conviction parameters that pallet exposes* — exists in `pallet-proxy`'s `Governance` proxy type combined with `pallet-conviction-voting`'s `delegate`/`undelegate` lock mechanics.

### Finding Description
On Asset Hub, `ProxyType::Governance` is defined to allow **any** `ConvictionVoting` call to pass through the proxy filter unrestricted: [1](#0-0) 

This is the same shape as `TemporaryHoldings.execute()`, which whitelists `GatewayRegistry` as a target without constraining the `stake`/`extend` duration argument. Here, the proxy filter whitelists the entire `ConvictionVoting` pallet without constraining the `conviction` or `balance` arguments of `delegate`.

`ConvictionVoting::delegate` lets the caller (acting as the proxied account) lock up to the account's full balance with `Conviction::Locked6x`, and `ConvictionVoting::undelegate` then commits that lock for `32 * VoteLockingPeriod` blocks: [2](#0-1) [3](#0-2) 

The lock is enforced with `T::Currency::set_lock`/`extend_lock` using `WithdrawReasons::except(RESERVE)`, i.e. it blocks transfers of the locked balance: [4](#0-3) 

Because a `Governance` proxy delegate can call `delegate(class, target, Conviction::Locked6x, full_balance)` followed immediately by `undelegate(class)` on behalf of the proxied account, they can commit the proxied account's entire spendable balance to a lock lasting `32 * VoteLockingPeriod` (e.g. 7 or 30 days per era on the runtimes in this repo, i.e. up to ~6.4–30.7 months) — a duration the delegate fully controls via the `conviction` parameter, exactly like the attacker-controlled `durationBlocks` in the Solidity report. Just as `TemporaryHoldings._canExecute` returning control to `admin` after `lockedUntil` doesn't help because the stake inside `GatewayRegistry` is independently timed, revoking the `Governance` proxy (`remove_proxy`) does **not** unwind the conviction-voting lock: the lock lives in `VotingFor`/`ClassLocksFor` storage on the delegator's own account and is only released by `PriorLock::rejig` once the target block passes: [5](#0-4) 

The `Governance` proxy is intended for temporary governance participation (voting/treasury spends), not for locking the entire principal balance; existing guards (proxy filter, delay/announcement) do not check or cap the `conviction`/`balance` arguments passed to `delegate`, so nothing stops a delegate from maximizing both.

### Impact Explanation
A user who grants a `Governance` proxy (a common configuration for governance-participation delegation, e.g. giving a "governance bot" or advisor limited rights) can have their entire liquid balance frozen for the maximum conviction lock period by that delegate, with no way for the account owner to reclaim it early even after revoking the proxy. This is a permanent (until natural expiry, effectively unbounded from the user's perspective for governance/voting/spending purposes) fund lock caused by a public-entrypoint (`pallet-proxy`) filter that widens the delegate's origin beyond the intended "vote/spend within scope" use case to "lock 100% of my balance for years."

### Likelihood Explanation
Medium: it requires the victim to have granted a `Governance` proxy to another account (a standard, documented delegation pattern), and the counterparty (a non-privileged, ordinary delegate — not a validator/collator/admin) to act adversarially by calling `delegate` + `undelegate` with maximum conviction on the victim's behalf. No governance action, no validator/collator compromise, and no leaked keys are required — only the ordinary permissions already granted via `pallet-proxy`.

### Recommendation
Restrict `ProxyType::Governance`'s `ConvictionVoting` pass-through to calls that cannot commit the delegator's full balance under maximum conviction without further consent, e.g.:
- Disallow `ConvictionVoting::delegate`/`undelegate` (and `vote` with high conviction) under `ProxyType::Governance`, requiring a dedicated, more restrictive proxy type for conviction delegation with a bounded conviction/amount, or
- Require the underlying pallet to bound delegated conviction/lock duration to some value tied to a proxy-level cap, mirroring the report's suggested fix of validating the "duration" parameter before allowing the whitelisted call through the generic pass-through.

### Proof of Concept
1. Victim `V` (on Asset Hub) creates a proxy: `Proxy::add_proxy(V, delegate=D, proxy_type=Governance, delay=0)`.
2. `D` calls `Proxy::proxy(V, None, ConvictionVoting::delegate(class, target=D_or_other, conviction=Locked6x, balance=V's_full_balance))`. This passes the `Governance` filter since it only checks `RuntimeCall::ConvictionVoting(..)` [1](#0-0) .
3. `D` immediately calls `Proxy::proxy(V, None, ConvictionVoting::undelegate(class))`, which computes `unlock_block = now + VoteLockingPeriod * 32` and calls `prior.accumulate(unlock_block, balance)` [6](#0-5) , locking `V`'s entire delegated balance via `T::Currency::set_lock`.
4. `V` calls `Proxy::remove_proxy(D, Governance, 0)` to revoke `D`'s access — the currency lock on `V`'s account is unaffected and remains until `unlock_block` is reached, at which point `unlock`/`update_lock` can finally release it [7](#0-6) .
5. `V`'s balance (up to full free balance) remains unusable for transfers for `32 * VoteLockingPeriod` blocks, entirely at `D`'s discretion, with no recourse for `V`.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L938-945)
```rust
			ProxyType::Governance => matches!(
				c,
				RuntimeCall::Treasury(..) |
					RuntimeCall::Utility(..) |
					RuntimeCall::ConvictionVoting(..) |
					RuntimeCall::Referenda(..) |
					RuntimeCall::Whitelist(..)
			),
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L669-706)
```rust
	/// Attempt to end the current delegation.
	///
	/// Return the number of votes of upstream.
	fn try_undelegate(who: T::AccountId, class: ClassOf<T, I>) -> Result<u32, DispatchError> {
		let votes =
			VotingFor::<T, I>::try_mutate(&who, &class, |voting| -> Result<u32, DispatchError> {
				match core::mem::replace(voting, Voting::default()) {
					Voting::Delegating(Delegating {
						balance,
						target,
						conviction,
						delegations,
						mut prior,
					}) => {
						// remove any delegation votes to our current target.
						let votes = Self::reduce_upstream_delegation(
							&target,
							&class,
							conviction.votes(balance),
						);
						let now = T::BlockNumberProvider::current_block_number();
						let lock_periods = conviction.lock_periods().into();
						prior.accumulate(
							now.saturating_add(
								T::VoteLockingPeriod::get().saturating_mul(lock_periods),
							),
							balance,
						);
						voting.set_common(delegations, prior);

						Ok(votes)
					},
					Voting::Casting(_) => Err(Error::<T, I>::NotDelegating.into()),
				}
			})?;
		Self::deposit_event(Event::<T, I>::Undelegated(who, class));
		Ok(votes)
	}
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L708-729)
```rust
	fn extend_lock(who: &T::AccountId, class: &ClassOf<T, I>, amount: BalanceOf<T, I>) {
		ClassLocksFor::<T, I>::mutate(who, |locks| {
			match locks.iter().position(|x| &x.0 == class) {
				Some(i) => locks[i].1 = locks[i].1.max(amount),
				None => {
					let ok = locks.try_push((class.clone(), amount)).is_ok();
					debug_assert!(
						ok,
						"Vec bounded by number of classes; \
						all items in Vec associated with a unique class; \
						qed"
					);
				},
			}
		});
		T::Currency::extend_lock(
			CONVICTION_VOTING_ID,
			who,
			amount,
			WithdrawReasons::except(WithdrawReasons::RESERVE),
		);
	}
```

**File:** substrate/frame/conviction-voting/src/lib.rs (L731-761)
```rust
	/// Rejig the lock on an account. It will never get more stringent (since that would indicate
	/// a security hole) but may be reduced from what they are currently.
	fn update_lock(class: &ClassOf<T, I>, who: &T::AccountId) {
		let class_lock_needed = VotingFor::<T, I>::mutate(who, class, |voting| {
			voting.rejig(T::BlockNumberProvider::current_block_number());
			voting.locked_balance()
		});
		let lock_needed = ClassLocksFor::<T, I>::mutate(who, |locks| {
			locks.retain(|x| &x.0 != class);
			if !class_lock_needed.is_zero() {
				let ok = locks.try_push((class.clone(), class_lock_needed)).is_ok();
				debug_assert!(
					ok,
					"Vec bounded by number of classes; \
					all items in Vec associated with a unique class; \
					qed"
				);
			}
			locks.iter().map(|x| x.1).max().unwrap_or(Zero::zero())
		});
		if lock_needed.is_zero() {
			T::Currency::remove_lock(CONVICTION_VOTING_ID, who);
		} else {
			T::Currency::set_lock(
				CONVICTION_VOTING_ID,
				who,
				lock_needed,
				WithdrawReasons::except(WithdrawReasons::RESERVE),
			);
		}
	}
```

**File:** substrate/frame/conviction-voting/src/conviction.rs (L97-110)
```rust
impl Conviction {
	/// The amount of time (in number of periods) that our conviction implies a successful voter's
	/// balance should be locked for.
	pub fn lock_periods(self) -> u32 {
		match self {
			Conviction::None => 0,
			Conviction::Locked1x => 1,
			Conviction::Locked2x => 2,
			Conviction::Locked3x => 4,
			Conviction::Locked4x => 8,
			Conviction::Locked5x => 16,
			Conviction::Locked6x => 32,
		}
	}
```

**File:** substrate/frame/conviction-voting/src/vote.rs (L171-188)
```rust
impl<BlockNumber: Ord + Copy + Zero, Balance: Ord + Copy + Zero> PriorLock<BlockNumber, Balance> {
	/// Accumulates an additional lock.
	pub fn accumulate(&mut self, until: BlockNumber, amount: Balance) {
		self.0 = self.0.max(until);
		self.1 = self.1.max(amount);
	}

	pub fn locked(&self) -> Balance {
		self.1
	}

	pub fn rejig(&mut self, now: BlockNumber) {
		if now >= self.0 {
			self.0 = Zero::zero();
			self.1 = Zero::zero();
		}
	}
}
```
