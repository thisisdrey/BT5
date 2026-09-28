### Title
Unprivileged attacker can fill a victim's `MAX_TOKENS_PER_USER` slot via dust `DepositToken` transfers, DoS-ing new deposits and borrows - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces a per-account cap of `MAX_TOKENS_PER_USER = 30` across `depositTokensOfAccount` + `debtTokensOfAccount` via `onlyIfAdditionWillNotReachMaxTokens` (contracts/Pool.sol:143-148). `DepositToken` is a freely transferable ERC20: `transfer`/`transferFrom` only check that the *sender's* amount is unlocked (`_revertIfLocked` on the sender, DepositToken.sol:348-376), and `_transfer` unconditionally pushes the token into the *recipient's* account list when `balanceOf[recipient_] == 0` (DepositToken.sol:518-520). An attacker can deposit dust amounts of many collateral types and `transfer` 1 wei of each msd token to a victim, filling all 30 slots. Afterwards any action that would add a *new* token to the victim's lists — `deposit()` of a collateral the victim doesn't already hold (`_mint` → `addToDepositTokensOfAccount`, DepositToken.sol:486-488), or `DebtToken.issue/mint` of a synthetic not already held (`addToDebtTokensOfAccount`, Pool.sol:204-208) — reverts with `UserReachedMaxTokens`.

### Finding Description
- Entry point: `DepositToken.transfer(victim, dust)` — permissionless, no consent check on recipient.
- For each registered `DepositToken` the attacker: approves underlying → `deposit(dust, attacker)` → `transfer(victim, 1)`. Each first-time receipt appends to `depositTokensOfAccount[victim]`.
- After `depositTokensOfAccount.length(victim) + debtTokensOfAccount.length(victim) == 30`, `Pool.addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert for any *new* token (Pool.sol:143-148, 204-219). Since the check runs inside `_mint`/`_transfer`, the entire `deposit`/`issue` transaction reverts.
- Consequences: victim cannot deposit a new collateral type to shore up an unhealthy position (can be griefed into liquidation when their held collateral can't cover), cannot open a debt position in a synthetic they don't already have debt tokens for, and any inbound msd-token transfer of a new type reverts — this also bricks `Pool.liquidate` flows where `seize` → `_transfer` to a liquidator whose list is full, and `SmartFarmingManager` leverage paths that mint new deposit tokens for the victim.

### Impact Explanation
Partial denial of service / temporary freezing of funds matching the CVE's bug class (unauthenticated partial DoS): a targeted user is blocked from adding collateral or borrowing until they manually clear slots by fully transferring/withdrawing the dust balances. Recovery requires the victim to detect the spam and spend one tx per spam token; while the list is full, an unhealthy position can be pushed into liquidation because the victim cannot add a different collateral. No privileged role, oracle manipulation, or governance action is required.

### Likelihood Explanation
Fully reachable by any EOA using only public entry points (`deposit`, `transfer`). Cost is bounded by the number of registered deposit tokens (must acquire a dust amount of each underlying) and is front-runnable repeatedly: the attacker can monitor the mempool and top the victim back up to 30 whenever a slot is freed. The only mitigant is that victims can self-unblock by emptying the dust balances, which keeps this a temporary rather than permanent freeze.

### Recommendation
Track membership idempotently instead of reverting: in `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount`, return silently (or skip) when the account is at `MAX_TOKENS_PER_USER` but the token is already in the list, and/or only enforce the cap on user-initiated `deposit`/`issue` while letting plain ERC20 `transfer`s succeed without list insertion (e.g., don't auto-add tokens received via transfer; add lazily on deposit). Alternatively, allow accounts to permissionlessly remove dust entries (`removeDustToken(token)`) or reject `transfer` of amounts below a minimum.

### Proof of Concept
```solidity
// Foundry fork test sketch (base or mainnet deployment)
// attacker = unprivileged EOA; victim = target EOA
IDepositToken[] memory tokens = pool.getDepositTokens(); // registered collateral deposit tokens
uint256 slots = pool.MAX_TOKENS_PER_USER()
    - pool.getDebtTokensOfAccount(victim).length
    - pool.getDepositTokensOfAccount(victim).length;

for (uint256 i; i < slots; ++i) {
    IDepositToken dt = tokens[i];
    IERC20 underlying = dt.underlying();
    deal(address(underlying), attacker, 1e6);            // dust collateral
    underlying.approve(address(dt), 1e6);
    dt.deposit(1, attacker);                              // mint 1 wei of msdTOKEN
    dt.transfer(victim, 1);                               // adds token to victim's list
}

assertEq(
    pool.getDepositTokensOfAccount(victim).length
        + pool.getDebtTokensOfAccount(victim).length,
    pool.MAX_TOKENS_PER_USER()
);

// Victim can no longer deposit a collateral type they don't already hold
IDepositToken fresh = tokens[slots]; // a token victim has zero balance of
vm.startPrank(victim);
fresh.underlying().approve(address(fresh), 1e18);
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
fresh.deposit(1e18, victim);
// Nor issue a new synthetic debt (addToDebtTokensOfAccount reverts the same way)
vm.stopPrank();
```