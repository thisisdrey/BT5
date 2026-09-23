Based on the investigation, there is no valid Go analog to this vulnerability.

The mquery CVE (CVE-2020-35149) is a JavaScript-specific prototype pollution bug where `lib/utils.js`'s merge/clone allowed a `__proto__` key to pollute `Object.prototype`, giving attackers a path to alter behavior globally across the JS object graph. This bug class is fundamentally tied to JavaScript's prototype-based object model, where every object implicitly inherits from a shared, mutable `Object.prototype`.

Go has no prototype chain or implicit shared object inheritance, so the root cause cannot exist in Go. The closest structural analogs found in the Go standard library are map copy/clone/merge utilities:

- `maps.Copy` / `maps.Clone` in [1](#0-0)  perform straightforward key/value copies into a `map[K]V` — there is no shared global namespace or special key (`__proto__`, `constructor`, `prototype`) that these functions treat specially, so an attacker-controlled key like `"__proto__"` is just an ordinary string key with no elevated semantic meaning.
- The JSON v2 map-unmarshal merge logic in [2](#0-1)  merges JSON object keys into a Go `map[K]V`, but again, keys are inert data — there's no mechanism by which a decoded map key can affect unrelated objects, structs, or future decode operations.
- Generic reflect-based struct cloning in [3](#0-2)  operates on statically-typed Go struct fields by index/offset, not on attacker-supplied string keys, so there's no way to redirect a copy into an unintended field via a crafted key name.

Because Go's type system enforces static field access (structs) or flat key/value semantics (maps) with no shared mutable prototype object, there is no equivalent sink where an attacker-controlled key name could escalate to modifying behavior beyond the target data structure itself.

### No Vulnerability found for this question.

### Citations

**File:** src/maps/maps.go (L48-66)
```go
// Clone returns a copy of m.  This is a shallow clone:
// the new keys and values are set using ordinary assignment.
func Clone[M ~map[K]V, K comparable, V any](m M) M {
	// Preserve nil in case it matters.
	if m == nil {
		return nil
	}
	return clone(m).(M)
}

// Copy copies all key/value pairs in src adding them to dst.
// When a key in src is already present in dst,
// the value in dst will be overwritten by the value associated
// with the key in src.
func Copy[M1 ~map[K]V, M2 ~map[K]V, K comparable, V any](dst M1, src M2) {
	for k, v := range src {
		dst[k] = v
	}
}
```

**File:** src/encoding/json/v2/arshal_default.go (L1042-1063)
```go
				// Check if a pre-existing map entry value exists for this key.
				if v2 := va.MapIndex(k.Value); v2.IsValid() {
					if !uo.Flags.Get(jsonflags.AllowDuplicateNames) && (!seen.IsValid() || seen.MapIndex(k.Value).IsValid()) {
						// TODO: Unread the object name.
						name := xd.PreviousTokenOrValue()
						return newDuplicateNameError(dec.StackPointer(), nil, dec.InputOffset()-len64(name))
					}
					if !uo.Flags.Get(jsonflags.MergeWithLegacySemantics) {
						v.Set(v2)
					} else {
						v.SetZero()
					}
				} else {
					v.SetZero()
				}

				// Unmarshal the map entry value.
				err = unmarshalVal(dec, v, uo)
				va.SetMapIndex(k.Value, v.Value)
				if seen.IsValid() {
					seen.SetMapIndex(k.Value, reflect.Zero(emptyStructType))
				}
```

**File:** src/runtime/_mkmalloc/astutil/clone.go (L20-72)
```go
func cloneNode(n ast.Node) ast.Node {
	var clone func(x reflect.Value) reflect.Value
	set := func(dst, src reflect.Value) {
		src = clone(src)
		if src.IsValid() {
			dst.Set(src)
		}
	}
	clone = func(x reflect.Value) reflect.Value {
		switch x.Kind() {
		case reflect.Pointer:
			if x.IsNil() {
				return x
			}
			// Skip fields of types potentially involved in cycles.
			switch x.Interface().(type) {
			case *ast.Object, *ast.Scope:
				return reflect.Zero(x.Type())
			}
			y := reflect.New(x.Type().Elem())
			set(y.Elem(), x.Elem())
			return y

		case reflect.Struct:
			y := reflect.New(x.Type()).Elem()
			for i := 0; i < x.Type().NumField(); i++ {
				set(y.Field(i), x.Field(i))
			}
			return y

		case reflect.Slice:
			if x.IsNil() {
				return x
			}
			y := reflect.MakeSlice(x.Type(), x.Len(), x.Cap())
			for i := 0; i < x.Len(); i++ {
				set(y.Index(i), x.Index(i))
			}
			return y

		case reflect.Interface:
			y := reflect.New(x.Type()).Elem()
			set(y, x.Elem())
			return y

		case reflect.Array, reflect.Chan, reflect.Func, reflect.Map, reflect.UnsafePointer:
			panic(x) // unreachable in AST

		default:
			return x // bool, string, number
		}
	}
	return clone(reflect.ValueOf(n)).Interface().(ast.Node)
```
