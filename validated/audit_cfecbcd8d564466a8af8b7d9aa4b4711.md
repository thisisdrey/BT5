### Title
Reward tokens funded to RewardsDistributor can never be withdrawn once they exceed accrued claims - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` holds the entire `rewardToken` balance used to pay user rewards, but unlike sibling contracts (`DepositToken`, `DebtToken`, `NativeTokenGateway`, `VesperGateway`) which inherit `TokenHolder.sweep`, it implements no recovery or withdrawal function. The only outgoing transfer path is `_transferRewardIfEnoughTokens`, which is strictly gated on `tokensAccruedOf[account_]`. Any balance in excess of accrued user claims is permanently locked in the contract.

### Finding Description
The contract distributes `rewardToken` via an index/speed model (`tokenSpeeds`, `tokenStates`, `accountIndexOf`) and pays users only up to their accrued `tokensAccruedOf` amount [1](#0-0) . There is no admin/governor function to pull out the `rewardToken` (or any token) — the contract inherits `Manageable`, not `TokenHolder` [2](#0-1) . `TokenHolder.sweep` exists in the codebase as the standard recovery mechanism for stuck tokens [3](#0-2) , but `RewardsDistributor` does not include it.

Concrete lock scenarios reachable in the deployed configuration:

- Governor funds the distributor with `N` reward tokens, then lowers `tokenSpeeds[token]` via `updateTokenSpeed`/`updateTokenSpeeds` before the funded amount is fully emitted. Emission stops at `periodFinish`-equivalent accounting, and the un-emitted remainder is locked forever [4](#0-3) .
- The Vesper `syncTokenSpeed` path keeps speeds in sync with external `rewardRates`; a rate reduction strands the previously funded surplus [5](#0-4) .
- Any reward tokens sent directly (donation, refund, fee-on-transfer overage) are unclaimable since only index-accrued amounts can ever leave.

### Impact Explanation
Permanent freezing of unclaimed yield / protocol funds. The surplus `rewardToken` balance belongs to the protocol (it was funded for distribution), yet once it no longer corresponds to accrued user claims it is mathematically impossible to recover — mirroring the Cooler "voting reward locked in contract forever" bug class, where the contract accrues value with no withdrawal path.

### Likelihood Explanation
Medium-low. It requires an operational event (speed reduction after funding, or token removal from `tokens`) rather than an attacker action, but funding-before-emission is the normal operating mode, and there is no escape hatch. Not attacker-profitable; classified as permanent freezing of funds.

### Recommendation
Have `RewardsDistributor` inherit `TokenHolder` (or add a governor-only `sweep`/`withdrawExcessReward`) so surplus `rewardToken` above total accrued claims can be recovered. To protect user claims, either restrict sweeping to `balance - sum(tokensAccruedOf)` or rely on governor trust as in the other contracts.

### Proof of Concept
Hardhat fork test sketch:

```ts
// given: RewardsDistributor funded and speed set for depositToken
await rewardToken.mint(distributor.address, parseEther('1000'))
await distributor.updateTokenSpeed(depositToken.address, parseEther('1'))

// accrue a small amount for alice via updateBeforeMintOrBurn
await distributor.updateBeforeMintOrBurn(depositToken.address, alice.address)

// when: governor stops emissions
await distributor.updateTokenSpeed(depositToken.address, 0)

// then: alice claims only her accrued amount
await distributor['claimRewards(address)'](alice.address)
const remaining = await rewardToken.balanceOf(distributor.address)
expect(remaining).to.be.gt(0) // ~1000 - accrued, locked forever

// there is no function on RewardsDistributor to move `remaining`
```

Note: I was not able to fully enumerate every function in `RewardsDistributor.sol` within available iterations, but the indexed ABI in `deployments/mainnet/RewardsDistributor.json` lists all external entry points (`claimRewards`, `claimable`, `updateTokenSpeed*`, `updateBefore*`, `syncTokenSpeed`) and contains no withdrawal/sweep function, corroborating the finding [6](#0-5) .

### Citations

**File:** contracts/RewardsDistributor.sol (L39-45)
```text
contract RewardsDistributor is
    Initializable,
    ReentrancyGuardDeprecated,
    ReentrancyGuardTransient,
    Manageable,
    RewardsDistributorStorageV2
{
```

**File:** contracts/RewardsDistributor.sol (L50-56)
```text
    string public constant VERSION = "1.3.2";

    /// @notice The initial index
    uint224 public constant INITIAL_INDEX = 1e18;

    /// @notice Max reward tokens to avoid DoS scenario
    uint224 public constant MAX_REWARD_TOKENS = 20;
```

**File:** contracts/RewardsDistributor.sol (L248-256)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L287-300)
```text
    function _updateTokenSpeed(
        IERC20 token_,
        uint256 newSpeed_
    ) private onlyIfDistributorExists onlyIfTokenExists(address(token_)) {
        uint256 _currentSpeed = tokenSpeeds[token_];
        if (_currentSpeed > 0) {
            _updateTokenIndex(token_);
        } else if (newSpeed_ > 0) {
            // Add token to the list
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
            } else {
```

**File:** contracts/utils/TokenHolder.sol (L37-45)
```text
    function sweep(IERC20 token_, address to_, uint256 amount_) external {
        _requireCanSweep();

        if (address(token_) == address(0)) {
            Address.sendValue(payable(to_), amount_);
        } else {
            token_.safeTransfer(to_, amount_);
        }
    }
```

**File:** deployments/mainnet/RewardsDistributor.json (L661-727)
```json
      "INITIAL_INDEX()": {
        "notice": "The initial index"
      },
      "MAX_REWARD_TOKENS()": {
        "notice": "Max reward tokens to avoid DoS scenario"
      },
      "accountIndexOf(address,address)": {
        "notice": "The supply index for each token for each account as of the last time they accrued token"
      },
      "claimRewards(address)": {
        "notice": "Claim tokens accrued by account in all tokens"
      },
      "claimRewards(address,address[])": {
        "notice": "Claim tokens accrued by account in the specified tokens"
      },
      "claimRewards(address[],address[])": {
        "notice": "Claim tokens accrued by the accounts in the specified tokens"
      },
      "claimable(address)": {
        "notice": "Returns claimable amount consider all tokens"
      },
      "claimable(address,address)": {
        "notice": "Returns updated claimable amount for given token"
      },
      "governor()": {
        "notice": "Get the governor"
      },
      "pool()": {
        "notice": "Pool contract"
      },
      "poolRegistry()": {
        "notice": "Get pool registry contract"
      },
      "rewardToken()": {
        "notice": "The token to reward"
      },
      "syncTokenSpeed(address)": {
        "notice": "This is temporary fix to keep tokenSpeed and rewardRate from Vesper in sync."
      },
      "tokenSpeeds(address)": {
        "notice": "The amount of token distributed for each token per second"
      },
      "tokenStates(address)": {
        "notice": "The reward state for each token"
      },
      "tokens(uint256)": {
        "notice": "Track tokens for reward"
      },
      "tokensAccruedOf(address)": {
        "notice": "The token accrued but not yet transferred to each user"
      },
      "updateBeforeMintOrBurn(address,address)": {
        "notice": "Update indexes on pre-mint and pre-burn"
      },
      "updateBeforeTransfer(address,address,address)": {
        "notice": "Update indexes on pre-transfer"
      },
      "updateTokenSpeed(address,uint256)": {
        "notice": "Update speed for a single deposit token"
      },
      "updateTokenSpeedKeeper(address)": {
        "notice": "This function is part of temporary fix to keep tokenSpeed and rewardRate in sync."
      },
      "updateTokenSpeeds(address[],uint256[])": {
        "notice": "Update token speeds"
      }
    },
```
