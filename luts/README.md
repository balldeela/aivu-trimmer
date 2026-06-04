# Color LUTs

Drop `.cube` 3D LUT files in this folder and they'll appear in the **Color LUT**
dropdown, applied during the **side-by-side MP4 (Quest)** export.

> LUTs are **not** applied to the lossless `.aivu` export — that path copies the
> original bitstream without re-encoding, so there's nothing to color-transform.

## Blackmagic Cine (Gen 5) → Rec709

The Blackmagic URSA Cine Immersive records in **Blackmagic Design Film Gen 5**
color science. The matching display conversions are:

| LUT | Use |
|---|---|
| `Blackmagic Gen 5 Film to Video.cube` | Film → Rec709 (legal / broadcast range) |
| `Blackmagic Gen 5 Film to Extended Video.cube` | Film → Rec709 (full range) |

These ship with **DaVinci Resolve** (free) at:

```
/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/Blackmagic Design/
```

They are **not redistributed in this repo** (they're Blackmagic's). If this folder
has no `.cube` files, the app automatically falls back to discovering the Gen 5
LUTs from your DaVinci Resolve installation. To bundle them locally instead, copy
them here.

## Important caveat

Only apply a Film→Rec709 LUT if your source `.aivu` is in **log / Film gamma**.
If the footage is already display-ready (graded Rec709), applying the LUT will
**double-correct** it (crushed, over-contrasty image). When in doubt, leave the
dropdown on **No LUT** and compare.
