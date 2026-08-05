## Title
Exponential backoff lock in `pallet-scarcity`'s `AsScarcity` extension can saturate to a permanent lockout, with no unlock or challenge-response recovery path - (File: `substrate/frame/scarcity/src/extension.rs`)

### Summary
The RocketPool report's core broken invariant is: a state-transition (`ActionChallengeMade`) sets a time-bounded lock/challenge, but the codebase never implements a path to resolve or recover from it, so funds/state become permanently stuck. `pallet-scarcity` has a structurally identical pattern: a failed dispatch through the `AsScarcity` transaction extension sets a `LockInfo.until` timestamp computed by an **unbounded exponential backoff** with no cap on the exponent's effect and no unlock extrinsic exposed to the affected purse key. An attacker who can force repeated dispatch failures against a victim's purse key can escalate the lock delay far beyond any realistic epoch, permanently denying the victim the ability to move or burn their own NFT via the intended feeless path.

### Finding Description
`AsScarcity::failed_dispatch_lock` computes the next lock expiry as: [1](#0-0) 

```rust
fn failed_dispatch_lock(previous: Option<LockInfo>) -> LockInfo {
    let retries = previous.map(|lock| lock.retries.saturating_add(1)).unwrap_or(1);
    let exponent = retries.saturating_sub(1);
    let multiplier = 2u64.saturating_pow(u32::from(exponent).min(63));
    LockInfo {
        retries,
        until: T::UnixTime::now()
            .as_secs()
            .saturating_add(multiplier.saturating_mul(T::LockPeriod::get())),
    }
}
```

`retries` is a `u8`; after only ~40 consecutive failed dispatches, `multiplier = 2^exponent` already reaches astronomically large values (`2^39 ≈ 5.5×10^11`), and `multiplier.saturating_mul(T::LockPeriod::get())` saturates to `u64::MAX` well before `retries` even approaches its `u8::MAX` ceiling. `T::UnixTime::now().saturating_add(u64::MAX)` then yields `until = u64::MAX` seconds — a lock that can never expire within any realistic chain lifetime.

`post_dispatch_details` calls this unconditionally on any dispatch error and stores the result with no ceiling: [2](#0-1) 

`validate()` then permanently rejects any further use of the purse key while `lock.until > now`: [3](#0-2) 

Just like the Rocket Pool DAO challenge (`actionChallengeMake`/`actionChallengeDecide`), the pallet has:
- No monitoring/response process analogous to `ActionChallengeMade` — nothing observes repeated failures and intervenes.
- No `actionChallengeDecide`-equivalent extrinsic to clear or reduce `Locked::<T>` for the affected key. The only path that clears the lock is a **successful** dispatch (`Locked::<T>::remove(&owner)` on `Ok`), which is unreachable once `until` has saturated.
- The exponential growth is not bounded by any `min()`/cap relative to `T::LockPeriod`, unlike the `members.challenge.window` fixed default in the original report — here the growth is literally unbounded (saturates to the max representable value), which is strictly worse than a fixed 7-day window that at least eventually expires.

An attacker does not need to control the purse key, a validator, collator, or any privileged role. Race-based griefing is possible because `validate()` only pre-checks that the destination is currently free, while the actual failure occurs at dispatch time if the destination becomes occupied in between (the pallet's own test explicitly documents this race as "the only failure path that still reaches dispatch"): [4](#0-3) 

By repeatedly minting/transferring an NFT into the victim's intended destination address just before each of the victim's transfer attempts is included in a block, an attacker can force `AddressOccupied` dispatch failures against the victim over and over, ratcheting `retries` and thus `until` toward saturation — with no ceremony, no signature from the victim's key required to grief them, and no on-chain remedy.

### Impact Explanation
This is a public-entrypoint griefing vector that permanently locks a user's asset out of its intended feeless, self-custodial transfer/burn mechanism — matching the "permanent user-fund or bridge-state lock" impact category. Once `until` saturates near `u64::MAX`, the purse key can never again satisfy `lock.until > now` becoming false, so the holder can never transfer or burn through `AsScarcity` again. The only theoretical remedy is a privileged collection-owner force-transfer/force-burn (a different, trusted code path), which defeats the pallet's stated design goal of "coinage-style" self-custodial purse-key ownership without any System account or balance.

### Likelihood Explanation
Likelihood is high for a targeted griefing attack: the attacker only needs to observe the victim's pending transfer transactions in the transaction pool/mempool (a standard front-running primitive, not privileged access) and race a `mint`/`transfer` to occupy the destination address before each dispatch. No validator, relayer, or admin collusion is required — this is a purely public-entrypoint, unprivileged attack achievable by any account able to submit ordinary `Scarcity` transactions. Because the backoff multiplier saturates after only tens of forced failures, the number of races an attacker needs is small and bounded.

### Recommendation
- Cap `LockInfo.until` growth to a fixed maximum bound (e.g., `T::LockPeriod::get().saturating_mul(some_bounded_max)`) rather than allowing unbounded exponent-driven saturation.
- Add an explicit unlock/reset mechanism analogous to `actionChallengeDecide` — e.g., allow the purse-key holder to clear or reduce their own lock through a fresh, cheap proof of continued control (a bounded manual "unlock" extrinsic), rather than relying solely on eventual dispatch success.
- Consider treating `AddressOccupied` at dispatch-time as a lower-severity failure (e.g., capped/no backoff increase) since `validate()` already pre-checks destination occupancy and this failure mode is attacker-inducible via racing rather than being purely victim error.

### Proof of Concept
1. Victim owns an NFT bound to purse key `V` and submits `Scarcity::transfer { to: D }` authorized via `AsScarcity`.
2. Attacker observes the pending transaction and submits a competing transaction that mints/transfers an NFT into `D` so it lands in the same or an earlier block, before the victim's transfer dispatches.
3. Victim's dispatch fails with `AddressOccupied`; `post_dispatch_details` calls `failed_dispatch_lock`, setting `Locked::<T>::get(V) = LockInfo { retries: 1, until: now + LockPeriod }` per [5](#0-4) .
4. Attacker repeats step 2 against every subsequent retry by `V` (choosing a fresh always-free-then-occupied destination each time, or repeating on the same one). After roughly 40 repetitions, `multiplier = 2^39` times `LockPeriod` saturates `until` to effectively `u64::MAX`.
5. `V` can never again pass the `lock.until > now` check in `validate()` ( [3](#0-2) ), permanently losing use of the feeless purse-key transfer/burn path for their NFT, with no unlock extrinsic available to recover.

### Citations

**File:** substrate/frame/scarcity/src/extension.rs (L163-173)
```rust
	fn failed_dispatch_lock(previous: Option<LockInfo>) -> LockInfo {
		let retries = previous.map(|lock| lock.retries.saturating_add(1)).unwrap_or(1);
		let exponent = retries.saturating_sub(1);
		let multiplier = 2u64.saturating_pow(u32::from(exponent).min(63));
		LockInfo {
			retries,
			until: T::UnixTime::now()
				.as_secs()
				.saturating_add(multiplier.saturating_mul(T::LockPeriod::get())),
		}
	}
```

**File:** substrate/frame/scarcity/src/extension.rs (L219-224)
```rust
		let now = T::UnixTime::now().as_secs();
		if let Some(lock) = Locked::<T>::get(&owner) {
			if lock.until > now {
				return Err(CustomInvalidity::NftTemporarilyLocked.into());
			}
		}
```

**File:** substrate/frame/scarcity/src/extension.rs (L283-299)
```rust
	fn post_dispatch_details(
		pre: Self::Pre,
		_info: &DispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_post_info: &PostDispatchInfoOf<<T as frame_system::Config>::RuntimeCall>,
		_len: usize,
		result: &DispatchResult,
	) -> Result<Weight, TransactionValidityError> {
		if let Pre::UsingNft { owner, nft } = pre {
			if result.is_err() {
				NftsByOwner::<T>::insert(&owner, nft);
				Locked::<T>::insert(&owner, Self::failed_dispatch_lock(Locked::<T>::get(&owner)));
			} else {
				Locked::<T>::remove(&owner);
			}
		}
		Ok(Weight::zero())
	}
```

**File:** substrate/frame/scarcity/src/tests.rs (L1278-1296)
```rust
#[test]
fn failed_dispatch_restores_and_locks() {
	new_test_ext().execute_with(|| {
		setup_item();
		define(0);
		mint(0, OWNER);

		// Race shape: the destination is empty at validation time and becomes occupied before
		// dispatch — the only failure path that still reaches dispatch now that validate
		// pre-checks the destination.
		let (_, val, origin) = validate_transfer(OWNER, 4).unwrap();
		mint(1, 4);
		let pre = prepare_transfer(val, &origin, 4);
		let dispatch = Scarcity::transfer(origin, 4);
		assert_noop!(dispatch, Error::<Test>::AddressOccupied);
		post_dispatch(pre, Err(Error::<Test>::AddressOccupied.into()));
		assert_eq!(NftsByOwner::<Test>::get(OWNER).map(|nft| nft.instance), Some(0));
		assert_eq!(Locked::<Test>::get(OWNER), Some(LockInfo { retries: 1, until: 60 }));
		assert_ok!(Scarcity::do_try_state());
```
