### Title
Emission rewards are permanently lost when token total supply is zero - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenIndex` advances the accrual timestamp even when a tracked token's `totalSupply()` is zero, while computing a zero reward ratio for that period. All reward tokens emitted at `tokenSpeeds[token]` during any zero-supply interval are therefore never credited to the reward index and can never be claimed by anyone — they are permanently stuck in the distributor. This is the same bug class as the Olas finding (incentives vanish instead of being deferred/refunded when the denominator — total weight / total supply — is zero).

### Finding Description
The index update logic is in `_calculateTokenIndex` (`contracts/RewardsDistributor.sol:197-212`):

```solidity
uint256 _speed = tokenSpeeds[token_];
uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
if (_deltaTimestamps > 0 && _speed > 0) {
    uint256 _totalSupply = token_.totalSupply();
    uint256 _tokensAccrued = _deltaTimestamps * _speed;
    uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
    _newIndex = (_supplyState.index + _ratio).toUint224();
    _newTimestamp = block.timestamp.toUint32();
}
```

When `_totalSupply == 0`, `_ratio = 0` but `_newTimestamp` is still set to `block.timestamp`. `_updateTokenIndex` then persists this state, so the elapsed window is consumed with zero accrual. Unlike the Olas case where a refund path existed but multiplied by a zero weight, here there is no catch-up or refund mechanism at all: once the timestamp advances, the `_tokensAccrued` for that window are dropped on the floor and the underlying reward ERC20 balance remains in the contract unclaimable.

This is reachable for `DepositToken` and `DebtToken` reward streams:
- A `DebtToken` can have `totalSupply() == 0` when all debt is repaid while its speed is still non-zero. A holder could fully repay/burn the last debt shares (e.g., via `DebtToken.repayAll` or `Pool` repayment paths), after which emissions keep accruing into the void until someone mints debt again. The first repayer can even deliberately repay in full, wait, and the subsequent emissions are burned rather than accruing to future borrowers, skewing later borrowers' share of the fixed emission budget — but more importantly the tokens are simply lost.
- Any period between `updateTokenSpeed(token, speed > 0)` and the first mint also silently discards emissions (the test at `test/RewardDistributor.test.ts:229-258` confirms index stays at `DEFAULT_INDEX` while the timestamp is consumed once supply appears).

There is no function to reclaim or re-credit the skipped emissions; `_tokensAccrued` are computed from `deltaTimestamps * speed` per call, so the accounting window is irrecoverably closed.

### Impact Explanation
Reward tokens emitted during zero-supply windows are permanently locked in `RewardsDistributor` — a freezing of protocol yield. For `DebtToken` streams this is user-reachable: the last borrower repaying in full zeroes supply, and any subsequent emission-seconds are lost until new debt is minted. The loss is bounded by `speed * zeroSupplyDuration` per token, but it is irreversible and silently deducted from the intended distribution budget, mirroring the assessed Medium severity of the Olas report (no direct theft of user assets, but loss/deduction of incentive funds under the zero-denominator condition).

### Likelihood Explanation
Medium-low. It requires `tokenSpeeds[token] > 0` while `totalSupply == 0`. For debt tokens this occurs whenever outstanding debt fully repays to zero (plausible in low-utilization periods or after market shutdowns/wind-downs where speeds may not be zeroed). For deposit tokens, a full exit of all depositors also zeroes supply. No privileged action is needed by the attacker — any borrower can trigger a zero-supply state by repaying, though the window's size depends on how long supply stays at zero.

### Recommendation
When `_totalSupply == 0`, do not advance `_supplyState.timestamp` (or track the skipped accrual so it can be credited later once supply returns). Concretely, only update `_newTimestamp` when `_totalSupply > 0`, so that emissions during zero-supply periods roll forward and are distributed to the next holders rather than being burned:

```solidity
if (_deltaTimestamps > 0 && _speed > 0) {
    uint256 _totalSupply = token_.totalSupply();
    if (_totalSupply > 0) {
        uint256 _tokensAccrued = _deltaTimestamps * _speed;
        _newIndex = (_supplyState.index + _tokensAccrued.wadDiv(_totalSupply)).toUint224();
        _newTimestamp = block.timestamp.toUint32();
    }
}
```

### Proof of Concept
Hardhat test sketch (mirroring the existing suite in `test/RewardDistributor.test.ts`):

```typescript
it('loses emissions accrued while totalSupply is zero', async () => {
  const speed = parseEther('1')
  await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, speed)

  // Supply is zero: alice mints nothing yet, 10 seconds pass
  msdTOKEN1.totalSupply.returns(0)
  msdTOKEN1.balanceOf.returns(0)
  await increaseTimeOfNextBlock(10)
  await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)
  await mine()

  // Now supply exists, 10 more seconds pass
  msdTOKEN1.totalSupply.returns(parseEther('100'))
  msdTOKEN1.balanceOf.returns(parseEther('100'))
  await increaseTimeOfNextBlock(10)
  const claimable = await rewardDistributor['claimable(address)'](alice.address)

  // Expected if zero-supply window were preserved: 20 VSP
  // Actual: 10 VSP — the first 10 seconds of emissions are permanently lost
  expect(claimable).eq(parseEther('10'))
})
```

The same sequence applies when `DebtToken` supply goes to zero mid-stream: emissions during the zero-supply window are dropped while the timestamp is consumed, and no function allows recovering them.