### Title
Permissionless "sufficients" griefing can permanently block Relay-Chain→Asset-Hub sovereign account migration - (File: cumulus/pallets/ah-ops/src/lib.rs)

### Summary
`do_migrate_parachain_sovereign_derived_acc` in the `ah-ops` pallet migrates a parachain sovereign-derived account's full balance (locks, freezes, holds, reserves) from the Relay Chain representation to its Asset Hub equivalent. Before transferring funds it hard-checks `sufficients(from) == 0` and defensively asserts `total == reducible`. Just like the Tempus `depositAndFix` bug — where an attacker griefs a post-condition assertion by pre-seeding an unrelated balance — an attacker can make an account "sufficient" for some asset before migration runs, tripping the `InternalError` guard and permanently blocking migration of that account's funds.

### Finding Description [1](#0-0) 
The function first releases locks, thaws freezes, releases holds, and unreserves the account, then zeroes `consumers`, and only then checks:
```
ensure!(frame_system::Pallet::<T>::sufficients(from) == 0, Error::<T>::InternalError);
```
followed by a sanity check that `total == reducible` before performing the actual `transfer` of the entire balance to the new account: [2](#0-1) 

The developer comment "*We dont handle sufficients and there should be none*" shows this is an assumed invariant about the target account, not an enforced one. `sufficients` on `frame_system::Account` is incremented whenever any *sufficient* asset (e.g., a `pallet-assets` asset flagged `is_sufficient = true`) is deposited into an account for the first time — this is a fully permissionless action available to any signed account holding that asset (via `pallet_assets::transfer`/`mint`, or any equivalent runtime instance). Since parachain sovereign-derived accounts are deterministically computable off-chain, an attacker can pre-compute the target `from` address for any parachain and simply send a dust amount of a sufficient asset to it before the migration executes. This makes `sufficients(from) > 0`, so `ensure!` fails with `Error::<T>::InternalError`, aborting the whole extrinsic (FRAME dispatchables run in an implicit transactional storage layer, so all the lock/freeze/hold releases done earlier in the same call are rolled back too — the call is fully idempotent-failing but permanently blocked as long as the attacker keeps refreshing the sufficient-asset balance, or even a single deposit if nothing is set up to clear `sufficients` automatically).

This mirrors the Tempus bug exactly: a state precondition (`yieldShares == 0` / `sufficients == 0`) that the function assumes is only affected by its own logic can instead be manipulated by an unrelated, unprivileged, permissionless action, causing the legitimate operation to revert every time it is attempted.

### Impact Explanation
This falls under "permanent user-fund or bridge-state lock" and "runtime bug that compromises intended behavior": the sovereign account's Relay-Chain balance (locks, freezes, holds, reserve) can never be migrated to Asset Hub for as long as the account remains "sufficient," effectively freezing that parachain's sovereign funds during/after the Relay Chain → Asset Hub migration. Because parachain sovereign account addresses are deterministic and public, any unprivileged actor with a sufficient-asset can execute this griefing against any/all parachains' sovereign accounts, at negligible cost (dust-sized asset transfer), with no need for governance, admin, validator, or relayer compromise.

### Likelihood Explanation
High: the only requirement is (a) knowledge of the deterministic sovereign-derived address (public/derivable), and (b) any existing sufficient asset that can be transferred to that address with a permissionless call (this is a completely standard, no-privilege action supported by `pallet-assets`). No governance, admin, or timing race is required — the attacker doesn't even need to front-run in the mempool sense; simply pre-funding the target address at any point before migration is sufficient, since the check is a static storage read at execution time.

### Recommendation
Do not hard-fail the migration on `sufficients(from) != 0`. Instead, either:
1. Handle non-zero sufficients defensively (e.g., decrement/clear sufficients as part of the migration, mirroring how consumers are zeroed), or
2. Separate the "sufficients" check into a best-effort/defensive log (as is already done for other invariants via `defensive_assert!`) rather than an `ensure!` that aborts the whole migration, or
3. Explicitly transfer/burn the extraneous sufficient-asset balance (or exclude it from the migration precondition) so a griefer cannot indefinitely block migration of a specific sovereign account.

### Proof of Concept
1. Compute the deterministic sovereign-derived Relay-Chain account (`from`) for a target parachain (publicly derivable via well-known derivation logic).
2. Before the Asset Hub migration processes this account, call any permissionless extrinsic that deposits a `is_sufficient = true` asset into `from` (e.g., `pallet_assets::transfer_keep_alive`/`mint` of any existing sufficient asset to `from`), which increments `frame_system::Account::<T>::get(from).sufficients` from 0 to 1.
3. When the migration logic invokes `do_migrate_parachain_sovereign_derived_acc(from, to, ...)`, execution reaches: [3](#0-2) 
   and fails with `Error::<T>::InternalError`, aborting the transaction and reverting the intermediate lock/freeze/hold releases performed earlier in the same call.
4. The parachain's sovereign balance remains stuck; repeat step 2 whenever needed to keep blocking retries.

### Citations

**File:** cumulus/pallets/ah-ops/src/lib.rs (L510-521)
```rust
			// Unreserve unnamed reserves
			let unnamed_reserve = <T as Config>::Currency::reserved_balance(from);
			let missing = <T as Config>::Currency::unreserve(from, unnamed_reserve);
			defensive_assert!(missing == 0, "Should have unreserved the full amount");

			// Set consumer refs to zero
			let consumers = frame_system::Pallet::<T>::consumers(from);
			frame_system::Account::<T>::mutate(from, |acc| {
				acc.consumers = 0;
			});
			// We dont handle sufficients and there should be none
			ensure!(frame_system::Pallet::<T>::sufficients(from) == 0, Error::<T>::InternalError);
```

**File:** cumulus/pallets/ah-ops/src/lib.rs (L523-539)
```rust
			// Sanity check
			let total = <T as Config>::Currency::total_balance(from);
			let reducible = <T as Config>::Currency::reducible_balance(
				from,
				Preservation::Expendable,
				Fortitude::Polite,
			);
			defensive_assert!(
				total >= <T as Config>::Currency::minimum_balance(),
				"Must have at least ED"
			);
			defensive_assert!(total == reducible, "Total balance should be reducible");

			// Now the actual balance transfer to the new account
			<T as Config>::Currency::transfer(from, to, total, Preservation::Expendable)
				.defensive()
				.map_err(|_| Error::<T>::FailedToTransfer)?;
```
