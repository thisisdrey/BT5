[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L24-24)
```text
  mapping(address addressBookOwner => mapping(address grantee => uint256 roleBitmask)) public addressBook;
```

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L30-42)
```text
  /// @inheritdoc ILoansAuth
  function registerAddress(Roles role, address addr) external {
    // forge-lint: disable-next-line(incorrect-shift)
    addressBook[msg.sender][addr] |= (1 << uint8(role));
    emit AddressRegistered(msg.sender, role, addr);
  }

  /// @inheritdoc ILoansAuth
  function unregisterAddress(Roles role, address addr) external {
    // forge-lint: disable-next-line(incorrect-shift)
    addressBook[msg.sender][addr] &= ~(1 << uint8(role));
    emit AddressUnregistered(msg.sender, role, addr);
  }
```

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L46-66)
```text
  /// @inheritdoc ILoansAuth
  function registerAddressOnBehalfOf(address addressBookOwner, Roles role, address addr) external onlyAdminOrGuardian {
    require(addressBookOwner != address(this), InvalidAddressBookOwner());

    // forge-lint: disable-next-line(incorrect-shift)
    addressBook[addressBookOwner][addr] |= (1 << uint8(role));
    emit AddressRegistered(addressBookOwner, role, addr);
  }

  /// @inheritdoc ILoansAuth
  function unregisterAddressOnBehalfOf(
    address addressBookOwner,
    Roles role,
    address addr
  ) external onlyAdminOrGuardian {
    require(addressBookOwner != address(this), InvalidAddressBookOwner());

    // forge-lint: disable-next-line(incorrect-shift)
    addressBook[addressBookOwner][addr] &= ~(1 << uint8(role));
    emit AddressUnregistered(addressBookOwner, role, addr);
  }
```

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L68-94)
```text
  /// @inheritdoc ILoansAuth
  function approveOriginator(address user) public onlyRole(GUARDIAN_ROLE) {
    addressBook[address(this)][user] |= ORIGINATOR_MASK;
    emit AddressRegistered(address(this), Roles.Originator, user);
    emit OriginatorApproved(user);
  }

  /// @inheritdoc ILoansAuth
  function revokeOriginator(address user) public onlyAdminOrGuardian {
    addressBook[address(this)][user] &= ~ORIGINATOR_MASK;
    emit AddressUnregistered(address(this), Roles.Originator, user);
    emit OriginatorRevoked(user);
  }

  /// @inheritdoc ILoansAuth
  function approveServicer(address user) public onlyRole(GUARDIAN_ROLE) {
    addressBook[address(this)][user] |= SERVICER_MASK;
    emit AddressRegistered(address(this), Roles.Servicer, user);
    emit ServicerApproved(user);
  }

  /// @inheritdoc ILoansAuth
  function revokeServicer(address user) public onlyAdminOrGuardian {
    addressBook[address(this)][user] &= ~SERVICER_MASK;
    emit AddressUnregistered(address(this), Roles.Servicer, user);
    emit ServicerRevoked(user);
  }
```

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L98-101)
```text
  /// @inheritdoc ILoansAuth
  function isRegisteredForRole(address addressBookOwner, Roles role, address addr) public view returns (bool) {
    return ((addressBook[addressBookOwner][addr] >> uint8(role)) & 1 == 1);
  }
```
