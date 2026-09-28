### Title
RewardsDistributor will permanently DoS reward updates and all dependent deposit/debt token operations when `block.timestamp` exceeds `uint32.max` - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor` stores per-token accrual state as `TokenState { uint224 index; uint32 timestamp; }` and writes `block.timestamp.toUint32()` via OpenZeppelin `SafeCast` on every index update. `SafeCast.toUint32` reverts on overflow rather than wrapping. When `block.timestamp > type(uint32).max` (February 2106), every call path that recomputes the index reverts, permanently bricking reward distribution and — because `DepositToken`/`DebtToken` call `updateBeforeMintOrBurn`/`updateBeforeTransfer` on every mint, burn, and transfer — permanently freezing user positions that touch rewarded tokens. This is the same bug class as the JalaPair report: a timestamp deliberately stored in 32 bits where truncation/wraparound was expected, but Solidity 0.8 semantics (plus `SafeCast` here) revert instead.

### Finding Description
In `_calculateTokenIndex`, the delta is computed in 256 bits against the stored `uint32` timestamp: [1](#0-0) 

Once `block.timestamp` exceeds `uint32.max` (≈ 2106-02-07), both branches reach `block.timestamp.toUint32()`, which reverts inside `SafeCast` (`require(value <= type(uint32).max)`): [2](#0-1) 

The reverting cast propagates through `_updateTokenIndex`, which is invoked by the externally reachable hooks `updateBeforeMintOrBurn` and `updateBeforeTransfer` whenever `tokenStates[token_].index > 0` (i.e., for any token that ever had a reward speed configured): [3](#0-2) [4](#0-3) 

These hooks are called by `DepositToken` and `DebtToken` (the contract docstring states "Called by DepositToken and DebtToken contracts") on mint/burn/transfer, and `claimRewards`/`claimable`/`updateTokenSpeeds`/`syncTokenSpeed` all hit the same reverting path. Additionally, the `uint224` index accumulation `(_supplyState.index + _ratio).toUint224()` shares the same cumulative-value-overflow class as `priceCumulativeLast`, though it overflows on an even longer horizon than the timestamp.

### Impact Explanation
Permanent denial of service / freezing of funds. After the timestamp boundary:

- `claimRewards` always reverts — all unclaimed accrued reward tokens in the distributor are locked forever.
- For any token with `tokenStates[token_].index > 0`, `updateBeforeMintOrBurn`/`updateBeforeTransfer` revert, which reverts every `DepositToken` deposit/withdraw/transfer and every `DebtToken` issue/repay/transfer touching that token — mirroring the JalaPair report where `mint`/`burn`/`swap` all revert through `_update`.
- Collateral backing debt becomes un-withdrawable and debt un-repayable through the normal token paths, so core protocol liveness is lost.

As in the original finding, impact is high (locked user funds and unclaimed yield) but the trigger is a limitation far in the future, matching the accepted Medium severity.

### Likelihood Explanation
Certain if the contracts remain in use past `uint32.max` seconds (~136 years from epoch, i.e., year 2106). No attacker action or privileged role is required — any unprivileged call (e.g., a deposit, a repay, or `claimRewards`) triggers the revert once the boundary is crossed. Unlike the JalaPair `priceCumulative` case, no extreme reserve ratio is needed; the `toUint32()` cast is unconditional whenever `deltaTimestamps > 0`, so it reverts on the first state-updating call after the boundary. The only mitigation is that the effect is confined to tokens whose `index > 0` (speeds were ever set) and that upgradeability/governance could redeploy or zero out speeds beforehand — but the code itself does not prevent the DoS.

### Recommendation
Do not downcast `block.timestamp` into the stored `uint32`. Either widen `TokenState.timestamp` (and the `uint224` index packing) to `uint64`/`uint256` in a storage-layout-compatible upgrade, or wrap the timestamp update in an explicit truncation that never reverts, e.g. `_newTimestamp = uint32(block.timestamp % 2**32)` inside an `unchecked`/modulo computation, and compute `_deltaTimestamps` with modular wraparound arithmetic (`block.timestamp mod 2^32 - uint256(timestamp)`) so the index accrual survives the boundary. Apply the same reasoning to the `toUint224()` index accumulation if cumulative growth beyond `uint224` is intended to wrap rather than revert.

### Proof of Concept
```solidity
// Foundry fork test sketch
function test_rewardsDistributorTimestampDoS() public {
    // rewardsDistributor, depositToken deployed and initialized;
    // governor has set tokenSpeeds[depositToken] > 0 so index > 0

    // sanity: a user can deposit now
    depositToken.deposit(1e18, address(this));

    // warp past uint32.max (2106-02-07 06:28:16 UTC)
    vm.warp(uint256(type(uint32).max) + 1);

    // 1) direct accrual path reverts
    vm.expectRevert(); // SafeCast: value doesn't fit in 32 bits
    rewardsDistributor.updateBeforeMintOrBurn(IERC20(address(depositToken)), address(this));

    // 2) claiming accrued rewards reverts -> unclaimed yield locked
    vm.expectRevert();
    rewardsDistributor.claimRewards(address(this), tokensArray);

    // 3) core token flows revert through the hook -> funds frozen
    vm.expectRevert();
    depositToken.deposit(1e18, address(this)); // _beforeTokenTransfer -> updateBeforeMintOrBurn reverts

    vm.expectRevert();
    depositToken.transfer(address(0xdead), 1); // updateBeforeTransfer reverts
}
```

Uncertainty note: I verified the reverting cast chain in `contracts/RewardsDistributor.sol` (`_calculateTokenIndex` → `toUint32`) and the storage struct `TokenState { uint224 index; uint32 timestamp }` in `contracts/storage/RewardsDistributorStorage.sol`. I was unable to view the exact hook call sites inside `DepositToken.sol`/`DebtToken.sol` within the tool budget; the contract docstring at `RewardsDistributor.sol:172-174` states the hooks are called by those contracts, but the PoC lines for deposit/transfer reverts assume the standard `_beforeTokenTransfer`/mint-burn hook wiring — those specific revert points should be confirmed against the full `DepositToken.sol`/`DebtToken.sol` sources.

### Citations

**File:** contracts/RewardsDistributor.sol (L175-191)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }

    /**
     * @notice Update indexes on pre-transfer
     * @dev Called by DepositToken and DebtToken contracts
     */
    function updateBeforeTransfer(IERC20 token_, address from_, address to_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, from_);
            _updateTokensAccruedOf(token_, to_);
        }
```

**File:** contracts/RewardsDistributor.sol (L200-211)
```text
    ) private view returns (uint224 _newIndex, uint32 _newTimestamp) {
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
        } else if (_deltaTimestamps > 0 && _supplyState.index > 0) {
            _newTimestamp = block.timestamp.toUint32();
        }
```

**File:** contracts/RewardsDistributor.sol (L271-281)
```text
    function _updateTokenIndex(IERC20 token_) private {
        TokenState storage _supplyState = tokenStates[token_];
        (uint224 _newIndex, uint32 _newTimestamp) = _calculateTokenIndex(_supplyState, token_);
        if (_newIndex > 0 && _newTimestamp > 0) {
            _supplyState.index = _newIndex;
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_newIndex, _newTimestamp);
        } else if (_newTimestamp > 0) {
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_supplyState.index, _newTimestamp);
        }
```

**File:** contracts/dependencies/openzeppelin/utils/math/SafeCast.sol (L83-90)
```text
     * @dev Returns the downcasted uint32 from uint256, reverting on
     * overflow (when the input is greater than largest uint32).
     *
     * Counterpart to Solidity's `uint32` operator.
     *
     * Requirements:
     *
     * - input must fit into 32 bits
```
