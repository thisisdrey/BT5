### Title
Attacker can permanently block collateral/debt delisting by keeping dust `totalSupply` — (`contracts/Pool.sol:725-744`)

### Summary
`Pool.removeDepositToken()` and `Pool.removeDebtToken()` revert with `TotalSupplyIsNotZero` whenever the token's `totalSupply() > 0`. Because `DepositToken.deposit()` is a permissionless public entry point and `DebtToken` issuance is available to any collateralized account, an unprivileged attacker can guarantee a nonzero `totalSupply` — either by holding a permanent dust position or by front-running/back-running every removal attempt — and thereby prevent the governor from ever delisting the asset.

### Finding Description
In `contracts/Pool.sol`:

```solidity
// contracts/Pool.sol:725-732
function removeDebtToken(IDebtToken debtToken_) external onlyGovernor {
    if (debtToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();
    ...
}

// contracts/Pool.sol:737-744
function removeDepositToken(IDepositToken depositToken_) external onlyGovernor {
    if (depositToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();
    ...
}
```

The guards are intended to prevent delisting an asset that still has live positions, but `totalSupply` is fully attacker-controllable:

- `DepositToken.deposit(uint256 amount_, address onBehalfOf_)` (`contracts/DepositToken.sol:211-237`) is callable by anyone while unpaused (`whenNotPaused nonReentrant onlyIfDepositTokenExists`). Notably it does **not** carry `onlyIfDepositTokenIsActive`, so even `toggleIsActive()` does not stop new deposits into the token. A deposit of a single wei of `underlying` mints a nonzero balance and raises `totalSupply` above 0 (net of fee rounding).
- For `DebtToken`, the attacker needs an open debt position (any user with deposited collateral can `DebtToken.issue`/`mint` a dust amount). Once any debt exists, `removeDebtToken` reverts.

Attack flow for the deposit-token case:
1. Governor calls `removeDepositToken(badDepositToken)`.
2. Attacker (watching the mempool, or simply keeping a standing dust deposit) calls `deposit(1, attacker)` on the same `DepositToken`.
3. `totalSupply() > 0` → the governor's transaction reverts with `TotalSupplyIsNotZero`.
4. Repeats indefinitely; on chains without public mempools the attacker keeps a permanent dust position and only tops it up if monitoring shows supply approaching zero — the marginal cost is a few wei of underlying plus gas.

This is the same bug class as the reference report (`_checkPoolsWithBalanceAreIncluded` reverting the whole distribution update when a removed pool has a balance): an unprivileged actor converts a balance check into a liveness failure for a privileged configuration function.

### Impact Explanation
The governor loses the ability to delist a collateral asset or a debt market for as long as the attacker is willing to maintain dust supply. Consequences:

- A compromised/degraded collateral (broken oracle, depegged underlying, misconfigured `collateralFactor`/`maxTotalSupply`) cannot be fully removed from the pool; it remains countenanced in `depositTokens` / `depositTokenOf`, keeps `isSwapActive`/deposit/liquidation paths referencing it, and continues contributing collateral value to `debtPositionOf`/health checks.
- Same for `removeDebtToken`: a synthetic market that governance wants to wind down stays live as long as any dust debt exists.
- Unlike a pause, removal is the mechanism that actually severs the asset; blocking it indefinitely converts a temporary issue into a permanent protocol exposure (potential insolvency vector if the reason for removal is solvency-related).

### Likelihood Explanation
- Cost is negligible: a dust `deposit()` or a minimal open debt, plus re-submission gas. A standing dust position makes the attack persistent without even needing front-running.
- Incentives exist: an attacker may want to keep a soon-to-be-removed collateral usable for minting/leverage, or to keep a debt market open (e.g., before oracle price moves, or to keep liquidation opportunities alive).
- No privileged role is needed; `deposit` is the normal public user flow and is not blocked by `onlyIfDepositTokenIsActive`, pause of *other* functions, reentrancy guards, or `SynthContext` checks.

### Recommendation
Rather than reverting on nonzero supply, make removal a graceful wind-down:

- Allow `removeDepositToken`/`removeDebtToken` to succeed while the token has supply, and instead rely on disabling (`isActive = false` / delisting from `depositTokenOf`/`debtTokenOf`) to block new deposits/issuance while still letting existing holders `withdraw`/`repay`.
- Alternatively, atomically force-exit residual positions during removal, or compare against a meaningful dust threshold rather than `> 0`.
- At minimum, ensure `deposit()` is gated by `onlyIfDepositTokenIsActive` so governance has a two-step path (deactivate → wait for exits → remove) that cannot be perpetually refilled by an attacker.

### Proof of Concept
Hardhat/fork outline (matches patterns in `test/Pool.test.ts` whitelisting suite):

```ts
// setup: real Pool, real DepositToken for e.g. WETH underlying, attacker = EOA
await underlying.mint(attacker.address, 1);              // dust
await underlying.connect(attacker).approve(depositToken.address, 1);

// 1) Governor tries to delist an idle deposit token -> succeeds normally
// 2) Attacker griefs:
await depositToken.connect(attacker).deposit(1, attacker.address); // mints >0 (fee==0 case)
expect(await depositToken.totalSupply()).to.be.gt(0);

// 3) Removal is now bricked
await expect(pool.connect(governor).removeDepositToken(depositToken.address))
    .to.be.revertedWithCustomError(pool, 'TotalSupplyIsNotZero');

// 4) Even if all honest users exit, attacker's dust keeps supply >0;
//    and deposit() is not blocked by toggleIsActive(), so governor cannot
//    prevent top-ups:
await depositToken.connect(governor).toggleIsActive();
await underlying.mint(attacker.address, 1);
await depositToken.connect(attacker).deposit(1, attacker.address); // still succeeds
```

The analogous `removeDebtToken` grief uses `DebtToken.issue`/`mint` of a minimal debt from a collateralized attacker account.