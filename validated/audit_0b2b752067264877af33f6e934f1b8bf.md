### Title
Rewards for periods with zero token supply are permanently skipped - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` advances a reward token’s checkpoint timestamp even when the tracked `DepositToken` or `DebtToken` has zero supply. Because the index does not accrue during that interval, rewards corresponding to `elapsed * tokenSpeed` are never allocated to any account and become permanently unclaimable inside the distributor.

### Finding Description
The strongest analog is `RewardsDistributor._calculateTokenIndex`. When `tokenSpeeds[token_] > 0` and time has elapsed, it computes a zero ratio whenever `token_.totalSupply() == 0`, but still returns `block.timestamp` as the new checkpoint timestamp. [1](#0-0) 

`_updateTokenIndex` persists that timestamp even when the index remains unchanged. [2](#0-1) 

This state transition is reachable by any unprivileged caller through `updateBeforeMintOrBurn`, which is explicitly documented as callable by anyone and has no caller or registered-token restriction. [3](#0-2)  It is also reached automatically before deposit-token mints, burns, and transfers, and before debt-token mints and burns. [4](#0-3) [5](#0-4) 

No pause, health check, reentrancy guard, supply cap, or privileged modifier prevents the zero-supply timestamp update.

### Impact Explanation
The reward-accrual invariant breaks: every elapsed second with nonzero `tokenSpeed` should either accrue index value or remain pending for the next non-zero supply checkpoint.

Instead, for a token with `speed > 0`, an interval of `T` seconds at zero supply permanently loses `T * speed` reward accrual. The reward tokens remain in `RewardsDistributor`, but no later depositor or borrower can claim them because the global index never increases for that interval. `RewardsDistributor` does not inherit `TokenHolder` or expose a sweep/recovery function, so the skipped amount has no normal withdrawal path and remains locked unless governance performs an implementation upgrade or out-of-band correction. [6](#0-5) 

This is permanent freezing of unclaimed yield. It also creates an unfair restart condition: the first minter after an empty period receives rewards only from the new timestamp, not from the period during which emissions were configured.

### Likelihood Explanation
The condition requires a configured reward token to have nonzero `tokenSpeed` while the corresponding `DepositToken.totalSupply()` or `DebtToken.totalSupply()` is zero. This can happen naturally before first use or after all deposits are withdrawn or all debt is repaid.

Once the state exists, any EOA can finalize the loss by calling:

```solidity
rewardsDistributor.updateBeforeMintOrBurn(token, attacker);
```

No privileged role is required. [7](#0-6)  The same update also occurs automatically when a user later deposits or borrows because minting calls `updateBeforeMintOrBurn` before increasing supply. [8](#0-7) [9](#0-8) 

### Recommendation
Do not advance `tokenStates[token_].timestamp` for reward accrual while `token_.totalSupply() == 0` and `tokenSpeeds[token_] > 0`.

For example, in `_calculateTokenIndex`, return no new timestamp for the zero-supply case, or keep the old timestamp until supply becomes nonzero:

```solidity
uint256 _totalSupply = token_.totalSupply();

if (_deltaTimestamps > 0 && _speed > 0 && _totalSupply > 0) {
    uint256 _tokensAccrued = _deltaTimestamps * _speed;
    _newIndex = (_supplyState.index + _tokensAccrued.wadDiv(_totalSupply)).toUint224();
    _newTimestamp = block.timestamp.toUint32();
} else if (_deltaTimestamps > 0 && _supplyState.index > 0 && _speed == 0) {
    _newTimestamp = block.timestamp.toUint32();
}
```

An alternative is to preserve the old timestamp only while supply is zero, so emissions resume from the last non-empty checkpoint and remain attributable to the next suppliers.

### Proof of Concept
The following Foundry fork test demonstrates the state transition against deployed state. It selects a configured reward-tracking token whose current supply is zero, advances time, calls the public update function, and proves that the timestamp advances while the index does not.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

interface IERC20Like {
    function totalSupply() external view returns (uint256);
}

interface IRewardsDistributorLike {
    function tokens(uint256 i) external view returns (address);
    function tokenSpeeds(address token) external view returns (uint256);
    function tokenStates(address token)
        external
        view
        returns (uint224 index, uint32 timestamp);
    function updateBeforeMintOrBurn(address token, address account) external;
}

contract RewardsDistributorEmptySupplyTest is Test {
    // deployments/mainnet/RewardsDistributor.json
    IRewardsDistributorLike constant DISTRIBUTOR =
        IRewardsDistributorLike(0x9585D3706758b251E37541D808e3Ea11EAd5b819);

    function test_zeroSupplyTimestampSkipsRewards() public {
        address token;
        uint256 speed;

        // Find a configured token with active emissions and zero supply.
        for (uint256 i = 0; i < 20; ++i) {
            try DISTRIBUTOR.tokens(i) returns (address candidate) {
                uint256 candidateSpeed = DISTRIBUTOR.tokenSpeeds(candidate);
                uint256 candidateSupply = IERC20Like(candidate).totalSupply();

                if (candidateSpeed > 0 && candidateSupply == 0) {
                    token = candidate;
                    speed = candidateSpeed;
                    break;
                }
            } catch {
                break;
            }
        }

        require(token != address(0), "fork block has no empty active reward token");

        (uint224 indexBefore, uint32 timestampBefore) =
            DISTRIBUTOR.tokenStates(token);

        vm.warp(block.timestamp + 1 days);

        address attacker = makeAddr("attacker");
        vm.prank(attacker);
        DISTRIBUTOR.updateBeforeMintOrBurn(token, attacker);

        (uint224 indexAfter, uint32 timestampAfter) =
            DISTRIBUTOR.tokenStates(token);

        assertGt(timestampAfter, timestampBefore);
        assertEq(indexAfter, indexBefore);

        uint256 skippedRewards = uint256(timestampAfter - timestampBefore) * speed;
        assertGt(skippedRewards, 0);
    }
}
```

The key assertion is that `timestampAfter > timestampBefore` while `indexAfter == indexBefore`: the elapsed emissions are consumed by the checkpoint but allocated to nobody.

### Citations

**File:** contracts/RewardsDistributor.sol (L39-50)
```text
contract RewardsDistributor is
    Initializable,
    ReentrancyGuardDeprecated,
    ReentrancyGuardTransient,
    Manageable,
    RewardsDistributorStorageV2
{
    using SafeERC20 for IERC20;
    using SafeCast for uint256;
    using WadRayMath for uint256;

    string public constant VERSION = "1.3.2";
```

**File:** contracts/RewardsDistributor.sol (L170-180)
```text
    /**
     * @notice Update indexes on pre-mint and pre-burn
     * @dev Called by DepositToken and DebtToken contracts
     * This function also may be called by anyone to update stored indexes
     */
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L197-211)
```text
    function _calculateTokenIndex(
        TokenState memory _supplyState,
        IERC20 token_
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

**File:** contracts/DepositToken.sol (L120-144)
```text
    /**
     * @notice Update reward contracts' states
     * @dev Should be called before balance changes (i.e. mint/burn)
     */
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
    }

    /**
     * @notice Update reward contracts' states
     * @dev Should be called before balance changes (i.e. transfer)
     */
    modifier updateRewardsBeforeTransfer(address sender_, address recipient_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeTransfer(this, sender_, recipient_);
        }
        _;
    }
```

**File:** contracts/DepositToken.sol (L469-476)
```text
    function _mint(
        address account_,
        uint256 amount_
    ) private onlyIfDepositTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToTheZeroAddress();

        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxDepositSupply();
```

**File:** contracts/DebtToken.sol (L110-121)
```text
    /**
     * @notice Update reward contracts' states
     * @dev Should be called before balance changes (i.e. mint/burn)
     */
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
    }
```

**File:** contracts/DebtToken.sol (L572-590)
```text
    function _mint(
        IPool pool_,
        IMasterOracle masterOracle_,
        address account_,
        uint256 amount_
    ) private onlyIfDebtTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToNullAddress();

        uint256 _debtFloorInUsd = pool_.debtFloorInUsd();
        uint256 _balanceBefore = balanceOf(account_);

        if (
            _debtFloorInUsd > 0 &&
            masterOracle_.quoteTokenToUsd(address(syntheticToken), _balanceBefore + amount_) < _debtFloorInUsd
        ) {
            revert DebtLowerThanTheFloor();
        }

        totalSupply_ += amount_;
```
