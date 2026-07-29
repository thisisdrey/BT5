### Lack of Quantitative Allowance for Operators in Async Vault Flows - ([File: tare-io__tare-contracts/contracts/PortfolioVault.sol])

### Summary
The `PortfolioVault` contract implements ERC-7540 asynchronous deposit and redemption flows but uses a boolean operator authorization model instead of a quantitative allowance. This allows any authorized operator to claim or cancel the entire pending or claimable balance of a controller, rather than a specific amount intended by the controller. [1](#0-0) 

### Finding Description
The `PortfolioVault` contract uses a modifier `onlyAccountOrOperator` to authorize third-party callers for sensitive async vault operations. [2](#0-1)  This modifier checks the `_isOperator` mapping, which stores a boolean approval status for an operator on behalf of a controller. [1](#0-0) 

When a shareholder sets an operator using `setOperator(operator, true)`, they grant that operator full permission to act on their behalf for all `controller` functions. [3](#0-2)  Consequently, in functions like `redeem`, `withdraw`, `deposit`, and `mint`, the operator can specify any amount up to the controller's total claimable limit. [4](#0-3) 

The EIP-4626 (and by extension EIP-7540) standard intends for third-party spenders to be restricted by an allowance. By using a boolean flag, the vault forces an "all-or-nothing" trust model, which is a material deviation from standard token/vault spending patterns and prevents users from safely delegating partial claims.

### Impact Explanation
An authorized operator can drain a controller's entire claimable asset balance (in the case of `redeem`/`withdraw`) or claimable share balance (in the case of `deposit`/`mint`). [5](#0-4)  While the `receiver` must still be a verified investor (holding `SHAREHOLDER_ROLE`), a malicious or compromised operator can redirect assets to an honest but unintended investor address or simply execute a full redemption when only a partial one was authorized. [6](#0-5)  This constitutes an unauthorized state transition and permission bypass where an actor acts for another shareholder beyond the intended scope.

### Likelihood Explanation
The likelihood is medium as it requires a user to have granted operator status to another entity. However, in the Tare ecosystem, the use of delegates and "hot wallets" for automated operations makes this trust assumption significant. The lack of granular control is a direct violation of the expected security properties of vault allowances.

### Recommendation
Replace the `_isOperator` boolean mapping with a quantitative allowance mapping: `mapping(address controller => mapping(address operator => uint256 allowance))`. Update the `onlyAccountOrOperator` modifier to check and decrement this allowance when the caller is not the controller. Add an `approveOperator(address operator, uint256 amount)` function to manage these allowances.

### Proof of Concept
1. Shareholder A has 10,000 claimable shares in the vault.
2. Shareholder A wants to allow Operator B to redeem only 1,000 shares.
3. Shareholder A calls `setOperator(OperatorB, true)` because there is no amount-based approval. [3](#0-2) 
4. Operator B (malicious or compromised) calls `redeem(10000, ShareholderA, ShareholderA)`. [4](#0-3) 
5. The `onlyAccountOrOperator(ShareholderA)` check passes because `_isOperator[ShareholderA][OperatorB]` is `true`. [7](#0-6) 
6. The vault burns all 10,000 shares and transfers the full asset value to Shareholder A, against their intent of a partial exit.

### Citations

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L140-140)
```text
  mapping(address controller => mapping(address operator => bool approved)) internal _isOperator;
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L237-240)
```text
  modifier onlyAccountOrOperator(address account) {
    require(msg.sender == account || _isOperator[account][msg.sender], Unauthorized());
    _;
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L687-691)
```text
  function setOperator(address operator, bool approved) external returns (bool) {
    _isOperator[msg.sender][operator] = approved;
    emit OperatorSet(msg.sender, operator, approved);
    return true;
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L753-757)
```text
  function redeem(
    uint256 shares,
    address receiver,
    address controller
  ) external nonReentrant whenNotPaused onlyAccountOrOperator(controller) returns (uint256 assets) {
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L758-759)
```text
    _requireInvestor(controller);
    _requireInvestor(receiver);
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L763-766)
```text
    require(shares > 0 && shares <= claimableShares_, ExceedsClaimable());

    assets = (shares * claimableAssets_) / claimableShares_;
    _claimRedeem(controller, receiver, assets, shares, claimableAssets_, claimableShares_);
```
