# Synthetic demo fixtures

Every file in this directory was manually constructed for software testing. The values do not come from a real electrochemical experiment, do not describe a real sample, and are not copied from a vendor installation or private export.

| File | Expected result | Points | SHA-256 |
|---|---|---:|---|
| `chi_cv_demo.txt` | CHI / CV / `Potential/V` × `Current/A` | 29 | `04a8daf2e0d00aa5aa311367cc59769a4576d99142a2e8176ae80bb6a7ce67bc` |
| `chi_ocpt_demo.bin` | CHI / OCP / metadata only | 0 | `3fb4f483d0ddb0b95eaeaa29a7bafec844a7068d7bc6a14f1547a929d4d55157` |
| `corrtest_eis_demo.z60` | CorrTest / EIS / `Zreal(ohm)` × `Zimag(ohm)` | 15 | `e3509b323ec12a4810e0bf2dc45ac64b677279b903aa5c994e5c47910d8e4534` |
| `corrtest_galstatic_demo.cor` | CorrTest / CP/GCD / `T(s)` × `E(V)` | 11 | `70b30d74a76be2986bd6e5184508623cbe7b976f1d7d697430a47ffcaa6250d7` |

`chi_ocpt_demo.bin` contains a short ASCII placeholder even though its suffix is `.bin`. It verifies that the application records binary-designated CHI files by metadata and SHA-256 only. It is not an example of a real CHI binary format.

Run the bounded demonstration from the repository root:

```sh
python3 scripts/run_demo.py
```

Do not replace these files with laboratory data. Follow [../DATA_POLICY.md](../DATA_POLICY.md) when proposing another fixture.
