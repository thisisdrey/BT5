## Title
Stale `announce`d proxy calls survive `remove_proxy` and can be executed against re-authorized delegates - (File: `substrate/frame/proxy/src/lib.rs`)

## Summary
`pallet_proxy`'s delayed-announcement mechanism (`announce` / `proxy_announced`) stores pending calls keyed only by `(delegate, real, call_hash)`, with no linkage to the specific `ProxyDefinition` (proxy type/delay) that existed when the call was announced. `remove_proxy`/`remove_proxy_delegate` deletes the `ProxyDefinition` from `Proxies` storage but never purges matching entries from `Announcements`. This mirrors the CoreWallet bug class: an authorization artifact (signed message / here, an announced call) is not invalidated or re-bound when the underlying role relationship is revoked, so it becomes replayable the moment the role is re-established, bypassing the very delay-review protection the mechanism exists to provide.

## Finding Description
`Pallet::announce` (`substrate/frame/proxy/src/lib.rs:436-472`) lets any current delegate of `real` publish an `Announcement { real, call_hash, height }` into `Announcements::<T>` keyed by `delegate`: [1](#0-0) 

At execution time, `proxy_announced` (`substrate/frame/proxy/src/lib.rs:550-574`) re-derives the *current* `ProxyDefinition` via `find_proxy(&real, &delegate, force_proxy_type)` and only checks the announcement's `real`/`call_hash`/elapsed-delay: [2](#0-1) 

`remove_proxy_delegate` (`substrate/frame/proxy/src/lib.rs:893-925`) removes the `ProxyDefinition` entry from `Proxies` but performs no lookup into `Announcements::<T>` for that delegate: [3](#0-2) 

Only explicit `remove_announcement` (by the delegate) or `reject_announcement` (by the real account) purge an `Announcement`; nothing does so automatically on `remove_proxy`: [4](#0-3) 

The corrupted/unbound value is the `Announcement.height`/pending record in `Announcements::<T>`: it is never invalidated when the `(real, delegate)` relationship it was made under is revoked. Because `proxy_announced` re-validates only against whatever `ProxyDefinition` exists *at execution time* (not the one active at announcement time), and `Announcements` is never cleared on `remove_proxy`, the following sequence is possible:

1. `real` calls `add_proxy(delegate, ProxyType, delay=D)`.
2. `delegate` calls `announce(real, call_hash_of(malicious_call))`.
3. `real` detects the pending call is dangerous and calls `remove_proxy(delegate, ProxyType, D)` intending to revoke the delegate and cancel the pending action — but the `Announcement` in storage is untouched (deposit still reserved from `delegate`'s balance).
4. Time passes (more than `D` blocks) — trivial since `real` believes the threat is gone.
5. At some later point `real` re-adds the same `delegate` as a proxy (e.g., trust restored, a new unrelated use case, or a `create_pure`/multi-purpose account workflow that reuses the same delegate account) with `add_proxy(delegate, ProxyType', delay=D')`.
6. `delegate` immediately calls `proxy_announced(delegate, real, ..., malicious_call)`. Because `now - height >= D'` is virtually guaranteed (a lot of time has elapsed since the original, stale announcement), and `find_proxy` succeeds against the new relationship, the stale, previously-rejected call executes instantly — with **zero** delay window for `real` to review/reject it, defeating the entire purpose of the delay-based announcement design.

This is structurally identical to the CoreWallet finding: a co-signer's signed authorization was not tied to the specific `<signer, cosigner>` state/nonce at signing time, so it survived role reassignment and could be replayed later. Here, a delegate's announced-call authorization is not tied to the specific proxy relationship instance (nor invalidated on revocation), so it survives `remove_proxy` and can be replayed the moment the relationship is reinstated.

## Impact Explanation
This falls under "unauthorized execution or origin escalation" and "runtime bugs that compromise intended behavior" for the live-scope program: the delayed-announcement feature exists specifically so `real` can review and reject dangerous proxied calls before they execute. Silent persistence of stale announcements across `remove_proxy` defeats that safety guarantee, allowing a previously-revoked (and presumably distrusted enough to revoke) delegate to execute an old, unreviewed call instantly against `real`'s account balance/state as soon as any future proxy relationship is established for that delegate — with no attacker needing governance, admin, validator, or node privileges. Depending on the proxy type granted, this can mean asset transfers, staking operations, or other state-changing calls made from `real`'s account without a genuine, contemporaneous review window.

## Likelihood Explanation
The exploit requires only ordinary user-level extrinsics (`add_proxy`, `announce`, `remove_proxy`, later `add_proxy` again, `proxy_announced`) — no privileged actor, no malicious relayer/validator/collator, and no leaked keys. The main precondition is that `real` re-adds the same delegate account after revocation, which is a realistic, common pattern (e.g., re-trusting a service, rotating through a shared delegate/bot account, or exchange/custodial workflows that reuse delegate addresses). The delegate fully controls the timing of steps 2 and 6, and storage cleanup is never automatic, so the "stale announcement" window persists indefinitely until manually cleared via `reject_announcement`, which `real` has no reason to think is necessary after calling `remove_proxy`.

## Recommendation
When `remove_proxy_delegate` (and `remove_all_proxy_delegates`) removes a `ProxyDefinition`, also purge (or mark invalid) any `Announcements` entries whose `(delegate, real)` pair matches the removed relationship, refunding the associated deposit. Alternatively, bind each `Announcement` to a snapshot/hash of the `ProxyDefinition` (proxy_type + delay + a relationship-generation counter) at announce time, and require `proxy_announced` to validate that the *current* definition matches the one recorded at announcement, rejecting execution (and optionally auto-expiring) if the relationship was ever revoked and re-created in between.

## Proof of Concept
Conceptual reproduction using existing pallet extrinsics (can be adapted into a `#[test]` in `substrate/frame/proxy/src/tests.rs` alongside `proxy_announced_removes_announcement_and_returns_deposit`):
```rust
// 1. real=1 grants delegate=2 a proxy with delay=1
assert_ok!(Proxy::add_proxy(RuntimeOrigin::signed(1), 2, ProxyType::Any, 1));

// 2. delegate announces a dangerous call
let call = Box::new(call_transfer(6, 1000));
let call_hash = BlakeTwo256::hash_of(&call);
assert_ok!(Proxy::announce(RuntimeOrigin::signed(2), 1, call_hash));

// 3. real notices and revokes the proxy, believing this cancels everything
assert_ok!(Proxy::remove_proxy(RuntimeOrigin::signed(1), 2, ProxyType::Any, 1));

// Announcement is still present in storage (not cleared by remove_proxy)
assert!(Announcements::<Test>::get(2).0.iter().any(|a| a.call_hash == call_hash));

// time passes well beyond the original delay
frame_system::Pallet::<Test>::set_block_number(100);

// 4. real, for unrelated reasons, re-adds delegate 2 as proxy
assert_ok!(Proxy::add_proxy(RuntimeOrigin::signed(1), 2, ProxyType::Any, 1));

// 5. delegate immediately executes the stale, previously-revoked announcement
// with no fresh delay/review window, since `height` from the old announcement
// already satisfies the new delay condition.
assert_ok!(Proxy::proxy_announced(RuntimeOrigin::signed(0), 2, 1, None, call));
```
This demonstrates that `remove_proxy` does not invalidate previously announced calls, allowing them to be executed the moment the same delegate/real relationship is re-established, without the intended delay-review safeguard.

### Citations

**File:** substrate/frame/proxy/src/lib.rs (L441-453)
```rust
			let who = ensure_signed(origin)?;
			let real = T::Lookup::lookup(real)?;
			Proxies::<T>::get(&real)
				.0
				.into_iter()
				.find(|x| x.delegate == who)
				.ok_or(Error::<T>::NotProxy)?;

			let announcement = Announcement {
				real: real.clone(),
				call_hash,
				height: T::BlockNumberProvider::current_block_number(),
			};
```

**File:** substrate/frame/proxy/src/lib.rs (L489-528)
```rust
		pub fn remove_announcement(
			origin: OriginFor<T>,
			real: AccountIdLookupOf<T>,
			call_hash: CallHashOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let real = T::Lookup::lookup(real)?;
			Self::edit_announcements(&who, |ann| ann.real != real || ann.call_hash != call_hash)?;

			Ok(())
		}

		/// Remove the given announcement of a delegate.
		///
		/// May be called by a target (proxied) account to remove a call that one of their delegates
		/// (`delegate`) has announced they want to execute. The deposit is returned.
		///
		/// The dispatch origin for this call must be _Signed_.
		///
		/// Parameters:
		/// - `delegate`: The account that previously announced the call.
		/// - `call_hash`: The hash of the call to be made.
		#[pallet::call_index(8)]
		#[pallet::weight(T::WeightInfo::reject_announcement(
			T::MaxPending::get(),
			T::MaxProxies::get()
		))]
		pub fn reject_announcement(
			origin: OriginFor<T>,
			delegate: AccountIdLookupOf<T>,
			call_hash: CallHashOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let delegate = T::Lookup::lookup(delegate)?;
			Self::edit_announcements(&delegate, |ann| {
				ann.real != who || ann.call_hash != call_hash
			})?;

			Ok(())
		}
```

**File:** substrate/frame/proxy/src/lib.rs (L556-574)
```rust
		) -> DispatchResult {
			ensure_signed(origin)?;
			let delegate = T::Lookup::lookup(delegate)?;
			let real = T::Lookup::lookup(real)?;
			let def = Self::find_proxy(&real, &delegate, force_proxy_type)?;

			let call_hash = T::CallHasher::hash_of(&call);
			let now = T::BlockNumberProvider::current_block_number();
			Self::edit_announcements(&delegate, |ann| {
				ann.real != real ||
					ann.call_hash != call_hash ||
					now.saturating_sub(ann.height) < def.delay
			})
			.map_err(|_| Error::<T>::Unannounced)?;

			Self::do_proxy(def, real, *call);

			Ok(())
		}
```

**File:** substrate/frame/proxy/src/lib.rs (L893-925)
```rust
	pub fn remove_proxy_delegate(
		delegator: &T::AccountId,
		delegatee: T::AccountId,
		proxy_type: T::ProxyType,
		delay: BlockNumberFor<T>,
	) -> DispatchResult {
		Proxies::<T>::try_mutate_exists(delegator, |x| {
			let (mut proxies, old_deposit) = x.take().ok_or(Error::<T>::NotFound)?;
			let proxy_def = ProxyDefinition {
				delegate: delegatee.clone(),
				proxy_type: proxy_type.clone(),
				delay,
			};
			let i = proxies.binary_search(&proxy_def).ok().ok_or(Error::<T>::NotFound)?;
			proxies.remove(i);
			let new_deposit = Self::deposit(proxies.len() as u32);
			if new_deposit > old_deposit {
				T::Currency::reserve(delegator, new_deposit - old_deposit)?;
			} else if new_deposit < old_deposit {
				T::Currency::unreserve(delegator, old_deposit - new_deposit);
			}
			if !proxies.is_empty() {
				*x = Some((proxies, new_deposit))
			}
			Self::deposit_event(Event::<T>::ProxyRemoved {
				delegator: delegator.clone(),
				delegatee,
				proxy_type,
				delay,
			});
			Ok(())
		})
	}
```
