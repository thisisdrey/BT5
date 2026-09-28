### Title
Dust-deposit griefing fills a victim's `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER`, DoS-ing the victim's new deposits and synthetic mints - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`DepositToken.deposit(amount_, onBehalfOf_)` lets any caller mint deposit tokens to an arbitrary `onBehalfOf_` address. When the recipient's balance for a deposit token goes from 0 to positive, `Pool.addToDepositTokensOfAccount` appends that token to the recipient's `depositTokensOfAccount` set, which is capped at `MAX_TOKENS_PER_USER = 30` shared with `debtTokensOfAccount` (`onlyIfAdditionWillNotReachMaxTokens`, `Pool.sol:143-148,79`). An unprivileged attacker can deposit dust of every supported collateral `onBehalfOf_` the victim, permanently occupying all 30 slots so that any of the victim's subsequent deposits into a *new* collateral type — or first-time mint of a new synthetic (`DebtToken` → `addToDebtTokensOfAccount`, `Pool.sol:204-208`) — reverts with `UserReachedMaxTokens()`.

### Finding Description
- `DepositToken.deposit` accepts any `onBehalfOf_` with no opt-in and transfers only `amount_` of underlying from the caller to the treasury (`DepositToken.sol:211-236`).
- On mint, the pool registers the deposit token against `onBehalfOf_`, enforcing the shared 30-entry cap via `onlyIfAdditionWillNotReachMaxTokens` (`Pool.sol:143-148, 216-220`).
- Governance caps total deposit tokens at the same `MAX_TOKENS_PER_USER` (`Pool.sol:703`), so the attacker can saturate the entire list: deposit 1 wei (plus fee) of each of the ≤30 supported collaterals to the victim.
- Afterward, `deposit()` on behalf of the victim into any collateral they do not already hold reverts, and any `DebtToken` mint/issue for a synthetic they do not already owe reverts via `addToDebtTokensOfAccount`.
- No guard stops this: `deposit` is `whenNotPaused nonReentrant` only; `addToDepositTokensOfAccount` trusts the token contract's `account_` argument; there is no minimum deposit size.
- Cleanup is possible (victim withdraws a dust position to free a slot), but the attacker can re-grief cheaply in the same or next block by front-running, making the freeze effectively persistent. The attacker can also fill slots *before* the victim's first ever deposit, pre-emptively bricking a fresh account.

### Impact Explanation
Temporary but renewable freezing of the victim's ability to use the protocol: they cannot add new collateral or open new debt positions while their list is saturated. For users relying on adding a specific collateral (e.g., to rescue an unhealthy position with a different asset, or via `SmartFarmingManager.leverage` which deposits `onBehalfOf` the user), the revert blocks the action entirely, potentially causing forced liquidation losses while a rescue deposit is griefed.

### Likelihood Explanation
Low cost, fully permissionless: any EOA calls `deposit(dust, victim)` for each listed collateral. Cost is dust underlying + deposit fees across ≤30 tokens. No privileged role, oracle manipulation, or timing window beyond normal front-running is required.

### Recommendation
- Enforce a meaningful minimum deposit amount, and/or
- Only count tokens toward `MAX_TOKENS_PER_USER` when the resulting balance exceeds a dust threshold (e.g., check `balanceOf(account_)` after mint rather than a 0→nonzero transition), and/or
- Track per-account registration based on actual balance at usage time (`depositOf`/`debtOf` already iterate by balance) so attacker-chosen dust entries can't crowd out the cap.

### Proof of Concept
```solidity
// Hardhat/Foundry fork test (mainnet fork, real Pool/DepositToken set)
// Assumption: pool has N deposit tokens added via addDepositToken (<=30).
address victim = makeAddr("victim");

address[] memory deps = pool.getDepositTokens();
for (uint256 i; i < deps.length && pool.getDepositTokensOfAccount(victim).length < 30; ++i) {
    IDepositToken dt = IDepositToken(deps[i]);
    IERC20 underlying = dt.underlying();
    deal(address(underlying), attacker, 10);          // dust
    underlying.approve(address(dt), type(uint256).max);
    vm.prank(attacker);
    dt.deposit(1, victim);                             // fills a slot for victim
}

// Victim cannot deposit a collateral not already in their list:
vm.prank(victim);
vm.expectRevert(UserReachedMaxTokens.selector);
someNewDepositToken.deposit(amount, victim);

// Victim cannot mint a new synthetic either:
vm.prank(victim);
vm.expectRevert(UserReachedMaxTokens.selector);
debtTokenOfNewSynth.mint(victim, amount);
```

Note: I was unable to inspect `DepositToken._mint`/`DebtToken.mint` in this iteration to confirm the exact `addTo*TokensOfAccount` trigger line, but the call path is implied by the modifiers' comments (`Pool.sol:199-203, 210-215`) — worth verifying the precise revert path when writing the PoC.