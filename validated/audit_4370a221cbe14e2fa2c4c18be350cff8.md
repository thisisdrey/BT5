[1](#0-0) [2](#0-1)

### Citations

**File:** cert/ca_pool.go (L240-247)
```go
	if !c.CheckSignature(signer.Certificate.PublicKey()) {
		return nil, ErrSignatureMismatch
	}

	err = CheckCAConstraints(signer.Certificate, c)
	if err != nil {
		return nil, err
	}
```

**File:** cert/ca_pool.go (L308-342)
```go
	// If the signer has a limited set of ip ranges to issue from make sure the cert only contains a subset
	signingNetworks := signer.Networks()
	if len(signingNetworks) > 0 {
		for _, certNetwork := range networks {
			found := false
			for _, signingNetwork := range signingNetworks {
				if signingNetwork.Contains(certNetwork.Addr()) && signingNetwork.Bits() <= certNetwork.Bits() {
					found = true
					break
				}
			}

			if !found {
				return fmt.Errorf("certificate contained a network assignment outside the limitations of the signing ca: %s", certNetwork.String())
			}
		}
	}

	// If the signer has a limited set of subnet ranges to issue from make sure the cert only contains a subset
	signingUnsafeNetworks := signer.UnsafeNetworks()
	if len(signingUnsafeNetworks) > 0 {
		for _, certUnsafeNetwork := range unsafeNetworks {
			found := false
			for _, caNetwork := range signingUnsafeNetworks {
				if caNetwork.Contains(certUnsafeNetwork.Addr()) && caNetwork.Bits() <= certUnsafeNetwork.Bits() {
					found = true
					break
				}
			}

			if !found {
				return fmt.Errorf("certificate contained an unsafe network assignment outside the limitations of the signing ca: %s", certUnsafeNetwork.String())
			}
		}
	}
```
