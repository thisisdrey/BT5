### Title
Attacker can dust-fill a victim's per-account token set to `MAX_TOKENS_PER_USER`, DoS-ing deposits, token receipt, and new debt issuance for that account - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to the Munchables `lockOnBehalf` grief — where any user could forcibly mutate another user's lock state — Metronome lets any unprivileged account forcibly mutate another account's per-user token lists. Every `DepositToken` transfer or mint to a recipient whose balance was zero calls `Pool.addToDepositTokensOfAccount(recipient)`, which reverts once the account's combined deposit + debt token count reaches `MAX_TOKENS_PER_USER` (30). An attacker can cheaply push a victim to the cap with dust transfers, after which *any* action that would add a new token to the victim's lists — deposits `onBehalfOf` the victim, transfers to the victim, or issuance of a new debt token — permanently reverts until the victim cleans up the dust.

### Finding Description
`Pool` tracks per-account deposit and debt tokens via `MappedEnumerableSet` and enforces a hard cap:

```solidity
// contracts/Pool.sol
uint256 public constant MAX_TOKENS_PER_USER = 30;

modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
```

`addToDepositTokensOfAccount` is guarded by this modifier and is callable only from a registered deposit token, but it is triggered by *any* recipient transition from a zero balance — no consent from the recipient is required:

```solidity
// contracts/DepositToken.sol::_transfer / _mint
if (_recipientBalanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(recipient_);
}
```

Attack path, all from unprivileged public entry points:

1. Attacker deposits (or buys) a dust amount of each listed deposit token in a pool (`DepositToken.deposit(amount, attacker)`).
2. Attacker calls `transfer(victim, 1)` on up to `30 - n` distinct deposit tokens the victim does not yet hold (where `n` = tokens already in the victim's lists). Each call adds that token to `depositTokensOfAccount[victim]` — the victim cannot refuse.
3. Once `debtTokensOfAccount[victim].length + depositTokensOfAccount[victim].length == 30`, every subsequent operation that would register a *new* token for the victim reverts with `UserReachedMaxTokens`:
   - `DepositToken.deposit(amount, victim)` by anyone (the `onBehalfOf` donation path — the direct analog of `lockOnBehalf`)
   - the victim's own `deposit` into a collateral they don't yet hold
   - `DebtToken.issue(..., victim)` / the victim's own mint of a synth whose debt token they don't yet hold (the `_mint` → `addToDebtTokensOfAccount` path applies the same cap)
   - any incoming transfer of a deposit token they don't already hold — including `SmartFarmingManager` leverage flows that mint deposit tokens to the user's account.

Just like the Munchables case, the attack requires almost no capital (1 wei per token) and is repeatable: the attacker can watch for the victim emptying dust balances and refill the set again.

### Impact Explanation
Targeted denial of service on an account:

- The victim is blocked from depositing new collateral types and from receiving deposit tokens. If the victim has an unhealthy position, third-party rescue deposits (`deposit(amount, victim)`) revert, leaving the position exposed to liquidation that a rescue would otherwise have prevented.
- The victim cannot open debt in a new synthetic asset, since minting a new debt token reverts at `addToDebtTokensOfAccount`.
- SmartFarmingManager operations that result in minting a new deposit token to the victim (e.g., `leverage` into a collateral they don't hold) revert.
- Recovery requires the victim to fully zero out each dust balance (transfer or withdraw it) so `removeFromDepositTokensOfAccount` fires — one transaction per dust token — and the attacker can re-grief at dust cost each time.

This is a temporary freezing of the account's ability to receive funds and use core protocol functions, matching the accepted impact classes (temporary freezing of funds / liveness of a user's position). It does not steal funds and does not freeze existing withdrawals, so severity is bounded accordingly.

### Likelihood Explanation
- Fully permissionless: `DepositToken.transfer` only checks the *sender's* unlocked balance via `_revertIfLocked`; the recipient's consent is never required.
- Cost is dust amounts of each deposit token plus gas; the pool itself caps listed deposit tokens at 30 (`addDepositToken` reverts with `ReachedMaxDepositTokens`), so the entire attack surface is bounded and cheap.
- Most effective against accounts that already hold several tokens (each existing token reduces the number of dust transfers needed) and against accounts needing urgent rescue deposits while near liquidation.
- Partial mitigation exists: the victim can self-recover by emptying dust balances, and pools with few listed deposit tokens can't reach the cap unless the victim also holds debt tokens — but deployments like mainnet Pool1/Pool2 list many deposit tokens, making the cap reachable in practice.

### Recommendation
- Do not add a token to `depositTokensOfAccount` on incoming *transfers* unless the recipient already uses the pool, or require recipient opt-in for first-time receipt (e.g., a whitelist/accept flow, mirroring the report's "two-step accept" suggestion).
- Alternatively, only register the token on `deposit()`/`_mint` and treat transfer-received dust as non-position (track positions separately from ERC20 balances), or exempt transfers below a minimum amount.
- At minimum, apply the `MAX_TOKENS_PER_USER` check only when the recipient is actually opening/maintaining a borrow position, so unsolicited dust cannot block unrelated actions.
- Consider allowing `addToDepositTokensOfAccount` to silently no-op for unsolicited transfers and lazily register on the account's first state-changing interaction.

### Proof of Concept
Foundry-style reproduction (hardhat repo, convertible directly):

```solidity
// Assume: pool with >= N listed deposit tokens, attacker funded.
// victim holds 1 deposit token (msdWETH) and no debt tokens.

function test_dustFillVictimTokenSet() public {
    // 1. Victim deposits into one collateral
    wethDepositToken.deposit(1 ether, victim); // depositTokensOfAccount[victim].length == 1

    // 2. Attacker acquires dust of the other listed deposit tokens
    for (uint256 i; i < depositTokens.length; ++i) {
        IDepositToken dt = IDepositToken(depositTokens[i]);
        if (dt == wethDepositToken) continue;
        dt.deposit(10, attacker);          // mint dust to attacker
        dt.transfer(victim, 1);            // registers token on victim's set
    }

    assertEq(
        pool.getDepositTokensOfAccount(victim).length +
        pool.getDebtTokensOfAccount(victim).length,
        30 // MAX_TOKENS_PER_USER
    );

    // 3a. Third-party rescue/donation deposit now reverts
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    usdcDepositToken.deposit(100e6, victim);

    // 3b. Victim depositing a collateral they don't hold reverts
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    usdcDepositToken.deposit(100e6, victim);

    // 3c. Issuing a new debt token for the victim reverts
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    msETHDebtToken.issue(1e18, victim); // via pool-issue path minting a new debt token

    // 4. Victim's existing withdraw still works, but the account is otherwise bricked
    //    until each dust balance is fully emptied, and the attacker can re-fill it.
}
```

Note: `unlockedBalanceOf`/`_revertIfLocked` constrain only the sender, so a zero-debt attacker's dust transfers always succeed. The finding stands on `Pool.sol` lines 79, 143–148, 204–220 and `DepositToken.sol` `_mint`/`_transfer` registration logic (lines 486–488, 518–520).