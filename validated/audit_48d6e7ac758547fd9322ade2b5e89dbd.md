No vulnerability found for this question.

The closest candidate uncovered in the Go standard library is the documented non-deterministic map-key ordering in `encoding/json/v2`'s `makeMapArshaler` marshal path (`src/encoding/json/v2/arshal_default.go:862-883`), which iterates map entries in randomized order unless the explicit `Deterministic` option is passed [1](#0-0) . However, this is intentional, explicitly documented behavior gated behind an experimental build tag (`goexperiment.jsonv2`) and an opt-in `Deterministic` option, not a defect: the package docs and `TestMapDeterminism` explicitly state that v2 map marshaling is non-deterministic by design (a deliberate change from v1) and that callers needing stability must set `json.Deterministic(true)` [2](#0-1) [3](#0-2) [4](#0-3) . On the unmarshal side, `encoding/json` (v1) and `encoding/json/v2` both handle duplicate object names deterministically (v1 keeps the last value, v2 rejects duplicates unless `AllowDuplicateNames` is set) [5](#0-4) [6](#0-5) .

This does not match the ASA-2025-004 primitive: the ibc-go bug involved the same bytes being deserialized inconsistently across independently-running validator nodes in a way reachable by an unprivileged channel opener, producing a state/consensus divergence with no opt-in required and no documentation warning users of the risk. The Go stdlib behavior found is (a) opt-in/documented, (b) gated behind an experimental flag not enabled in default builds, and (c) requires an application to deliberately skip the `Deterministic` option in a consensus-critical context — this is application misuse of a documented API, not a Go stdlib defect. No unguarded, reachable-by-default parsing/interpretation divergence with a concrete security effect (code execution, auth/integrity bypass, secret disclosure, or file write) was found in the scoped files.

### Citations

**File:** src/encoding/json/v2/arshal_default.go (L862-883)
```go
			switch {
			case !mo.Flags.Get(jsonflags.Deterministic) || n <= 1:
				for iter := va.Value.MapRange(); iter.Next(); {
					k.SetIterKey(iter)
					err := marshalKey(enc, k, mo)
					if err != nil {
						if mo.Flags.Get(jsonflags.CallMethodsWithLegacySemantics) &&
							errors.Is(err, jsontext.ErrNonStringName) && nillableLegacyKey && k.IsNil() {
							err = enc.WriteToken(jsontext.String(""))
						}
						if err != nil {
							if serr, ok := err.(*jsontext.SyntacticError); ok && serr.Err == jsontext.ErrNonStringName {
								err = newMarshalErrorBefore(enc, k.Type(), err)
							}
							return err
						}
					}
					v.SetIterValue(iter)
					if err := marshalVal(enc, v, mo); err != nil {
						return err
					}
				}
```

**File:** src/encoding/json/v2/arshal_default.go (L996-1013)
```go
			// Manually check for duplicate entries by virtue of whether the
			// unmarshaled key already exists in the destination Go map.
			// Consequently, syntactically different names (e.g., "0" and "-0")
			// will be rejected as duplicates since they semantically refer
			// to the same Go value. This is an unusual interaction
			// between syntax and semantics, but is more correct.
			if !nonDefaultKey && mapKeyWithUniqueRepresentation(k.Kind(), uo.Flags.Get(jsonflags.AllowInvalidUTF8)) {
				xd.Tokens.Last.DisableNamespace()
			}

			// In the rare case where the map is not already empty,
			// then we need to manually track which keys we already saw
			// since existing presence alone is insufficient to indicate
			// whether the input had a duplicate name.
			var seen reflect.Value
			if !uo.Flags.Get(jsonflags.AllowDuplicateNames) && va.Len() > 0 {
				seen = reflect.MakeMap(reflect.MapOf(k.Type(), emptyStructType))
			}
```

**File:** src/encoding/json/v2/arshal.go (L124-128)
```go
//   - A Go map is encoded as a JSON object, where each Go map key and value
//     is recursively encoded as a name and value pair in the JSON object.
//     The Go map key must encode as a JSON string, otherwise this results
//     in a [SemanticError]. The Go map is traversed in a non-deterministic order.
//     For deterministic encoding, consider using the [Deterministic] option.
```

**File:** src/encoding/json/v2/options.go (L141-161)
```go
// Deterministic specifies that marshaling the same input value will always
// serialize as the same output bytes.
//
// For example, Go maps are marshaled sorted by key.
//
// For native Go types, Determinism is guaranteed across different instances of
// identical binaries, but not across different builds of a program (such as
// different source or toolchain version, different GOOS/GOARCH, different
// build flags).
//
// A Go type with a custom marshaler should also respect the Deterministic
// option and serialize deterministically if it is true.
//
// This only affects marshaling and is ignored when unmarshaling.
func Deterministic(v bool) Options {
	if v {
		return jsonflags.Deterministic | 1
	} else {
		return jsonflags.Deterministic | 0
	}
}
```

**File:** src/encoding/json/v2_diff_test.go (L666-704)
```go
}

// In v1, maps are marshaled in a deterministic order.
// In v2, maps are marshaled in a non-deterministic order.
//
// The reason for the change is that v2 prioritizes performance and
// the guarantee that marshaling operates primarily in a streaming manner.
//
// The v2 API provides jsontext.Value.Canonicalize if stability is needed:
//
//	(*jsontext.Value)(&b).Canonicalize()
//
// Related issue:
//
//	https://go.dev/issue/7872
//	https://go.dev/issue/33714
func TestMapDeterminism(t *testing.T) {
	const iterations = 10
	in := map[int]int{0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9}

	for _, json := range jsonPackages {
		t.Run(path.Join("Marshal", json.Version), func(t *testing.T) {
			outs := make(map[string]bool)
			for range iterations {
				b, err := json.Marshal(in)
				if err != nil {
					t.Fatalf("json.Marshal error: %v", err)
				}
				outs[string(b)] = true
			}
			switch {
			case json.Version == "v1" && len(outs) != 1:
				t.Fatalf("json.Marshal encoded to %d unique forms, expected 1", len(outs))
			case json.Version == "v2" && len(outs) == 1:
				t.Logf("json.Marshal encoded to 1 unique form by chance; are you feeling lucky?")
			}
		})
	}
}
```

**File:** src/encoding/json/v2_diff_test.go (L811-827)
```go
//	https://go.dev/issue/48298
func TestDuplicateNames(t *testing.T) {
	for _, json := range jsonPackages {
		t.Run(path.Join("Unmarshal", json.Version), func(t *testing.T) {
			const in = `{"Name":1,"Name":2}`
			var got struct{ Name int }
			err := json.Unmarshal([]byte(in), &got)
			switch {
			case json.Version == "v1" && err != nil:
				t.Fatalf("json.Unmarshal error: %v", err)
			case json.Version == "v1" && got != struct{ Name int }{2}:
				t.Fatalf(`json.Unmarshal = %v, want {2}`, got)
			case json.Version == "v2" && err == nil:
				t.Fatal("json.Unmarshal error is nil, want non-nil")
			}
		})
	}
```
