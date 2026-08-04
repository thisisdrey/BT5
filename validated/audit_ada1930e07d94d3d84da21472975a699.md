## Analysis

I found a strong local analog: the `pallet-safe-mode` `extend()` extrinsic. This directly mirrors the external report's broken invariant — a caller-controlled call that pushes a protective/blocking deadline (`endTime` in the report, `EnteredUntil` here) further into the future, with no cap on repetition, thereby indefinitely delaying normal chain operation for everyone else. Critically, unlike the crowdloan `edit()` (Root-only) or `asset-rewards::set_pool_expiry_block` (pool-admin-only) that I also checked, `safe_mode::extend()` is explicitly documented as permissionless — callable by **any signed account**, not an owner/admin — which keeps this in scope per the impact gate (excludes "privileged governance or admin abuse as root cause", but this is an unprivileged public entrypoint). [1](#0-0) 

confirms `extend()` is signed-only (`ensure_signed`), accumulates on top of the current remaining duration, and is explicitly documented as having **no hard limit**: [2](#0-1) . The only gate is paying `ExtendDepositAmount`, and `AlreadyDeposited` only blocks the *same* account from re-depositing before release — it does not stop a different account, or the same account after depositing more funds, from calling `extend()` again once eligible, nor does it cap total elapsed safe-mode duration.

While safe-mode is entered, `BaseCallFilter = InsideBoth<DefaultFilter, SafeMode>` blocks all non-whitelisted calls chain-wide [3](#0-2) , i.e., withdrawals, refunds, and any other user-facing extrinsic not on `WhitelistedCalls` are frozen for as long as `EnteredUntil` remains in the future.

### Title
Unbounded Permissionless `extend()` in `pallet-safe-mode` Can Indefinitely Prolong Chain-Wide Call Filtering - (File: `substrate/frame/safe-mode/src/lib.rs`)

### Summary
`pallet-safe-mode::extend()` lets any signed account push `EnteredUntil` further into the future by `ExtendDuration` blocks, with no cap on the number of times this can be repeated or on the total cumulative extension, as explicitly documented in the code comment "This does not impose a hard limit as the safe-mode can be extended multiple times." This mirrors the `DaosLive.extendTime()` bug: a caller-reachable function that keeps a protective deadline perpetually in the future, blocking dependent user actions (here, all non-whitelisted extrinsics, potentially including withdrawals/refunds gated behind other pallets) for as long as attackers are willing to pay the (bounded, reclaimable) deposit.

### Finding Description
`Config::ExtendDuration` and `Config::ExtendDepositAmount` are runtime-configured constants; when `ExtendDepositAmount` is `Some(_)`, any signed account can call `extend()` [4](#0-3) . Each successful call adds `ExtendDuration` to `EnteredUntil` (per doc: "This accumulates on top of the current remaining duration" [5](#0-4) ), and there is no total-extension cap in the pallet logic itself. `AlreadyDeposited` (`Error::AlreadyDeposited`) only prevents a *given* account from re-entering/re-extending while it already holds an outstanding, unreleased deposit for the current entry — but any number of distinct accounts, or the same account cycling deposit→release→deposit, can keep the chain in safe mode indefinitely, since the deposit is refundable via `release_deposit`/`force_release_deposit` once `ReleaseDelay` passes and safe-mode exits, and re-entrant funding is possible.

While `EnteredUntil` remains in the future, `frame_system::Config::BaseCallFilter = InsideBoth<DefaultFilter, SafeMode>` blocks every call not present in `WhitelistedCalls`, chain-wide, for every account — not just the caller. Whether this becomes an actionable "fund lock/DoS" analog to the report depends entirely on the runtime's `WhitelistedCalls` set: if withdrawal/refund/claim extrinsics for balances, staking, treasury, or bridge payout pallets are not whitelisted, the coordinated (or even single, well-funded) use of `extend()` can perpetually block those legitimate user actions, exactly as `extendTime()` perpetually blocked contributor refunds in the original report.

### Impact Explanation
If safe-mode's `WhitelistedCalls` does not include the withdrawal/claim/refund paths users depend on (balances transfers, staking `withdraw_unbonded`, treasury payouts, bridge settlement calls), an attacker or colluding set of unprivileged accounts can use the permissionless, uncapped `extend()` to keep the entire runtime in a call-filtered state indefinitely, at the bounded cost of the (eventually-reclaimable) `ExtendDepositAmount` per extension cycle. This is a public underpriced work / chain-halting vector: it degrades block production usefulness (most calls rejected) and can permanently lock users out of fund-related dispatchables for as long as the attacker is willing to keep paying/rotating the deposit, which is a direct analog of the reported "refund block via repeated deadline extension."

### Likelihood Explanation
This requires the runtime integrator to have configured `ExtendDepositAmount = Some(_)` (permissionless extend enabled) and to have a `WhitelistedCalls` set that omits critical user-fund-related extrinsics. This is a deployment/configuration-dependent condition rather than an always-exploitable bug in every polkadot-sdk-based chain, so likelihood is dependent on runtime config; however, the pallet code itself provides no hard ceiling on cumulative extension count/duration, which is the exact structural flaw called out in the external report (missing cap + missing governance gate for extensions beyond a threshold).

### Recommendation
- Impose a hard ceiling (either an absolute max `EnteredUntil` value or a max cumulative extension count/duration since original `enter()`) enforceable in `do_extend`, independent of how many distinct depositing accounts participate.
- Require `ForceExtendOrigin` (governance/council) approval for any extension beyond a first bounded permissionless extension, similar to the report's recommendation to cap owner-driven extensions and require governance sign-off beyond a threshold.
- Ensure any runtime enabling permissionless `extend()` whitelists critical user-facing withdrawal/refund/claim calls so that safe-mode griefing cannot indefinitely block fund recovery paths.

### Proof of Concept
1. Runtime configures `pallet_safe_mode::Config` with `ExtendDepositAmount = Some(D)`, `EnterDepositAmount = Some(D2)`, and `WhitelistedCalls` that does not include, e.g., `Balances::transfer` or a staking/treasury withdrawal call.
2. Attacker (or safe-mode entrant) calls `enter()` — safe mode begins, `EnteredUntil = now + EnterDuration` [6](#0-5) .
3. Before `EnteredUntil` is reached, attacker calls `extend()` with a fresh signed account (or the same account after `release_deposit` reclaims the prior hold), each time adding `ExtendDuration` [4](#0-3) .
4. Repeat step 3 indefinitely — no code path in `do_extend`/`extend` enforces a maximum on `EnteredUntil` or on total extension count, as documented in the `ExtendDuration` doc comment: "This does not impose a hard limit as the safe-mode can be extended multiple times" [2](#0-1) .
5. For the entire duration, the `BaseCallFilter` continues rejecting all non-whitelisted calls, including any withdrawal/refund extrinsics not in `WhitelistedCalls`, reproducing the reported "refund blocked forever by repeated deadline extension" primitive without any privileged or admin action.

### Citations

**File:** substrate/frame/safe-mode/src/lib.rs (L44-51)
```rust
//!
//! ```ignore
//! impl frame_system::Config for Runtime {
//!   // …
//!   type BaseCallFilter = InsideBoth<DefaultFilter, SafeMode>;
//!   // …
//! }
//! ```
```

**File:** substrate/frame/safe-mode/src/lib.rs (L120-124)
```rust
		/// For how many blocks the safe-mode can be extended by each [`Pallet::extend`] call.
		///
		/// This does not impose a hard limit as the safe-mode can be extended multiple times.
		#[pallet::constant]
		type ExtendDuration: Get<BlockNumberFor<Self>>;
```

**File:** substrate/frame/safe-mode/src/lib.rs (L304-310)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::enter())]
		pub fn enter(origin: OriginFor<T>) -> DispatchResult {
			let who = ensure_signed(origin)?;

			Self::do_enter(Some(who), T::EnterDuration::get()).map_err(Into::into)
		}
```

**File:** substrate/frame/safe-mode/src/lib.rs (L326-343)
```rust
		/// Extend the safe-mode permissionlessly for [`Config::ExtendDuration`] blocks.
		///
		/// This accumulates on top of the current remaining duration.
		/// Reserves [`Config::ExtendDepositAmount`] from the caller's account.
		/// Emits an [`Event::Extended`] event on success.
		/// Errors with [`Error::Exited`] if the safe-mode is entered.
		/// Errors with [`Error::NotConfigured`] if the deposit amount is `None`.
		///
		/// This may be called by any signed origin with [`Config::ExtendDepositAmount`] free
		/// currency to reserve. This call can be disabled for all origins by configuring
		/// [`Config::ExtendDepositAmount`] to `None`.
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::extend())]
		pub fn extend(origin: OriginFor<T>) -> DispatchResult {
			let who = ensure_signed(origin)?;

			Self::do_extend(Some(who), T::ExtendDuration::get()).map_err(Into::into)
		}
```
