# MD5 Version 12 Format Specification

**Version:** 2.1
**Date:** June 2026
**Status:** Exporter complete (tangent generation reworked, see Changelog); engine implementation in progress

---

## Changelog

### 2.1 (June 2026) — Tangent recomputation + shader cleanup

- **Exporter tangent source reworked.** v2.0 took tangents straight from
  Blender's `calc_tangents()`. That path desynced from the stored normals (it
  gathered tangents on the *evaluated* mesh, keyed by eval loop index, but
  assigned them by *raw*-mesh loop index), so when a modifier changed the loop
  layout the tangent was taken from the wrong loop. Exported tangents came out
  neither perpendicular to the stored normal nor aligned with the stored UVs —
  measured ~101° tangent-to-normal on a real mesh (should be 90°), producing
  UV-shell shading seams. The exporter now **recomputes** the tangent frame from
  the final exported geometry instead of trusting `calc_tangents()`. See
  "Tangent Generation" below.
- **Engine-side note clarified.** "No shader changes required" still holds, but
  *only* because the exported tangents are now orthonormal to the normals. A
  per-pixel cotangent (Schüler/Mikkelsen) frame was used in
  `interaction_uber.pixel` as a temporary bring-up workaround while the exporter
  was emitting bad tangents; with correct v12 tangents it is redundant and has
  been retired (reverted to the per-vertex path). See "No Shader Changes Required".
- **Validation tooling added.** `check_md5_tangents.py` and `check_uv_match.py`,
  documented under "Validation Tooling".
- **Important framing correction:** the exported tangents are a Lengyel-style
  orthonormal frame, *close to but not bit-identical to* true MikkTSpace. See
  "Tangent Generation" for the accuracy note.

---

## Overview

MD5 Version 12 is a backward-compatible extension of the idTech 4 MD5 format
(version 10) that adds per-vertex normals, orthonormal tangent frames, and vertex
colors to `md5mesh` files. The `md5anim` format is structurally identical and
always uses version 10.

The primary benefit is correct normal map rendering: the exporter writes a
per-vertex tangent frame that is orthonormal to the stored normal and consistent
with the UVs, so it aligns with what modern baking tools (Blender, Substance
Painter, Marmoset, xNormal) produce and eliminates the tangent basis mismatches
that cause subtle shading errors in v10. The frame is Lengyel-style and close to,
but not bit-identical to, true MikkTSpace (see "Tangent Generation").

---

## What Changed from Version 10

| Feature | v10 | v12 |
|---------|-----|-----|
| `MD5Version` header | `10` | `12` |
| Per-vertex normal | Not stored; derived from geometry | Stored in bone-local space |
| Per-vertex tangent | Not stored; derived from geometry | Orthonormal tangent in bone-local space (recomputed from geometry; ⊥ to normal) |
| Bitangent sign | Not stored | Stored as 4th tangent component (±1) |
| Vertex colors | Not possible (color/color2 used for skinning) | Optional `vertexcolor` block per mesh |
| `vert` line format | `( u v ) firstWeight weightCount` | `( u v ) firstWeight weightCount ( nx ny nz ) ( tx ty tz tw )` |
| `md5anim` format | unchanged | unchanged (always version 10) |
| joints block | unchanged | unchanged |
| tris block | unchanged | unchanged |
| weights block | unchanged | unchanged |

---

## Blender Exporter

### Export Options

The Blender exporter provides two checkboxes:

**MD5 Version 12** — When checked, writes `MD5Version 12` with per-vertex normals,
MikkTSpace tangents, and optional vertex colors. When unchecked, writes standard
v10 format.

**Use sharp edges** (default: ON) — When checked, vertices at edges marked sharp in
Blender are split during export. This is non-destructive: the Blender scene is
never modified. Sharp edges are read directly from Blender's edge flags
(`sharp_edge` attribute). Additionally, if a Smooth by Angle modifier or custom
normals produce different corner normals at a shared vertex, those are also split.
When unchecked, no edge splitting occurs regardless of sharp edge markings.
Works for both v10 and v12.

### Non-Destructive Export

The exporter never modifies the Blender scene. All data comes from the raw mesh
(`obj.data`) for geometry, UVs, and bone weights. An evaluated mesh
(`evaluated_get(depsgraph).to_mesh()`) is used only to gather:

- Per-loop corner normals (for sharp edge split detection and v12 normal values)

The evaluated mesh is freed immediately after gathering this data. Sharp edge
splitting creates duplicate vertices only in the exported MD5 data structures,
not in Blender's mesh.

> **2.1:** the evaluated mesh is no longer used for tangents. `calc_tangents()`
> is not relied upon (its values desynced from the stored normals — see
> Changelog). Tangents are recomputed from the final exported geometry; see
> "Tangent Generation".

### Normal Sources

For **smooth vertices** (no sharp edge split):
- v10: `mesh.vertices[i].normal` (per-vertex smooth average, engine derives its own)
- v12: `mesh.vertices[i].normal` (per-vertex smooth average, transformed to bone-local)

For **split vertices** (at sharp edges, when "Use sharp edges" is checked):
- Both v10 and v12: `eval_mesh.corner_normals[loop_idx]` (per-loop split normal)
- For v10 this value is not written to file (engine derives normals from geometry;
  the split itself creates the geometric discontinuity for correct normals)
- For v12 this value is transformed to bone-local space and written to the file

### Tangent Generation (v12 only)

**As of 2.1 the exporter recomputes the tangent frame from the final exported
geometry** rather than reading `calc_tangents()`. This is a post-process pass
(`recompute_tangents_v12`) that runs after all submesh vertices and faces are
built, before the bone-local transform and file write. For each submesh:

1. **Per face**, derive the tangent `T` and bitangent `B` from the UV gradient
   (Lengyel): `T = (e1·dv2 − e2·dv1)/det`, `B = (e2·du1 − e1·du2)/det`, using the
   world-space vertex positions and the *stored* (V-flipped) md5 UVs.
2. **Area-weight and direction-normalize** each face's contribution
   (`T = normalize(T)·area`) so heavily UV-stretched faces — tiny UV area, huge
   `1/det` — cannot dominate the accumulated frame or flip the handedness sign.
3. **Per vertex**, Gram-Schmidt the accumulated tangent against the stored normal
   (`T = normalize(T − N·dot(N,T))`), guaranteeing `T ⊥ N`.
4. **Bitangent sign** `tw` is taken from the UV winding:
   `+1` if `dot(cross(N,T), B) ≥ 0`, else `−1`. Non-mirrored meshes come out
   near-uniform; a handful of degenerate-UV verts may receive an arbitrary sign,
   which is harmless.

A second Gram-Schmidt is applied in bone-local space at write time, so the
written tangent is perpendicular to the written normal even if the dominant-bone
matrix carries scale.

**Why recompute instead of fixing `calc_tangents()` indexing:** the recompute
operates only on data that is actually written to the file, so it is immune to
the evaluated-vs-raw mesh loop mismatch, object scale, and modifier stacks that
made `calc_tangents()` unreliable. The trade-off is accuracy: this is a
Lengyel-style frame, **close to but not bit-identical to MikkTSpace**. The
residual difference from a true MikkTSpace bake is far below the error of the
old corrupt data and, in practice, below visible threshold. If an exact
MikkTSpace match is ever required, the alternative is to fix the indexing
(compute `calc_tangents()` on the same raw mesh that is iterated) and keep the
bone-local Gram-Schmidt net — but the recompute is the default because it is
robust without per-scene testing.

**Invariant:** every exported v12 tangent is unit-length and perpendicular to its
vertex normal. Verify with `check_md5_tangents.py` (see Validation Tooling) — a
correct export reports mean angle 90.00° and PASS.

### Bone-Local Transform (v12 only)

Normals and tangents are stored relative to the **dominant bone** (highest skinning
weight). The exporter transforms from world space to bone-local space:

```python
inv_bone_3x3 = bone.matrix.inverted().to_3x3()
normal_local = (inv_bone_3x3 @ normal_world).normalized()
```

Where `bone.matrix = armature.matrix_world @ bone.matrix_local`.

The engine reverses this: `model_normal = joints[bestJoint].ToMat3() * stored_normal`.

### Animation Files

The exporter always writes `MD5Version 10` in md5anim files. The animation format
has no v12-specific extensions. The engine's animation parser should accept both
version 10 and 12 for forward compatibility.

---

## File Format: md5mesh

### Header

```
MD5Version 12
commandline "Exported from Blender by io_export_md5.py"

numJoints 71
numMeshes 1
```

### Joints Block

Identical to v10.

```
joints {
    "origin"  -1 ( 0.000000 0.000000 0.000000 ) ( -0.500000 -0.500000 -0.500000 )
    "Body"     0 ( 0.000000 0.000000 36.882813 ) ( -0.500000 -0.500000 -0.500000 )
    ...
}
```

### Mesh Block

```
mesh {
    shader "models/monsters/imp/skin"

    numverts 891
    vert 0 ( 0.394531 0.248016 ) 0 2 ( 0.000000 0.707107 0.707107 ) ( 0.707107 0.000000 0.000000 1.000000 )
    ...

    numtris 1346
    tri 0 0 2 1
    ...

    numweights 1401
    weight 0 16 0.750000 ( -1.234567 2.345678 3.456789 )
    ...

    numvertexcolors 891
    vertexcolor 0 ( 1.000000 1.000000 1.000000 1.000000 )
    ...
}
```

### Vertex Line Format (v12)

```
vert INDEX ( u v ) FIRST_WEIGHT WEIGHT_COUNT ( nx ny nz ) ( tx ty tz tw )
```

| Field | Type | Description |
|-------|------|-------------|
| `INDEX` | int | Vertex index (0-based) |
| `u v` | float | Texture coordinates (v flipped: `1.0 - blender_v`) |
| `FIRST_WEIGHT` | int | Index of first weight in weights block |
| `WEIGHT_COUNT` | int | Number of weights (max 4) |
| `nx ny nz` | float | Vertex normal in bone-local space (unit length) |
| `tx ty tz` | float | Tangent in bone-local space, unit length, **guaranteed perpendicular to the normal** (recomputed from geometry; Lengyel-style, MikkTSpace-compatible) |
| `tw` | float | Bitangent sign: `+1.0` or `-1.0`, from UV winding |

### Vertex Colors Block (Optional)

Appears after `numweights` inside the mesh block. Omitted if no vertex colors.

```
vertexcolor INDEX ( r g b a )
```

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `INDEX` | int | 0..numverts-1 | Must match vertex index |
| `r g b a` | float | 0.0 – 1.0 | Linear color, not sRGB |

If present, must have exactly `numverts` entries.

### Triangles and Weights Blocks

Identical to v10.

---

## File Format: md5anim

Structurally identical between v10 and v12. The exporter always writes
`MD5Version 10`. The engine should accept both 10 and 12.

---

## Engine Implementation

### Files to Modify

| File | Change |
|------|--------|
| `neo/renderer/Model.h` | Add `#define MD5_VERSION_V12 12` |
| `neo/renderer/Model_local.h` | Add `bool isV12` to `idRenderModelMD5`, change `ParseMesh` signature |
| `neo/renderer/Model_md5.cpp` | Version gate, extended vertex parsing, normal/tangent transform |
| `neo/renderer/tr_trisurf.cpp` | `R_BuildDeformInfo` skips tangent derivation for v12 |
| `neo/renderer/tr_local.h` | Add `hasExplicitTangents` param to `R_BuildDeformInfo` |
| `neo/d3xp/anim/Anim.cpp` | Accept version 12 in `idMD5Anim::LoadAnim()` |

### No Shader Changes Required

Existing shaders already implement MikkTSpace reconstruction:

```hlsl
float4 vNormal = vertex.normal * 2.0 - 1.0;
float4 vTangent = vertex.tangent * 2.0 - 1.0;
float3 vBitangent = cross( vNormal.xyz, vTangent.xyz ) * vTangent.w;
```

`SetNormal()` / `SetTangent()` / `SetBiTangentSign()` in `DrawVert.h` pack into
the same byte format that shaders unpack with `* 2.0 - 1.0`.

> **2.1 — important caveat.** This "no shader changes" claim holds *only* when
> the exported tangents are orthonormal to the normals. The reconstruction above
> builds `vBitangent = cross(N, T)·tw` and decodes the normal map in that basis;
> a tangent that is not perpendicular to `N` skews the frame and produces
> UV-shell seams — which is exactly what the pre-2.1 exporter caused.
>
> During bring-up, while the exporter was emitting non-perpendicular tangents, a
> per-pixel cotangent (Schüler/Mikkelsen) frame was added to
> `interaction_uber.pixel` to reconstruct the basis from screen-space `ddx/ddy`
> derivatives and sidestep the bad per-vertex data. With 2.1 exporting correct
> tangents, that workaround is redundant — it added a per-fragment derivative
> solve per light plus a faint view-dependent residual — and has been **retired**
> (reverted to the per-vertex path, restoring the `bumpFlatness` blend). The
> terrain shaders never carried it in shipped form. Net: with a 2.1-or-later
> export, no shader changes are required, as designed.

### ParseMesh Changes

1. Parse `( nx ny nz ) ( tx ty tz tw )` after weight indices for v12 verts
2. Parse optional `numvertexcolors` block after weights
3. Transform normals/tangents from bone-local to model space using dominant joint
4. Set on `basePose` via `SetNormal()`, `SetTangent()`, `SetBiTangentSign()`
5. Call `R_BuildDeformInfo` with `hasExplicitTangents = true`

### R_BuildDeformInfo Changes

When `hasExplicitTangents` is true:
- Skip `R_BuildDominantTris()` and `R_DeriveTangents()`
- Set `tri.tangentsCalculated = true`
- Silhouette data still builds normally (needed for shadows)
- `R_DuplicateMirroredVertexes` copies full `idDrawVert` including normals/tangents

### Binary Cache

Bump `MD5B_VERSION` from 106 to 107. No changes to `LoadBinaryModel` /
`WriteBinaryModel` — they serialize final `deformInfo->verts` which already
contain packed normal/tangent data.

### Vertex Colors — Future Work

Parsed but not wired to rendering. `color`/`color2` are used for skinning in v10.
Options: new vertex attribute, texture/SSBO lookup, or bake into diffuse.

---

## Validation Tooling

Because the engine trusts the stored v12 tangents and does **not** re-derive them
(in-engine re-derivation reintroduces the classic idTech 4 UV-seam problem,
visible even without normal maps), exporter correctness is load-bearing. Two
standalone, dependency-free checkers gate it:

**`check_md5_tangents.py <mesh.md5mesh>`** — reads each v12 vert and reports the
angle between the stored tangent and stored normal. A correct export is mean
90.00° with ~100% of verts within 5° of perpendicular → PASS. A corrupt export
(the pre-2.1 bug) reports a mean well off 90° (e.g. ~101°, with verts ranging
3°–180°) → FAIL. Run this on every md5 export; a regression announces itself
immediately without a bake or engine load.

**`check_uv_match.py <mesh.obj> <mesh.md5mesh>`** — confirms the bake mesh and the
md5 share the same UV unwrap (V-flip aware). PASS means the normal map baked
against the OBJ will align with the md5 in-game.

## Pipeline Notes — UV Unwrap Consistency

A tangent-space normal map is baked relative to a specific unwrap's tangents and
UVs. The mesh you bake against (the OBJ/FBX handed to Substance/Marmoset/xNormal)
and the md5 the engine loads **must carry the same UV coordinates**. They may
differ in vertex/triangle count — OBJ and md5 split UV seams slightly differently
— but the UV coordinate set must match (the md5 V is flipped: `1 − blender_v`).

The exporter does not re-unwrap or repack; its only UV transform is the V-flip. So
a mismatch never originates in the exporter — it means the two files were
generated from different UV state. Common causes:

- **More than one UV map**, where the OBJ exporter grabbed the active-*render*
  layer (camera icon) while the md5 exporter read `uv_layers.active` (the
  highlighted row). Keep a single UV map, or ensure both icons point at the same
  one.
- **A stale bake mesh** exported before a re-unwrap.

You do **not** need a rigged mesh to bake — bones are irrelevant to a normal-map
bake. Export the low-poly (same object, same UV map that feeds the md5) to OBJ,
rigged or not. Run `check_uv_match.py` on the exact pair before spending a bake;
it must report SAME UNWRAP.

---

## Testing Checklist

| Test | Expected Result |
|------|----------------|
| v10, sharp edges OFF | Identical to previous exporter output |
| v10, sharp edges ON, edges marked | Extra vertices at sharp edges, correct flat shading |
| v12, sharp edges OFF | Normals + tangents, same vertex count as v10 |
| v12, sharp edges ON, edges marked | Normals + tangents, extra vertices at sharp edges |
| v12 with vertex colors | `numvertexcolors` block present |
| v12 without vertex colors | No `numvertexcolors` block |
| md5anim export | Always writes `MD5Version 10` |
| Engine loads v10 mesh | Unchanged behavior |
| Engine loads v12 mesh | Reads normals/tangents/colors |
| Engine loads v10/v12 anim | Both accepted |
| Normal map baked in Blender | v12 matches Blender viewport |
| Blender scene after export | Unchanged (non-destructive) |
| `check_md5_tangents.py` on v12 export | PASS — mean 90.00°, ~100% within 5° of perpendicular |
| `check_uv_match.py` on bake-OBJ + md5 pair | SAME UNWRAP |
| v12 in-game, normal map across UV shells | No shading seam at shell boundaries |

---

## File Size Impact

For a typical character mesh (891 verts, 71 joints):

| Component | v10 | v12 | Delta |
|-----------|-----|-----|-------|
| vert line | ~40 chars | ~100 chars | +60 chars/vert |
| vertexcolor line | 0 | ~50 chars | +50 chars/vert |
| Total mesh file | ~120 KB | ~220 KB | ~+80% |
| md5anim file | unchanged | unchanged | 0% |

Size increase is text-only. The engine's binary cache packs efficiently.
