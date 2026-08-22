### No vulnerability found for this question.

`newNISTCurve` only constructs a `nistCurve` value holding static, non-secret configuration—`name`, `curve` (the `ecdh.Curve` implementation), `dhLen`, and `pubLen`—and is invoked once at package load to build the global `DHP256` var [1](#0-0) . It carries no per-session key material, private keys, or shared secrets; actual key bytes are only produced transiently inside `GenerateKeypair` and `DH`, which return byte slices to the caller (the noise library) rather than storing them in the `nistCurve` receiver [2](#0-1) . Since there is no retained key state in this struct or function, there is nothing here that could remain "bound to a reusable index" after an abnormal session teardown, and the premise of the question (key material lifetime tied to a duplicated-counter abnormal close) does not apply to this code path.

### Citations

**File:** noiseutil/nist.go (L12-30)
```go
// DHP256 is the NIST P-256 ECDH function
var DHP256 noise.DHFunc = newNISTCurve("P256", ecdh.P256(), 32)

type nistCurve struct {
	name   string
	curve  ecdh.Curve
	dhLen  int
	pubLen int
}

func newNISTCurve(name string, curve ecdh.Curve, byteLen int) nistCurve {
	return nistCurve{
		name:  name,
		curve: curve,
		dhLen: byteLen,
		// Standard uncompressed format, type (1 byte) plus both coordinates
		pubLen: 1 + 2*byteLen,
	}
}
```

**File:** noiseutil/nist.go (L32-55)
```go
func (c nistCurve) GenerateKeypair(rng io.Reader) (noise.DHKey, error) {
	if rng == nil {
		rng = rand.Reader
	}
	privkey, err := c.curve.GenerateKey(rng)
	if err != nil {
		return noise.DHKey{}, err
	}
	pubkey := privkey.PublicKey()
	return noise.DHKey{Private: privkey.Bytes(), Public: pubkey.Bytes()}, nil
}

func (c nistCurve) DH(privkey, pubkey []byte) ([]byte, error) {
	ecdhPubKey, err := c.curve.NewPublicKey(pubkey)
	if err != nil {
		return nil, fmt.Errorf("unable to unmarshal pubkey: %w", err)
	}
	ecdhPrivKey, err := c.curve.NewPrivateKey(privkey)
	if err != nil {
		return nil, fmt.Errorf("unable to unmarshal private key: %w", err)
	}

	return ecdhPrivKey.ECDH(ecdhPubKey)
}
```
