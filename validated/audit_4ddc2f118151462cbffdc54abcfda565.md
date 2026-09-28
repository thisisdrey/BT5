### Title
Dust-transfer griefing fills `MAX_TOKENS_PER_USER` account list, DoS-ing deposits, withdrawals and transfers - (File: contracts/Pool.sol)

### Summary
The PJSIP bug is a capacity overflow: parsing writes more frames than the caller-allocated buffer can hold. The Metronome analog is the per-account token list in `Pool`, capped at `MAX_TOKENS_PER_USER = 30` (`Pool.sol:79`). Any deposit-token transfer or mint that brings a recipient's balance of a new token from zero calls `Pool.addToDepositTokensOfAccount`, which reverts with `UserReachedMaxTokens` once the combined `depositTokensOfAccount + debtTokensOfAccount` length reaches 30 (`Pool.sol:143-148`, `DepositToken.sol:517-520`). Because `DepositToken.transfer`/`transferFrom` are public and unrestricted for unlocked balances, an unprivileged attacker can send dust amounts of every listed deposit token to a victim (or to `feeCollector`) and permanently occupy all 30 slots.

### Finding Description
- `Pool.addToDepositTokensOfAccount` enforces the cap via `onlyIfAdditionWillNotReachMaxTokens` and reverts when the account is at 30 entries (`Pool.sol:143-148`, `216-219`).
- `DepositToken._transfer` adds the token to the *recipient's* list before removing it from the sender's (`DepositToken.sol:518-524`), so pushing dust onto a target costs the attacker only a dust balance in each token.
- Consequences once a target's list is full:
  - `deposit(amount, onBehalfOf = victim)` for any deposit token the victim does not yet hold reverts in `_mint` (`DepositToken.sol:486-488`): the victim cannot add a new collateral type, including to rescue an unhealthy position.
  - Any `transfer`/`transferFrom`/`seize` to the victim of a new token reverts.
  - If the target is `feeCollector` and `withdrawFee > 0`, `_withdraw` first transfers the fee to `feeCollector` (`DepositToken.sol:546-548`); when `feeCollector` holds zero of that collateral the transfer reverts, so `withdraw` of that collateral fails for *every* user — protocol-wide withdrawal freeze.
  - `DebtToken` issuance (`issue`/`mint`) similarly adds debt tokens to the borrower's list; a borrower at 30 entries cannot mint a new synthetic asset.
- The victim does not choose which tokens land in their list — entries are forced by inbound transfers, matching the "more frames than the caller's buffer" bug class: the protocol writes entries into a fixed-capacity per-account set on behalf of a recipient who never consented.

### Impact Explanation
Temporary freezing of user funds and forced-liquidation risk:
- `feeCollector` fill-up DoS-es `withdraw` for every collateral type the collector does not yet hold, freezing all users' deposits in those tokens until the (multisig/governance-controlled) collector manually empties dust balances.
- Individual victims are blocked from depositing new collateral types, so a leveraged position that drifts unhealthy cannot be topped up and is forced into liquidation.
- No privilege is required; the attacker only needs unlocked dust balances in each deposit token.

### Likelihood Explanation
Requirements: at least 30 combined deposit/debt token types exist in the pool (deployed pools on mainnet/Optimism/Base/Hemi approach this scale as new collateral and synths are added), and the attacker holds dust balances — cheap to acquire. Each dust `transfer` is one cheap call. Recovery requires the victim to burn/transfer each dust balance to zero, which for `feeCollector` requires coordinated multisig action, extending the freeze window. If `withdrawFee == 0` on the deployed `FeeProvider`, the protocol-wide variant degrades to the per-victim deposit/liquidation-griefing variant, which still blocks collateral top-ups and inbound transfers.

### Recommendation
- Let the recipient evict: allow `removeFromDepositTokensOfAccount`/`removeFromDebtTokensOfAccount` to be called with an explicit account argument by the account owner, or expose an opt-out that ignores zero/dust balances.
- Alternatively, in `addToDepositTokensOfAccount`, skip the cap check or no-op when the account is the protocol `feeCollector`, and consider skipping the list update for transfers below a dust threshold.
- Optionally, perform the removal of the sender's entry before adding the recipient's entry in `_transfer` so a full-list sender is not additionally constrained.

### Proof of Concept
Foundry fork test sketch (mainnet fork, deployed `Pool` + `DepositToken` proxies):

```solidity
function test_feeCollectorListOverflowFreezesWithdrawals() public {
    IPool pool = IPool(POOL_PROXY);
    address feeCollector = pool.poolRegistry().feeCollector();
    IDepositToken[] memory tokens = getAllDepositTokens(pool); // >= 30 exist

    // Attacker deposits dust in each token and sends it to feeCollector
    for (uint i; i < tokens.length; ++i) {
        deal(address(tokens[i].underlying()), attacker, 1e6);
        tokens[i].underlying().approve(address(tokens[i]), 1e6);
        // if attacker doesn't already hold: deposit 1 wei-worth
        try tokens[i].deposit(1, attacker) {} catch {}
        tokens[i].transfer(feeCollector, 1); // fills feeCollector's list
    }

    // Sanity: list is at cap
    assertGe(
        pool.getDepositTokensOfAccount(feeCollector).length
      + pool.getDebtTokensOfAccount(feeCollector).length,
        pool.MAX_TOKENS_PER_USER()
    );

    // Pick a deposit token the feeCollector never held (balance 0)
    IDepositToken fresh = tokens[X]; // feeCollector.balanceOf(fresh) == 0
    // A user withdraws -> fee transfer to feeCollector reverts with UserReachedMaxTokens
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    fresh.withdraw(userBalance, user);
}
```

Same pattern with `victim` instead of `feeCollector` shows `deposit(amount, victim)` reverting for any new collateral type, blocking top-ups on an unhealthy position.