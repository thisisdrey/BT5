Audit Report

## Title
Not a currently exploitable vulnerability — the described `CurrentEra`/`ActiveEra` mismatch was a historical bug already remediated in this codebase - (File: `substrate/frame/nomination-pools/src/lib.rs`)

## Summary
The claim describes a real, but already-fixed, historical defect in which two different era concepts were used inconsistently across `pallet-nomination-pools` call sites, causing one identified pool member's points to be dissolved without releasing the corresponding held balance. Examination of the current `unbond` and `withdraw_unbonded` implementations shows that both now call the exact same `T::StakeAdapter::current_era()` accessor to key/reconcile `SubPoolsStorage`, and the specific manifestation of the bug was remediated via `pr_10986` ("Use active era for withdrawals") and the affected account's funds were recovered via the one-time migration in `pr_11018`, which also introduced `do_claim_trapped_balance` as a defensive general safety net.

## Finding Description
Both `unbond` at `substrate/frame/nomination-pools/src/lib.rs:2290` and `withdraw_unbonded` at `substrate/frame/nomination-pools/src/lib.rs:2410` compute their era value via the identical call `T::StakeAdapter::current_era()`. Since both code paths call the same adapter function, the `unbond_era` computed and inserted into `SubPoolsStorage::with_era` at unbond time is keyed consistently with the era value later used by `withdraw_unlocked(active_era)` and the `sub_pools.with_era.get_mut(era)` lookup in `withdraw_unbonded`. The claim's premise — that "the era used to record an unbonding chunk diverges from the era used elsewhere to reconcile the member's sub-pool balance" as a *currently reachable, unprivileged* exploit — is not substantiated in the current code: the two call sites are not using two different era sources; they use the same accessor.

The prdocs cited by the claim (`pr_10986`, `pr_11018`) themselves document that the divergence was a defect that has already been fixed by standardizing on active era, and that the one known affected account was remediated by a one-time migration. The claim itself concedes: "the exact triggering conditions of the historical occurrence are not fully described... a full step-by-step exploit transaction sequence cannot be reconstructed with certainty from this repository snapshot alone." This is a direct admission that no reachable, reproducible exploit path from an unprivileged extrinsic caller to bad state has been demonstrated against the current code.

## Impact Explanation
No concrete, currently-exploitable impact is demonstrated. The claim relies on speculation about a "future occurrence" of a divergence class that the current code does not exhibit at the two cited call sites (both use the same `current_era()` accessor). Without a demonstrated live divergence path in current code, there is no fund-lock vulnerability to assess for severity under the Polkadot SDK impact gate.

## Likelihood Explanation
The claim provides no attacker-controlled, unprivileged extrinsic sequence that currently produces the divergence; it only cites the historical fixed bug and defensive remediation. The recommendation to "expose `do_claim_trapped_balance` as a permissionless extrinsic" is a hardening suggestion, not evidence of an active vulnerability — the helper already exists as a defensive backstop and was exercised via governance-driven migration only because the historical defect had already occurred and been fixed by that point; this does not establish an ongoing unprivileged exploit path in the current codebase.

## Recommendation
N/A — no vulnerability confirmed in current code. If the reporter wishes to substantiate the claim, a specific unprivileged reproduction showing two *distinct* era values being used at `unbond` vs. `withdraw_unbonded` (or any other era-keyed `SubPools` bookkeeping site) with the current adapter implementation would be required.

## Proof of Concept
None provided that reproduces the claimed divergence against current code. The claim's own PoC section explicitly states the reproduction cannot be constructed from the indexed repository state.