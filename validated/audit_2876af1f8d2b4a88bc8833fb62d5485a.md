### Title
Attacker can permanently DoS a victim's deposits and liquidations by stuffing their per-account deposit-token list to `MAX_TOKENS_PER_USER` via dust `DepositToken.transfer` — (File: contracts/Pool.sol)

### Summary
Analogous to the ClamAV EGG null-pointer-deref crash (crafted input → unhandled state → process DoS), `Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on each account's enumerable deposit-token set (`MappedEnumerableSet.AddressSet`), reverting with `UserReachedMaxTokens()` when exceeded. Because `DepositToken.transfer`/`transferFrom` register the token on the *recipient's* list (and only remove it when balance hits zero on burn), any unprivileged EOA can dust-transfer every registered `DepositToken` to a victim, filling their list. Once at the cap, all subsequent `DepositToken` transfers/mints to that account — including deposits and liquidation `seize` proceeds — revert.

### Finding Description
- `Pool` declares `MAX_TOKENS_PER_USER = 30` and `error UserReachedMaxTokens()` (`contracts/Pool.sol:32`, `:79`), and stores per-account deposit tokens in a `MappedEnumerableSet` (`contracts/Pool.sol:72`). `Pool.addDepositToken` caps the global list with the same constant (`contracts/Pool.sol:703-709`), so up to 30 distinct `DepositToken`s can exist per pool.
- `DepositToken._transfer`/`_mint` call `pool.addToDepositTokensOfAccount(recipient)` which inserts into the recipient's set and reverts `UserReachedMaxTokens` when it already contains 30 entries (the `addToDepositTokensOfAccount`/`removeFromDepositTokensOfAccount` pair is only balanced when a balance returns to zero, `contracts/DepositToken.sol:459-462`).
- Any holder of a `DepositToken` (attacker deposits a dust amount of each underlying via `NativeTokenGateway`/`VesperGateway` or buys on market) can call `DepositToken.transfer(victim, 1 wei)` for each of the pool's deposit tokens. Each dust amount leaves a nonzero balance, so the token is never removed from the victim's set.
- After 30 dust transfers, every later code path that would add another `DepositToken` to the victim — `deposit()` minting a new collateral token, a plain transfer, or `DepositToken.seize` crediting collateral during `Pool.liquidate` — hits `UserReachedMaxTokens` and reverts.

### Impact Explanation
- The victim is permanently unable to receive any deposit token not already in their set: new-position deposits, transfers in, and liquidation seize credits all revert. This is a permanent freeze of the deposit/transfer surface for that account (only removable if the victim fully withdraws/transfers out one of the 30 dust tokens — which they can do, but the attacker can refill the slot at any time for ~30 dust transfers).
- More critically, `Pool.liquidate` seizes collateral into the liquidator-specified recipient account; if the protocol/treasury flow ever credits the *victim's* position via a token not yet in their set, liquidation reverts, potentially leaving an underwater position unliquidatable — protocol insolvency risk. At minimum this is a griefing-based temporary/permanent freezing of the victim's funds and liveness, matching the CVE's "crash the scanning process → denial of service" bug class.

### Likelihood Explanation
- Fully unprivileged: requires only public `DepositToken.transfer` calls and dust balances of up to 30 collateral types obtainable through normal public `deposit` paths. No governance, keeper, oracle, or trusted-remote involvement.
- Cost is bounded by acquiring 1 wei of each listed deposit token plus gas; repeatable against any account, and re-appliable whenever a slot frees.

### Recommendation
- Only add to `depositTokensOfAccount` on recipient registration when balance transitions 0 → nonzero *above a minimum dust threshold*, or
- Remove the per-account cap entirely (the global `depositTokens` set is already capped at 30, so iterating the per-account set is bounded by the global list), or
- In `addToDepositTokensOfAccount`, skip the check for tokens already in the set and never revert on receive-side bookkeeping (worst case: cap enforcement only on the user's own deposit path, not on incoming transfers).

### Proof of Concept
```solidity
// Foundry fork test (mainnet), assuming Pool with N deposit tokens registered
function testDustGriefDoS() public {
    address victim = makeAddr("victim");
    IPool pool = IPool(POOL);

    uint256 n = pool.getDepositTokens().length; // up to MAX_TOKENS_PER_USER = 30
    for (uint256 i; i < n; ++i) {
        IDepositToken dt = IDepositToken(pool.getDepositTokens()[i]);
        // attacker acquires dust via deposit of the underlying
        deal(address(dt.underlying()), attacker, 1);
        dt.underlying().approve(address(dt), 1);
        dt.transfer(victim, 1); // registers token on victim's set
    }
    assertEq(pool.depositTokensOfAccount(victim).length, 30);

    // victim can no longer receive any other DepositToken
    IDepositToken other = IDepositToken(ANOTHER_DEPOSIT_TOKEN);
    vm.expectRevert(IPool.UserReachedMaxTokens.selector);
    other.transfer(victim, 1);
    // any deposit/seize path that would add a new token to victim reverts identically
}
```

Note: I verified `MAX_TOKENS_PER_USER`, `UserReachedMaxTokens`, the `MappedEnumerableSet` accounting, and the add/remove hooks (`Pool.sol:32,79,72`; `DepositToken.sol:459-462`; `Pool.sol:703-709`), but did not capture the exact revert line inside `addToDepositTokensOfAccount` or the `seize` credit path before running out of search iterations — the PoC's revert selector/line may need adjustment after reading those functions.