### Title
Attacker permanently blocks `Pool.removeDepositToken`/`removeDebtToken` with a dust deposit/debt via front-running — (File: contracts/Pool.sol)

### Summary
`Pool.removeDepositToken` and `Pool.removeDebtToken` revert with `TotalSupplyIsNotZero` whenever the token's `totalSupply() > 0` (`contracts/Pool.sol:725-744`). Any unprivileged user can mint a dust balance of the deposit token through the public `DepositToken.deposit` (`contracts/DepositToken.sol:211-237`) — or, for a debt token, hold a dust debt position — so the governor's removal transaction reverts. This is the same bug class as the PoolTogether `VaultBooster.setBoost` front-running grief: a public path that changes a balance/supply the admin check depends on, executed ahead of (or independently of) the privileged call, yields a repeatable DoS of the admin function.

### Finding Description
```solidity
// contracts/Pool.sol
function removeDebtToken(IDebtToken debtToken_) external onlyGovernor {
    if (debtToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();
    ...
}
function removeDepositToken(IDepositToken depositToken_) external onlyGovernor {
    if (depositToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();
    ...
}
```

`DepositToken.deposit(amount_, onBehalfOf_)` is callable by anyone when the pool is not paused, only requires `amount_ > 0`, and mints new `msdTOKEN` supply into `totalSupply` (`DepositToken.sol:211-237`, `_mint` at `DepositToken.sol:469-489`). There is no admin "force burn" / "seize arbitrary supply" path, so once even `1 wei` of supply exists, `removeDepositToken` reverts unconditionally until the attacker voluntarily withdraws.

For `removeDebtToken` the situation is worse: `DebtToken.totalSupply()` returns `totalSupply_ + _interestAmountAccrued` (`DebtToken.sol:500-503`), so as long as `interestRatePerSecond() > 0` and any principal exists, supply is never zero — and an attacker can additionally front-run with a minimal mint to keep a nonzero balance. (A debt position requires collateral and is subject to `debtFloorInUsd`, so the deposit-token variant is the cheapest and cleanest attack.)

### Impact Explanation
- Liveness DoS on governor operations: the governor cannot remove a compromised, misconfigured, or deprecated deposit/debt token from the pool offerings as long as any dust supply exists, and the attacker can re-inflate supply every time it reaches zero (front-running each removal attempt in the mempool for the cost of a dust deposit).
- If removal is needed in an incident response (e.g., a broken oracle for that collateral, a fee-on-transfer token behaving badly), the admin function is permanently blocked without attacker cooperation.
- Cost to attacker is negligible: `1 wei` of the underlying plus gas.

### Likelihood Explanation
- Requires only public entry points (`deposit`, or `transfer`/`issue`-adjacent flows for supply already held). No privileged role, flash loan, or oracle manipulation needed.
- Repeatable: each governor `removeDepositToken` transaction can be front-run again, mirroring the VaultBooster report's "as long as needed" property.
- Caveat: if `isActive` is toggled off by the governor first, `deposit` reverts via `onlyIfDepositTokenIsActive` in `_mint`; however any pre-existing holder balance (which the governor cannot burn) still keeps `totalSupply > 0`, and `DepositToken.transfer` is unrestricted for unlocked balances, so an attacker distributing dust across accounts can keep supply nonzero anyway — supply only drops to zero if every holder fully withdraws.

### Recommendation
- Add a privileged force-removal path that seizes/burns residual dust supply to the fee collector or treasury, or allow removal when `totalSupply` is below a dust threshold.
- Alternatively, atomically combine `toggleIsActive` (disable) + removal, and/or add a timelock + "deactivation then removal" two-step where deposits and new issuance are blocked while the token winds down, so dust minting cannot re-grief the removal.

### Proof of Concept
Foundry/Hardhat-style reproduction (Hardhat, matching the repo's test style):

```ts
// Setup: pool with governor, deposit token msdMET with underlying MET, currently zero supply
// Attacker front-runs (or simply precedes) governor.removeDepositToken(msdMET)

// 1. Attacker deposits dust
await met.mint(attacker.address, 1)
await met.connect(attacker).approve(msdMET.address, 1)
await msdMET.connect(attacker).deposit(1, attacker.address) // totalSupply = 1 wei

// 2. Governor removal reverts forever while attacker holds dust
await expect(pool.removeDepositToken(msdMET.address))
  .to.be.revertedWithCustomError(pool, 'TotalSupplyIsNotZero')

// 3. Even if attacker withdraws, they can front-run every retry with another 1-wei deposit
```

The deposit path is guarded only by `whenNotPaused`/`nonReentrant`/`onlyIfDepositTokenExists` and `amount_ > 0` (`DepositToken.sol:211-216`), none of which stop an unprivileged attacker.

**Uncertainty noted:** I did not fully verify whether an alternate force-removal or seize-to-zero mechanism exists elsewhere (e.g., `DepositToken.seize` used by liquidations, or PoolRegistry-level removal). If such a privileged burn exists and is wired, the impact is reduced to requiring an extra admin step rather than a hard block.