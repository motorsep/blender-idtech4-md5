# MD5 Version 12 Format Specification

**Version:** 2.0
**Date:** March 2026
**Status:** Exporter complete, engine implementation in progress

---

## Overview

MD5 Version 12 is a backward-compatible extension of the idTech 4 MD5 format
(version 10) that adds per-vertex normals, MikkTSpace tangent frames, and vertex
colors to `md5mesh` files. The `md5anim` format is structurally identical and
always uses version 10.

The primary benefit is correct normal map rendering: MikkTSpace tangents from the
exporter match what modern baking tools (Blender, Substance Painter, Marmoset,
xNormal) produce, eliminating tangent basis mismatches that cause subtle shading
errors in v10.

---

## What Changed from Version 10

| Feature | v10 | v12 |
|---------|-----|-----|
| `MD5Version` header | `10` | `12` |
| Per-vertex normal | Not stored; derived from geometry | Stored in bone-local space |
| Per-vertex tangent | Not stored; derived from geometry | MikkTSpace tangent in bone-local space |
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
- MikkTSpace tangents (for v12 tangent values)

The evaluated mesh is freed immediately after gathering this data. Sharp edge
splitting creates duplicate vertices only in the exported MD5 data structures,
not in Blender's mesh.

### Normal Sources

For **smooth vertices** (no sharp edge split):
- v10: `mesh.vertices[i].normal` (per-vertex smooth average, engine derives its own)
- v12: `mesh.vertices[i].normal` (per-vertex smooth average, transformed to bone-local)

For **split vertices** (at sharp edges, when "Use sharp edges" is checked):
- Both v10 and v12: `eval_mesh.corner_normals[loop_idx]` (per-loop split normal)
- For v10 this value is not written to file (engine derives normals from geometry;
  the split itself creates the geometric discontinuity for correct normals)
- For v12 this value is transformed to bone-local space and written to the file

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
| `tx ty tz` | float | MikkTSpace tangent in bone-local space (unit length) |
| `tw` | float | Bitangent sign: `+1.0` or `-1.0` |

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
