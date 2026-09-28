### Title
Boundary off-by-one in `MetAirdrop._transferReward` locks user rewards at exactly `updatedAt + lockPeriod` - (File: contracts/MetAirdrop.sol)

### Summary
`MetAirdrop._transferReward` decides whether a claimed reward is paid out in liquid `MET` or locked into `esMET` using a strict `<` comparison. When `block.timestamp` is exactly equal to `updatedAt + lockPeriod` — i.e. the moment the lock period has ended — the check `_end < block.timestamp` evaluates to `false`, so the reward is incorrectly routed into `esMET.lockFor` and locked again for at least `MINIMUM_LOCK_PERIOD + 1` seconds instead of being transferred freely to the claimant.

### Finding Description
In `contracts/MetAirdrop.sol`:

```solidity
function _transferReward(address to_, uint256 amount_) internal override {
    uint256 _end = updatedAt + lockPeriod;

    if (_end < block.timestamp) {          // strict < — boundary misses the free-transfer branch
        MET.safeTransfer(to_, amount_);
        return;
    }

    uint256 _min = ESMET.MINIMUM_LOCK_PERIOD() + 1;
    uint256 _max = ESMET.MAXIMUM_LOCK_PERIOD();

    uint256 _remainLockPeriod = Math.min(Math.max(_end - block.timestamp, _min), _max);

    token.safeApprove(address(ESMET), 0);
    token.safeApprove(address(ESMET), amount_);
    ESMET.lockFor(to_, amount_, _remainLockPeriod);
}
```

`lockPeriod` is defined as "For how long `MET` tokens will be locked" starting from `updatedAt` (line 22, line 29 comment). The intended semantic boundary is: at `block.timestamp >= updatedAt + lockPeriod` the position should be unlocked. The strict `<` makes the equality case fall through to the locking path. Because `_end - block.timestamp == 0` at that moment, `_remainLockPeriod` is clamped to `ESMET.MINIMUM_LOCK_PERIOD() + 1`, meaning the user's tokens get locked for the full minimum esMET lock duration — far longer than a 1-second boundary glitch.

This is reached from the unprivileged public entry point `RecurringAirdrop.claim(amount_, proof_)` (`contracts/utils/RecurringAirdrop.sol:52`), which verifies a merkle proof for `msg.sender` and calls `_transferReward(msg.sender, _claimable)` — anyone with a valid leaf can trigger it at the boundary timestamp.

### Impact Explanation
Temporary freezing of user funds: a claimant whose transaction lands at `block.timestamp == updatedAt + lockPeriod` receives an esMET position locked for `MINIMUM_LOCK_PERIOD + 1` seconds instead of liquid MET. The funds are recoverable only after the forced lock expires, and during that window the user bears the lock they never consented to (and, depending on esMET semantics, a lock-tied NFT position rather than fungible MET).

### Likelihood Explanation
Likelihood is low but non-zero: it requires the claim transaction to be mined in a block whose timestamp is exactly `updatedAt + lockPeriod`. `updatedAt` is set on every `updateMerkleRoot` call (`RecurringAirdrop.sol:86`), and `lockPeriod` defaults to `7 days`, so the boundary is a single predictable second per distribution round — known to all observers. On chains where block timestamps tick in fixed increments (e.g. 2s or 12s slots), whether the boundary second is even reachable depends on `updatedAt`'s alignment, but when reachable any claimant (not just a front-running attacker) hits it.

### Recommendation
Change the strict inequality to inclusive:

```solidity
if (_end <= block.timestamp) {
    MET.safeTransfer(to_, amount_);
    return;
}
```

This makes "the lock period has ended" true at the exact boundary, matching the documented `lockPeriod` semantics.

### Proof of Concept
Hardhat fork test (mainnet, since `ESMET`/`MET` are mainnet addresses), extending the existing suite in `test/MetAirdrop.test.ts`:

```ts
it('should receive liquid MET when claiming exactly at updatedAt + lockPeriod', async function () {
  const updatedAt = await airdrop.updatedAt()
  const lockPeriod = await airdrop.lockPeriod()
  const boundary = updatedAt.add(lockPeriod)

  // Mine the claim at the exact boundary timestamp
  await time.setNextBlockTimestamp(boundary.toNumber())

  const amount = rewards0[alice.address]
  const leaf = generateLeaf(alice.address, amount)
  const proof = merkleTree0.getHexProof(leaf)
  await airdrop.connect(alice).claim(amount, proof)

  // EXPECTED (fixed code): alice holds liquid MET, no new esMET position
  expect(await met.balanceOf(alice.address)).eq(amount)

  // ACTUAL (current code): alice gets 0 MET; a new esMET721 position is created
  // with unlockTime = boundary + MINIMUM_LOCK_PERIOD + 1
})
```

With the current `_end < block.timestamp` check the claim at `boundary` takes the `lockFor` path and `met.balanceOf(alice)` stays `0`; after the fix it returns `amount`. This reproduces deterministically since `setNextBlockTimestamp` controls `block.timestamp` exactly.

Note on confidence: the boundary-second reachability on live chains depends on slot granularity alignment, but on a Hardhat fork it is fully reproducible. No other strict-`<`-vs-`<=` timing boundary with unprivileged reach and fund-freezing impact was found; `DebtToken`'s `block.timestamp == _lastTimestampAccrued` early-return and `>` accrual guard are correct, and the `RewardsDistributor.syncTokenSpeed` `periodFinish` path is keeper-gated (out of scope).