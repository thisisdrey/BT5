### Title
Rewards claimed via `Treasury.claimFromVesper` that are not enumerated in `getRewardTokens()` are permanently stuck in the Treasury - ([File: contracts/Treasury.sol](contracts/Treasury.sol))

### Summary
`Treasury.claimFromVesper` claims all rewards a Vesper `poolRewards` contract pushes to the Treasury, but only sweeps the tokens returned by `getRewardTokens()`. Any reward token sent by `claimReward` that is not present in that enumerated list (the analog of Aura "stash" secondary rewards, which are paid out but not reported in the primary reward token list) remains in the Treasury forever, because the contract has no generic recovery function.

### Finding Description
In `Treasury.sol` the flow is:

```solidity
_rewards.updateReward(address(this));
_rewards.claimReward(address(this));          // pushes every claimable reward token

address[] memory _rewardTokens = _rewards.getRewardTokens();
for (uint256 i; i < _len; ++i) {              // iterates ONLY the listed tokens
    IERC20 _token = IERC20(_rewardTokens[i]);
    uint256 _amount = _token.balanceOf(address(this));
    IDepositToken _depositToken = _pool.depositTokenOf(_token);
    if (address(_depositToken) != address(0)) {
        _amount -= _depositToken.totalSupply();
    }
    if (_amount > 0) {
        _token.safeTransfer(to_, _amount);
    }
}
``` [1](#0-0) 

Two accounting gaps exist:

1. **Non-enumerated reward tokens are never swept.** Vesper `PoolRewards`-style contracts can pay additional reward tokens whose addresses are not part of the fixed `getRewardTokens()` list (exactly the same bug class as Aura stash AURA). `claimReward(address(this))` transfers all accrued rewards to the Treasury, but the sweep loop only iterates the enumerated list, so the extra tokens stay in the contract.

2. **No escape hatch exists.** `Treasury` inherits `Initializable`, `ReentrancyGuardDeprecated`, `ReentrancyGuardTransient`, `Manageable`, and `TreasuryStorageV1` — notably it does **not** inherit `TokenHolder`, so there is no `sweep()` recovery function. [2](#0-1) 
   The only outbound paths are:
   - `pull()` — restricted to `underlying()` of a registered `DepositToken` and callable only by that DepositToken. [3](#0-2) 
   - `migrateTo()` — restricted to `onlyPool` and only transfers `underlying()` of each registered `DepositToken`. [4](#0-3) 

   Neither can move an arbitrary ERC20 reward token.

Even for an enumerated token, if the reward token is itself a collateral, `_amount -= _depositToken.totalSupply()` will underflow-revert whenever the reward balance is smaller than the backing total supply — but that only delays the claim; the deeper issue is unlisted tokens.

### Impact Explanation
Secondary/extra reward tokens paid by a Vesper pool's `poolRewards` contract become permanently frozen in the `Treasury`. This is loss of yield owed to the protocol (the same "rewards remain in the protocol and cannot be withdrawn" impact as the AuraStash finding). Since no unprivileged or privileged function can extract non-underlying, non-listed tokens, the funds are effectively burned unless the implementation is upgraded.

### Likelihood Explanation
Moderate. The path is triggered by the governor calling `claimFromVesper`, but the stuck condition is created automatically whenever `claimReward` emits a token absent from `getRewardTokens()` — a configuration Vesper reward contracts support (extra/multi-reward programs, token migration, or stale reward lists). No attacker action is required; however, an unprivileged user cannot force the claim. The defect is deterministic once such a reward token exists.

### Recommendation
- After `claimReward`, sweep tokens by a whitelist-free method: e.g., track Treasury balances of all reward tokens via `balanceOf` deltas for tokens supplied by an explicit `IERC20[] calldata extraTokens_` parameter, or add a `TokenHolder`-style `sweep()` guarded by `onlyGovernor` that refuses to sweep registered collateral underlyings below their backing (`balanceOf - depositToken.totalSupply()` surplus only).
- Alternatively, compare the pre/post `claimReward` balance diff per token rather than the whole balance, to avoid mixing reward amounts with collateral backing.

### Proof of Concept
Hardhat sketch (mock pattern already used in `test/RewardDistributor.test.ts` and `contracts/mock/PoolRewardsMock.sol`):

```ts
// Fork or mock setup:
// 1. Deploy Treasury initialized with a real/mock Pool.
// 2. Configure PoolRewardsMock with rewardTokens = [VSP] only.
// 3. Extend/mock claimReward so it transfers BOTH VSP and EXTRA (e.g. a stash-style token)
//    to the Treasury, mimicking a secondary reward not in getRewardTokens().
const vsp = await deploy('ERC20Mock', ['VSP','VSP',18])
const extra = await deploy('ERC20Mock', ['EXTRA','EXTRA',18])
await vsp.mint(poolRewards.address, amt)
await extra.mint(poolRewards.address, amt)
await poolRewards.setRewardTokens([vsp.address])

// 4. Governor calls claimFromVesper
await treasury.connect(governor).claimFromVesper(vPool.address, governor.address)

// 5. Assertions
expect(await vsp.balanceOf(treasury.address)).eq(0)          // listed token swept
expect(await extra.balanceOf(treasury.address)).eq(amt)     // unlisted token stuck
// No function on Treasury can move `extra`: pull() reverts SenderIsNotDepositToken,
// migrateTo() only moves registered underlyings, and there is no sweep().
```

A live-fork variant uses a real Vesper pool whose `poolRewards` contract distributes a token absent from `getRewardTokens()`.

### Citations

**File:** contracts/Treasury.sol (L25-25)
```text
contract Treasury is Initializable, ReentrancyGuardDeprecated, ReentrancyGuardTransient, Manageable, TreasuryStorageV1 {
```

**File:** contracts/Treasury.sol (L44-59)
```text
    function migrateTo(address newTreasury_) external override onlyPool {
        if (newTreasury_ == address(0)) revert AddressIsNull();

        address[] memory _depositTokens = pool.getDepositTokens();
        uint256 _len = _depositTokens.length;

        for (uint256 i; i < _len; ++i) {
            IERC20 _underlying = IDepositToken(_depositTokens[i]).underlying();

            uint256 _underlyingBalance = _underlying.balanceOf(address(this));

            if (_underlyingBalance > 0) {
                _underlying.safeTransfer(newTreasury_, _underlyingBalance);
            }
        }
    }
```

**File:** contracts/Treasury.sol (L66-72)
```text
    function pull(address to_, uint256 amount_) external override nonReentrant {
        address _msgSender = _msgSender();
        if (!pool.doesDepositTokenExist(IDepositToken(_msgSender))) revert SenderIsNotDepositToken();
        if (to_ == address(0)) revert RecipientIsNull();
        if (amount_ == 0) revert AmountIsZero();
        IDepositToken(_msgSender).underlying().safeTransfer(to_, amount_);
    }
```

**File:** contracts/Treasury.sol (L79-101)
```text
    function claimFromVesper(IVPool vPool_, address to_) external onlyGovernor {
        IPoolRewards _rewards = IPoolRewards(vPool_.poolRewards());
        _rewards.updateReward(address(this));
        _rewards.claimReward(address(this));

        IPool _pool = pool;
        address[] memory _rewardTokens = _rewards.getRewardTokens();
        uint256 _len = _rewardTokens.length;
        for (uint256 i; i < _len; ++i) {
            IERC20 _token = IERC20(_rewardTokens[i]);
            uint256 _amount = _token.balanceOf(address(this));

            // Note: If the reward token is a collateral, transfer the surpass balance only
            IDepositToken _depositToken = _pool.depositTokenOf(_token);
            if (address(_depositToken) != address(0)) {
                _amount -= _depositToken.totalSupply();
            }

            if (_amount > 0) {
                _token.safeTransfer(to_, _amount);
            }
        }
    }
```
