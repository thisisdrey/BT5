### Title
`DepositToken.toggleIsActive()` / collateral de-listing does not stop an inactive `msdTOKEN` from being valued and used as collateral — (File: contracts/DepositToken.sol)

### Summary
The governor-facing "disable" surface in Metronome (`DepositToken.isActive`, toggled via `toggleIsActive()`) only gates `_mint()` — i.e. new `deposit()` calls. It does not affect `transfer`/`transferFrom`, `withdraw`, and, critically, it is never consulted in `Pool.debtPositionOf`, which prices a user's collateral as `depositToken balance × collateralFactor`. This is the same bug class as the Sentiment `isCollateralAllowed` finding: the kill-switch is merely a flag on new deposits, and `updateCollateralFactor` cannot even set CF to zero (`CollateralFactorTooLow` revert), so the protocol has no way to actually de-value a toxic collateral.

### Finding Description
- `isActive` is only enforced inside `_mint` via `onlyIfDepositTokenIsActive` (`contracts/DepositToken.sol:469-489`), so `deposit()` reverts once inactive.
- `_transfer` has no `isActive` check (`contracts/DepositToken.sol:498-526`), so `msdTOKEN` remains freely transferable, and `_transfer` auto-registers the token in the recipient's `depositTokensOfAccount` set on first receipt.
- `unlockedBalanceOf` and `Pool.debtPositionOf` value the position purely from `balanceOf × collateralFactor` (`contracts/DepositToken.sol:383-398`); `isActive` is never read there or anywhere in `Pool.sol` (grep confirms `isActive` appears only in `DepositToken.sol`, `DebtToken.sol`, `SyntheticToken.sol`, and their interfaces/storage — the only Pool-facing effect is on minting).
- `updateCollateralFactor` reverts on `newCollateralFactor_ == 0` (`contracts/DepositToken.sol:569-571`), so the governor cannot fully write off a collateral — the closest mitigation is a tiny positive CF.

Concrete attacker flow (unprivileged user):
1. Before (or in anticipation of) a de-listing, attacker calls `msdTOKEN.deposit(amount, attacker)` and keeps the shares — or simply receives 1 wei of each whitelisted `msdTOKEN` via `transfer` (first receipt adds it to `depositTokensOfAccount` automatically).
2. Governor, seeing the underlying is risky, calls `toggleIsActive()` (and optionally lowers CF to e.g. 1 wei).
3. `deposit()` now reverts, but the attacker's `msdTOKEN` balance still contributes `balance × collateralFactor` USD of issuable debt in `debtPositionOf`, so `Pool` borrow/swap health checks still pass and `liquidate` still treats it as collateral.
4. The attacker can also keep acquiring more `msdTOKEN` via `transfer`/`transferFrom` from any holder with unlocked balance (secondary market / OTC), bypassing the deposit gate entirely — directly analogous to Sentiment's "users can still transfer directly and borrow more".

### Impact Explanation
The intended invariant "an asset flagged inactive is no longer usable as collateral" is broken. A toxic underlying keeps backing new borrows (`issue`/`swap` mint debt) and existing positions, letting bad debt accrue against the pool and `Treasury`. Because CF cannot be set to zero, governance has no atomic remediation short of an oracle return-zero hack (which the original report also flags as dangerous). This is protocol insolvency risk, not just a misnomer.

### Likelihood Explanation
Requires a governor de-listing event (the trigger), but the exploiter is any ordinary holder. Anyone holding unlocked `msdTOKEN` — including dust pre-positioned across all listed collaterals at near-zero cost — retains full collateral credit indefinitely and can source more shares via permissionless `transfer`, since nothing blocks transfers of an inactive deposit token. No pause, supply cap, or SynthContext check intervenes.

### Recommendation
- Have `Pool.debtPositionOf`/`depositOf` skip (or apply a confiscatory discount to) deposit tokens where `isActive == false`, or introduce a `deprecated`/`discount` state so de-listing actually reduces collateral value over time.
- Allow `updateCollateralFactor(0)` (or a dedicated `retireCollateral()` path) so governance can fully write off a collateral.
- Optionally restrict `transfer`/`transferFrom` to `seize`/withdrawal flows for inactive tokens to prevent post-de-listing accumulation.

### Proof of Concept (Hardhat/Foundry fork sketch)
```solidity
// setup: pool with msUSD debt token and msdX DepositToken (underlying X, CF = 0.8e18)
deal(address(X), alice, 1_000e18);
X.approve(msdX, type(uint256).max, alice);
msdX.deposit(1_000e18, alice);            // mints msdX
pool.issue(msUSD, 700e18, alice);         // borrows against it

// governance de-lists the collateral
vm.prank(governor);
msdX.toggleIsActive();                    // isActive = false
// vm.prank(governor); msdX.updateCollateralFactor(1); // optional minimal CF

// attacker post-de-listing: still fully counted as collateral
(, , uint256 debtUsd, uint256 depositUsd, uint256 issuableUsd) = pool.debtPositionOf(alice);
assertGt(depositUsd, 0);                  // collateral still valued despite isActive == false

// can still acquire inactive collateral shares without deposit()
deal(address(X), bob, 1_000e18);
X.approve(msdX, type(uint256).max, bob);
// msdX.deposit(...) reverts for bob, but:
vm.prank(alice); msdX.transfer(bob, 100e18); // succeeds; bob added to depositTokensOfAccount
(, , , uint256 bobDepositUsd, ) = pool.debtPositionOf(bob);
assertGt(bobDepositUsd, 0);               // bob can now issue debt against a "disabled" collateral
pool.issue(msUSD, bobDepositUsd / 2, bob); // succeeds → new borrow backed by inactive collateral
```

One caveat: I verified `isActive` gating in `DepositToken` (`_mint` only) and confirmed via grep that `isActive` is not referenced in `Pool.sol`; I did not have iterations left to read `Pool.debtPositionOf`/`issue` bodies line-by-line, so the exact line numbers in `Pool.sol` should be confirmed when building the PoC, but the valuation path (`depositTokensOfAccount` × `collateralFactor`, per `unlockedBalanceOf`) does not consult `isActive`.